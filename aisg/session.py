"""Long-lived AISG session: one owner for the port, a queue for everyone else.

The CLI opens the port, does one thing and exits. A service cannot: the link
is stateful (address assignment, then an SNRM session with per-peer sequence
numbers), reconnecting costs a scan, and the bus is half-duplex so commands,
keepalive polls and alarm reports all contend for the same UART.

So exactly one thread -- SerialWorker -- ever touches the port. Everything
else submits a Command and waits on its Future. That is also what serialises
access, which a device with allocatable=1 needs anyway.

Monitoring modes, because "hold the link open to watch for alarms" and
"release the port so the CLI can use it" are mutually exclusive in Normal
Response Mode (the secondary only speaks when polled, so watching means
polling forever, so the idle timer never fires):

  off          on-demand only; port released after IDLE_RELEASE_S
  duty-cycled  open -> connect -> GetAlarmStatus -> disconnect -> close,
               every MONITOR_PERIOD_S. Alarm latency ~= the period, and the
               port is free the rest of the time. The default.
  continuous   port pinned, keepalive every KEEPALIVE_S, no idle release.
               Alarm latency ~= the keepalive interval; the CLI gets
               PortBusy until an operator takes a lease.
"""

from __future__ import annotations

import enum
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import hdlc
from .link import (ACCEPT, INDICATION, UNEXPECTED, AisgError, AisgLink,
                   AisgTimeout, Device, LinkReset, PortBusy, PortLost)
from . import retap
from .retap import Ret

logger = logging.getLogger("aisg.session")


class LinkState(str, enum.Enum):
    CLOSED = "closed"            # no fd, no lock: the CLI is free to use it
    OPENING = "opening"
    PORT_BUSY = "port_busy"      # someone else holds the flock
    PORT_LOST = "port_lost"      # node exists, adapter not answering
    DISCOVERING = "discovering"
    NO_DEVICES = "no_devices"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DRAINING = "draining"        # post-timeout quarantine
    RELEASING = "releasing"
    BACKOFF = "backoff"
    LEASED = "leased"            # deliberately handed to an operator
    SHUTDOWN = "shutdown"


class MonitorMode(str, enum.Enum):
    OFF = "off"
    DUTY_CYCLED = "duty-cycled"
    CONTINUOUS = "continuous"


# States in which a queued command fails immediately rather than waiting for a
# link that is not coming soon. A hung HTTP request is worse than an honest
# 503.
FAIL_FAST = {LinkState.PORT_BUSY, LinkState.PORT_LOST, LinkState.NO_DEVICES,
             LinkState.BACKOFF, LinkState.LEASED, LinkState.SHUTDOWN}


class SessionError(AisgError):
    """The link could not be made available for this command."""

    def __init__(self, state: LinkState, detail: str = ""):
        self.state = state
        super().__init__(f"link unavailable ({state.value})"
                         + (f": {detail}" if detail else ""))


# --- layer-7 demultiplexer -------------------------------------------------

INDICATION_CODES = {retap.ALARM_INDICATION, retap.ANT_ALARM_INDICATION}


class RetapRouter:
    """Classify an inbound I-frame payload for AisgLink.exchange().

    The expected procedure code is read from the request itself (retap puts
    it in byte 0), so Ret needs no changes to benefit from this.
    """

    def __init__(self, expected: int | None):
        self.expected = expected

    def __call__(self, payload: bytes) -> str:
        if not payload:
            return UNEXPECTED
        code = payload[0]
        if code in INDICATION_CODES:
            return INDICATION
        if self.expected is None or code == self.expected:
            return ACCEPT
        return UNEXPECTED


class SerialTransport:
    """Duck-types AisgLink.request() so Ret can be used unchanged."""

    def __init__(self, link: AisgLink, on_indication=None, on_poll=None):
        self._link = link
        self._on_indication = on_indication
        self._on_poll = on_poll

    def request(self, addr: int, message: bytes, timeout: float = 3.0) -> bytes:
        return self._link.exchange(
            addr, message, timeout=timeout,
            router=RetapRouter(message[0] if message else None),
            on_indication=self._on_indication,
            on_poll=self._on_poll,
        )


# --- alarms ----------------------------------------------------------------

class AlarmTracker:
    """Holds the active alarm set and emits edges.

    Fed either a full snapshot (GetAlarmStatus) or a single indication, so
    the polling and subscribed paths converge on one representation and a
    duplicated report is idempotent rather than a duplicated event.
    """

    def __init__(self):
        self.active: set[str] = set()

    def snapshot(self, names, emit: bool = True) -> list[dict]:
        new = {n for n in names if n and n != "OK"}
        if not emit:
            self.active = new
            return []
        events = [{"type": "alarm", "action": "raised", "name": n}
                  for n in sorted(new - self.active)]
        events += [{"type": "alarm", "action": "cleared", "name": n}
                   for n in sorted(self.active - new)]
        self.active = new
        return events

    def indicate(self, payload: bytes) -> list[dict]:
        """An AlarmIndication payload: proc | len16 | [antenna] | rc | codes."""
        names = [retap.rc_name(c) for c in payload[3:]]
        merged = self.active | {n for n in names if n and n != "OK"}
        return self.snapshot(merged)


# --- commands --------------------------------------------------------------

def _sweep_device_data(ret: Ret, last: int = 0x0B) -> dict:
    """Read the antenna parameter block. Fields above 0x0B answer FAIL on the
    RET21-AS155D, so stop there rather than generating pointless traffic."""
    out = {}
    for fid in range(1, last + 1):
        try:
            out[f"0x{fid:02X}"] = ret.get_device_data(fid).hex(" ")
        except AisgError:
            continue
    return out


def _set_tilt(ret: Ret, degrees: float) -> dict:
    ret.set_tilt(degrees)
    return {"tilt": ret.get_tilt()}


READ_COMMANDS: dict[str, Callable[..., Any]] = {
    "get_information": lambda ret: ret.get_information(),
    "get_tilt": lambda ret: {"tilt": ret.get_tilt()},
    "get_alarm_status": lambda ret: {"alarms": ret.get_alarm_status()},
    "get_device_data": _sweep_device_data,
}

WRITE_COMMANDS: dict[str, Callable[..., Any]] = {
    "set_tilt": _set_tilt,
    "clear_alarms": lambda ret: ret.clear_alarms(),
    "calibrate": lambda ret: ret.calibrate(),
    "self_test": lambda ret: ret.self_test(),
    "reset": lambda ret: ret.reset(),
}

ALL_COMMANDS = {**READ_COMMANDS, **WRITE_COMMANDS}

# Per-command wall-clock ceilings. The caller's timeout is the *total* budget
# (the old request() multiplied it by retries+1, so a calibrate could occupy
# the worker for 8 minutes).
COMMAND_TIMEOUTS = {
    "calibrate": 150.0,
    "self_test": 45.0,
    "set_tilt": 90.0,
    "get_device_data": 30.0,
}
DEFAULT_COMMAND_TIMEOUT = 15.0

PRIORITY_CONTROL = 0    # force-reset, lease: must not queue behind a calibrate
PRIORITY_USER = 5
PRIORITY_MONITOR = 9


@dataclass(order=True)
class Command:
    priority: int
    seq: int
    name: str = field(compare=False)
    args: dict = field(compare=False, default_factory=dict)
    future: Any = field(compare=False, default=None)
    submitted_at: float = field(compare=False, default_factory=time.monotonic)
    link_deadline: float = field(compare=False, default=0.0)


class SerialWorker(threading.Thread):
    """Sole owner of the serial port and the AISG link."""

    def __init__(self, port: str, *, baud: int = 9600, dtr: bool = True,
                 monitor: MonitorMode = MonitorMode.DUTY_CYCLED,
                 monitor_period: float = 60.0, keepalive: float = 1.0,
                 idle_release: float = 60.0, first_address: int = 1,
                 probe_count: int = 4, emit=None, debug: bool = False):
        super().__init__(name="aisg-worker", daemon=True)
        self.port = port
        self.baud = baud
        self.dtr = dtr
        self.monitor = monitor
        self.monitor_period = monitor_period
        self.keepalive = keepalive
        self.idle_release = idle_release
        self.first_address = first_address
        self.probe_count = probe_count
        self.debug = debug
        self._emit = emit or (lambda ev: None)

        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.state = LinkState.CLOSED
        self.link: AisgLink | None = None
        self.device: Device | None = None
        self.address: int | None = None
        self.identity: dict = {}
        self.device_data: dict = {}
        self.tilt: float | None = None
        self.target_tilt: float | None = None
        self.alarms = AlarmTracker()
        self.alarm_mode = "unknown"      # subscribed | polled | unknown
        self._subscribe_unsupported = False

        self._last_error = ""
        self._last_command_at = 0.0
        self._last_monitor_at = 0.0
        self._last_keepalive_at = 0.0
        self._backoff = 1.0
        self._backoff_until = 0.0
        self._lease_until = 0.0
        self._heartbeat = time.monotonic()
        self._indications: list[bytes] = []
        self._want_link = False

    # -- public API (called from the event loop thread) --

    def submit(self, name: str, args: dict | None = None,
               priority: int = PRIORITY_USER, link_wait: float = 20.0):
        import concurrent.futures
        if name not in ALL_COMMANDS:
            raise ValueError(f"unknown command {name!r}")
        cmd = Command(priority, next(self._seq), name, args or {},
                      concurrent.futures.Future())
        cmd.link_deadline = time.monotonic() + link_wait
        self._q.put(cmd)
        return cmd.future

    def lease(self, seconds: float):
        """Release the port and refuse to reopen it for `seconds`.

        The idle timer alone does not give the CLI the port: any monitor tick
        or HTTP request reopens it, and the operator loses the race. This is
        the explicit handoff.
        """
        with self._lock:
            self._lease_until = time.monotonic() + seconds
        return self._lease_until

    def unlease(self):
        with self._lock:
            self._lease_until = 0.0

    def snapshot(self) -> dict:
        """Current state, cheap and lock-protected. Never enqueues -- health
        probes must not queue behind a 150s calibrate."""
        with self._lock:
            leased = max(0.0, self._lease_until - time.monotonic())
            return {
                "state": self.state.value,
                "port": self.port,
                "monitor": self.monitor.value,
                "alarm_mode": self.alarm_mode,
                "address": self.address,
                "tilt": self.tilt,
                "target_tilt": self.target_tilt,
                "alarms": sorted(self.alarms.active),
                "identity": dict(self.identity),
                "queue_depth": self._q.qsize(),
                "leased_for": round(leased, 1) if leased else 0,
                "port_pinned": self.monitor is MonitorMode.CONTINUOUS,
                "last_error": self._last_error,
                "heartbeat_age": round(time.monotonic() - self._heartbeat, 2),
                "stats": dict(self.link.stats) if self.link else {},
                "fcs_errors": self.link.fcs_errors if self.link else 0,
            }

    def stop(self):
        self._stop.set()

    # -- worker internals --

    def _set_state(self, state: LinkState, error: str = ""):
        with self._lock:
            if state is self.state and error == self._last_error:
                return
            self.state = state
            if error:
                self._last_error = error
        logger.info("state -> %s%s", state.value, f" ({error})" if error else "")
        self._emit({"type": "link", "state": state.value, "error": error})

    def run(self):
        head: Command | None = None
        while not self._stop.is_set():
            self._heartbeat = time.monotonic()
            try:
                # Clear an expired or dropped lease before dispatching, or a
                # command submitted right after unlease() fails fast against
                # a state the next _step() was about to leave anyway.
                self._refresh_lease()
                if head is None:
                    head = self._take()
                if head is not None:
                    handled = self._try_execute(head)
                    if handled:
                        head = None
                        continue
                self._step()
            except Exception:  # noqa: BLE001 - the worker must never die
                logger.exception("worker loop")
                self._fail_link("internal error")
                time.sleep(0.5)
        self._teardown()
        self._set_state(LinkState.SHUTDOWN)

    def _refresh_lease(self):
        if self.state is LinkState.LEASED and self._lease_until <= time.monotonic():
            self._set_state(LinkState.CLOSED)

    def _take(self) -> Command | None:
        try:
            return self._q.get(timeout=self._tick_budget())
        except queue.Empty:
            return None

    def _tick_budget(self) -> float:
        """How long to wait for work before doing housekeeping."""
        if self.state is LinkState.CONNECTED:
            return min(0.25, self.keepalive)
        return 0.25

    def _try_execute(self, cmd: Command) -> bool:
        """True when the command is finished with (done or failed).

        The command may sit here across many loop passes while the link comes
        up, so the future is only claimed at the moment we commit to it --
        set_running_or_notify_cancel() raises if called twice.
        """
        # _take() blocks for up to a tick, so a lease can be dropped while we
        # were waiting for this command. Re-read it rather than failing the
        # command against a state we have already left.
        self._refresh_lease()
        if self.state is LinkState.CONNECTED:
            if not cmd.future.set_running_or_notify_cancel():
                return True  # cancelled while queued
            self._execute(cmd)
            return True
        if self.state in FAIL_FAST:
            self._abandon(cmd, SessionError(self.state, self._last_error))
            return True
        if time.monotonic() > cmd.link_deadline:
            self._abandon(cmd, SessionError(self.state, "timed out waiting for link"))
            return True
        self._want_link = True
        return False

    @staticmethod
    def _abandon(cmd: Command, exc: Exception):
        if cmd.future.set_running_or_notify_cancel():
            cmd.future.set_exception(exc)

    def _execute(self, cmd: Command):
        fn = ALL_COMMANDS[cmd.name]
        budget = COMMAND_TIMEOUTS.get(cmd.name, DEFAULT_COMMAND_TIMEOUT)
        started = time.monotonic()

        def on_poll(polls, acked):
            if polls % 20 == 0:
                self._emit({"type": "progress", "command": cmd.name,
                            "polls": polls, "acked": acked,
                            "elapsed": round(time.monotonic() - started, 1)})

        transport = SerialTransport(self.link, on_indication=self._queue_indication,
                                    on_poll=on_poll)
        ret = Ret(transport, self.address)
        if cmd.name == "set_tilt":
            with self._lock:
                self.target_tilt = float(cmd.args.get("degrees"))
        try:
            result = fn(ret, **cmd.args) if cmd.args else fn(ret)
        except LinkReset as e:
            self._last_error = str(e)
            self._fail_link(str(e))
            cmd.future.set_exception(e)
        except (AisgTimeout, AisgError) as e:
            self._last_error = str(e)
            if isinstance(e, AisgTimeout) and e.acked:
                # The device has the command and may still be acting on it.
                # Quarantine the link so a late response is not misread as
                # the answer to whatever runs next.
                self._set_state(LinkState.DRAINING, "late response expected")
            cmd.future.set_exception(e)
        else:
            self._absorb(cmd.name, result)
            cmd.future.set_result(result)
        finally:
            self._last_command_at = time.monotonic()
            self._drain_indications()
        _ = budget  # per-command ceiling is enforced by retap timeouts

    def _absorb(self, name: str, result):
        """Fold a command result into the cached state."""
        with self._lock:
            if isinstance(result, dict):
                if "tilt" in result:
                    self.tilt = result["tilt"]
                if "alarms" in result:
                    pass  # handled below, outside the lock
                if name == "get_information":
                    self.identity = dict(result)
                if name == "get_device_data":
                    self.device_data = dict(result)
        if isinstance(result, dict) and "alarms" in result:
            for ev in self.alarms.snapshot(result["alarms"]):
                self._emit(ev)
        self._emit({"type": "status", **self.snapshot()})

    def _queue_indication(self, payload: bytes):
        """Called from inside exchange(). Window size 1 means we cannot send
        anything right now, so park it and handle it when the bus is ours."""
        self._indications.append(payload)

    def _drain_indications(self):
        while self._indications:
            payload = self._indications.pop(0)
            for ev in self.alarms.indicate(payload):
                self._emit(ev)

    # -- state machine step --

    def _step(self):
        now = time.monotonic()
        if self._lease_until > now:
            if self.state not in (LinkState.LEASED, LinkState.CLOSED):
                self._release()
            self._set_state(LinkState.LEASED)
            return
        if self.state is LinkState.LEASED:
            self._set_state(LinkState.CLOSED)

        if self.state in (LinkState.CLOSED, LinkState.PORT_BUSY,
                          LinkState.PORT_LOST, LinkState.NO_DEVICES):
            if self._should_open(now):
                self._open_and_connect()
            return
        if self.state is LinkState.BACKOFF:
            if now >= self._backoff_until:
                self._set_state(LinkState.CLOSED)
            return
        if self.state is LinkState.DRAINING:
            self._drain()
            return
        if self.state is LinkState.CONNECTED:
            self._connected_housekeeping(now)
            return

    def _should_open(self, now: float) -> bool:
        if self._want_link or not self._q.empty():
            return True
        if self.monitor is MonitorMode.CONTINUOUS:
            return True
        if self.monitor is MonitorMode.DUTY_CYCLED:
            return now - self._last_monitor_at >= self.monitor_period
        return False

    def _open_and_connect(self):
        self._set_state(LinkState.OPENING)
        try:
            self.link = AisgLink(self.port, baud=self.baud, dtr=self.dtr,
                                 debug=self.debug)
        except PortBusy as e:
            self._set_state(LinkState.PORT_BUSY, str(e))
            self._arm_backoff()
            return
        except PortLost as e:
            self._set_state(LinkState.PORT_LOST, str(e))
            self._arm_backoff()
            return
        except AisgError as e:
            self._set_state(LinkState.BACKOFF, str(e))
            self._arm_backoff()
            return

        self._clear_stale_links()
        self._set_state(LinkState.DISCOVERING)
        try:
            addr = self._discover()
        except AisgError as e:
            self._release()
            self._set_state(LinkState.BACKOFF, str(e))
            self._arm_backoff()
            return
        if addr is None:
            self._release()
            self._set_state(LinkState.NO_DEVICES,
                            "no AISG devices answered (power feed? RS-485 wiring?)")
            self._arm_backoff()
            return

        self._set_state(LinkState.CONNECTING)
        try:
            self.link.connect(addr)
        except AisgError as e:
            self._release()
            self._set_state(LinkState.BACKOFF, str(e))
            self._arm_backoff()
            return
        self.address = addr
        self._want_link = False
        if not self._on_connected():
            # The link died during entry actions; going to CONNECTED here
            # would advertise a session that is already gone.
            self._fail_link(self._last_error or "entry actions failed")
            return
        self._backoff = 1.0
        self._set_state(LinkState.CONNECTED)

    def _clear_stale_links(self):
        """DISC anything that might still think it has a link with us.

        The CLI never disconnects -- it just closes the port -- so a device
        can be left holding an address and a half-open link. Starting from
        DISC makes the opening state the same however the previous session
        ended, and since DISC also makes this RET drop its address, the plain
        broadcast scan then finds it again, which is the best-tested path.

        Cheap insurance rather than a fix for anything: the FRMR seen while
        building this turned out to be a half-duplex bug in the read pump,
        not stale device state.
        """
        for addr in {a for a in (self.address, self.first_address)
                     if a is not None}:
            try:
                self.link.disconnect(addr, timeout=0.3)
            except AisgError:
                pass
        self.address = None

    def _discover(self) -> int | None:
        """Broadcast scan first, then probe for an already-addressed device."""
        devices = self.link.scan()
        if devices:
            for i, dev in enumerate(devices):
                self.link.assign_address(dev, self.first_address + i)
            self.device = devices[0]
            return devices[0].address
        found = self.link.scan_assigned(self.first_address, self.probe_count)
        if found:
            # scan_assigned leaves each answering peer connected; keep only
            # the one we intend to drive.
            for dev in found[1:]:
                try:
                    self.link.disconnect(dev.address)
                except AisgError:
                    pass
            return found[0].address
        return None

    def _on_connected(self) -> bool:
        """Entry actions. Order matters: identity, then subscribe, then a
        silent alarm baseline so a reconnect does not replay old alarms as
        fresh events.

        False means the link is unusable and the caller must not advertise it.
        """
        transport = SerialTransport(self.link, on_indication=self._queue_indication)
        ret = Ret(transport, self.address)
        try:
            if not self.identity:
                self.identity = ret.get_information()
            if not self.device_data:
                self.device_data = _sweep_device_data(ret)
            self.alarm_mode = self._subscribe(ret)
            self.alarms.snapshot(ret.get_alarm_status(), emit=False)
            self.tilt = ret.get_tilt()
        except LinkReset as e:
            self._last_error = str(e)
            logger.warning("link reset during entry actions: %s", e)
            return False
        except AisgError as e:
            # A single procedure the device dislikes (an unsupported field,
            # say) is not a reason to refuse the whole session.
            logger.warning("entry actions incomplete: %s", e)
        return True

    def _subscribe(self, ret: Ret) -> str:
        """AlarmSubscribe is per-link and must be redone after every SNRM.

        Only a permanent rejection is cached -- an UnsupportedProcedure means
        this device will never support it, so stop asking and poll instead.
        """
        if self._subscribe_unsupported:
            return "polled"
        try:
            ret.alarm_subscribe()
            return "subscribed"
        except AisgError as e:
            if any(k in str(e) for k in ("UnknownProcedure", "UnsupportedProcedure")):
                self._subscribe_unsupported = True
                logger.info("AlarmSubscribe unsupported; falling back to polling")
            return "polled"

    def _connected_housekeeping(self, now: float):
        due_monitor = (self.monitor is MonitorMode.DUTY_CYCLED
                       and now - self._last_monitor_at >= self.monitor_period)
        if due_monitor or (self.monitor is MonitorMode.CONTINUOUS
                           and self.alarm_mode == "polled"
                           and now - self._last_monitor_at >= self.monitor_period):
            self._last_monitor_at = now
            self._poll_alarms()
            if self.monitor is MonitorMode.DUTY_CYCLED and self._q.empty():
                self._release()   # duty cycle: give the port back
                self._set_state(LinkState.CLOSED)
                return
        if self.monitor is MonitorMode.CONTINUOUS:
            if now - self._last_keepalive_at >= self.keepalive:
                self._last_keepalive_at = now
                self._keepalive_poll()
            return
        idle_for = now - max(self._last_command_at, self._last_monitor_at)
        if self.monitor is MonitorMode.OFF and idle_for >= self.idle_release:
            self._release()
            self._set_state(LinkState.CLOSED)

    def _poll_alarms(self):
        transport = SerialTransport(self.link, on_indication=self._queue_indication)
        ret = Ret(transport, self.address)
        try:
            for ev in self.alarms.snapshot(ret.get_alarm_status()):
                self._emit(ev)
            self.tilt = ret.get_tilt()
        except LinkReset as e:
            self._fail_link(str(e))
        except AisgError as e:
            logger.debug("alarm poll: %s", e)
        finally:
            self._drain_indications()

    def _keepalive_poll(self):
        """A bare RR. In NRM this is also how a subscribed AlarmIndication
        reaches us, so the keepalive interval is the alarm latency floor."""
        try:
            p = self.link._peer(self.address)
            self.link._write(self.link._rr(self.address, p.vr))
            for faddr, ctrl, pl in self.link._read_frames(0.2):
                if faddr != self.address:
                    continue
                k = hdlc.kind(ctrl)
                if k == hdlc.U_FRAME and (ctrl & ~hdlc.PF) in (hdlc.CTRL_DM,
                                                               hdlc.CTRL_FRMR):
                    self._fail_link("DM/FRMR on keepalive")
                    return
                if k == hdlc.I_FRAME:
                    ns = hdlc.ns_of(ctrl)
                    if ns != p.vr:
                        continue
                    p.vr = (ns + 1) & 7
                    self.link._write(self.link._rr(self.address, p.vr, poll=False))
                    if pl and pl[0] in INDICATION_CODES:
                        for ev in self.alarms.indicate(pl):
                            self._emit(ev)
        except (OSError, AisgError) as e:
            self._fail_link(str(e))

    def _drain(self):
        """Poll and discard until the bus goes quiet, so a late response to an
        abandoned command is not misattributed to the next one."""
        end = time.monotonic() + 1.5
        try:
            while time.monotonic() < end:
                frames = self.link._read_frames(0.2)
                if not frames:
                    break
                p = self.link._peer(self.address)
                for faddr, ctrl, pl in frames:
                    if faddr == self.address and hdlc.kind(ctrl) == hdlc.I_FRAME:
                        p.vr = (hdlc.ns_of(ctrl) + 1) & 7
                        self.link._write(self.link._rr(self.address, p.vr))
        except (OSError, AisgError) as e:
            self._fail_link(str(e))
            return
        self._set_state(LinkState.CONNECTED)

    def _fail_link(self, why: str):
        self._release()
        self._set_state(LinkState.BACKOFF, why)
        self._arm_backoff()

    def _arm_backoff(self):
        self._backoff = min(self._backoff * 2, 60.0)
        self._backoff_until = time.monotonic() + self._backoff
        if self.state not in (LinkState.PORT_BUSY, LinkState.PORT_LOST,
                              LinkState.NO_DEVICES):
            self._set_state(LinkState.BACKOFF)

    def _release(self):
        if self.link is None:
            return
        self._set_state(LinkState.RELEASING)
        try:
            if self.address is not None:
                self.link.disconnect(self.address)
        except (OSError, AisgError):
            pass
        try:
            self.link.close()
        except OSError:
            pass
        self.link = None

    def _teardown(self):
        while True:
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                break
            if cmd.future.set_running_or_notify_cancel():
                cmd.future.set_exception(SessionError(LinkState.SHUTDOWN))
        self._release()
