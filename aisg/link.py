"""AISG v2.0 primary-station link layer over a serial port.

Implements device scan, address assignment, SNRM connection and
stop-and-wait I-frame exchange (window size 1) per 3GPP TS 25.462.
"""

import errno
import time

import serial

from . import hdlc


class AisgError(Exception):
    pass


class PortBusy(AisgError):
    """Another process holds the port's exclusive lock."""


class PortLost(AisgError):
    """The device node exists but the adapter is not answering (USB wedged,
    unplugged, or re-enumerated). Retrying the open will not help."""


class AisgTimeout(AisgError):
    """No layer-7 response inside the budget.

    Carries whether our I-frame was ever acknowledged: if it was, the device
    has the command and may still act on it, so it must never be resent.
    """

    def __init__(self, addr: int, sent_ns: int, acked: bool, polls: int):
        self.addr = addr
        self.sent_ns = sent_ns
        self.acked = acked
        self.polls = polls
        super().__init__(
            f"no layer-7 response from 0x{addr:02X} "
            f"(N(S)={sent_ns}, {'acked' if acked else 'unacked'}, {polls} polls)"
        )


class LinkReset(AisgError):
    """The secondary dropped the link: FRMR (frame rejected) or DM (discon-
    nected mode). Only a fresh SNRM recovers it -- the secondary cannot
    resynchronise on its own."""

    def __init__(self, addr: int, why: str):
        self.addr = addr
        self.why = why
        super().__init__(f"link to 0x{addr:02X} reset: {why}")


# Router verdicts for inbound I-frames.
ACCEPT = "accept"
INDICATION = "indication"
UNEXPECTED = "unexpected"


def accept_all(_payload) -> str:
    """Default router: first I-frame wins, i.e. the pre-demux behaviour."""
    return ACCEPT


class PeerState:
    """Per-address sequence state.

    These used to be two attributes on AisgLink, shared by every address on
    the bus. AISG allows several addressed secondaries with independent
    sequence state, so one shared pair guarantees a FRMR the moment a second
    device is driven -- and scan_assigned() reset it once per probed address.
    """

    __slots__ = ("vs", "vr", "connected")

    def __init__(self):
        self.vs = 0  # V(S), next N(S) we will send
        self.vr = 0  # V(R), next N(S) we expect to receive
        self.connected = False

    def reset(self):
        self.vs = 0
        self.vr = 0


class Device:
    def __init__(self, unique_id: bytes, device_type: int | None, vendor: bytes | None):
        self.unique_id = unique_id
        self.device_type = device_type
        self.vendor = vendor
        self.address = None

    def __repr__(self):
        vendor = self.vendor.decode("ascii", "replace") if self.vendor else "??"
        uid = self.unique_id.decode("ascii", "replace")
        return (
            f"<Device vendor={vendor} uid={uid!r} type={self.device_type} "
            f"addr={self.address}>"
        )


class AisgLink:
    def __init__(self, port: str, baud: int = 9600, dtr: bool | None = True,
                 timeout: float = 1.0, debug: bool = False,
                 exclusive: bool = True):
        # exclusive=True takes an advisory fcntl.flock on the port. It only
        # arbitrates against other processes that also lock, which is why it
        # belongs here rather than only in the service: with the default on,
        # the CLI and the daemon cannot silently interleave HDLC onto the same
        # half-duplex bus. Contention becomes SerialException instead of FCS
        # garbage plus mutual sequence-number destruction. The lock lives on
        # the fd, so close() releases it.
        try:
            self.ser = serial.Serial(port, baud, bytesize=8, parity="N",
                                     stopbits=1, timeout=0.05,
                                     exclusive=exclusive)
        except serial.SerialException as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise PortBusy(
                    f"{port} is locked by another process (the aisgd service "
                    f"holds it while its AISG session is up)"
                ) from e
            if e.errno in (errno.EIO, errno.EPROTO, errno.ENODEV, errno.ENXIO):
                # Seen on real hardware: the FTDI wedges at the USB level and
                # every open fails with "failed to set flow control: -71"
                # while the device node still exists. A driver rebind or
                # re-plug is the fix, not a retry.
                raise PortLost(f"{port} exists but the adapter is not "
                               f"responding ({e})") from e
            raise AisgError(f"cannot open {port}: {e}") from e
        if dtr is not None:
            self.ser.dtr = dtr
        self.timeout = timeout
        self.debug = debug
        self._peers = {}
        self._deframer = hdlc.Deframer()
        self.stats = {
            "polls": 0, "dup_i": 0, "unexpected": 0, "foreign": 0,
            "rnr": 0, "retx": 0, "link_resets": 0,
        }

    def close(self):
        self.ser.close()

    def _peer(self, addr: int) -> PeerState:
        if addr not in self._peers:
            self._peers[addr] = PeerState()
        return self._peers[addr]

    @property
    def fcs_errors(self) -> int:
        return self._deframer.fcs_errors

    def _log(self, direction, data):
        if self.debug:
            print(f"{direction} {data.hex(' ')}")

    def _xfer(self, frame: bytes, timeout: float | None = None) -> bytes:
        """Send one frame, collect raw response bytes until idle.

        The one-shot path, used for XID and U-frames (scan, address assign,
        SNRM, DISC). It keeps parse_frames() -- and therefore the FrameError
        on a bad FCS -- because scan() *relies* on that exception as its
        bus-collision signal for unique-ID bisection. The I-frame data path
        uses exchange() and the incremental Deframer instead; the two never
        interleave, so the Deframer is reset whenever this path runs.
        """
        self._deframer = hdlc.Deframer()
        self.ser.reset_input_buffer()
        self._log(">>", frame)
        self.ser.write(frame)
        self.ser.flush()
        deadline = time.monotonic() + (timeout or self.timeout)
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                # frame complete when we have an opening and a closing flag
                if buf.count(hdlc.FLAG) >= 2 and not self.ser.in_waiting:
                    break
        if buf:
            self._log("<<", bytes(buf))
        return bytes(buf)

    def _command(self, addr, ctrl, payload=b"", timeout=None):
        raw = self._xfer(hdlc.build_frame(addr, ctrl, payload), timeout)
        return list(hdlc.parse_frames(raw))

    # --- device scan / address assignment (TS 25.462 4.8.3-4.8.4) ---

    def scan_once(self, uid: bytes = b"", mask: bytes = b"") -> list[Device]:
        """Single scan probe. PL=0 for uid+mask matches every unaddressed device.

        Raises hdlc.FrameError on a garbled reply (collision: several devices
        answered at once) — callers may then bisect the unique-ID space.
        """
        payload = hdlc.xid_encode([(hdlc.PI_UNIQUE_ID, uid), (hdlc.PI_BITMASK, mask)])
        frames = self._command(hdlc.ADDR_BROADCAST, hdlc.CTRL_XID | hdlc.PF, payload)
        found = []
        for _addr, ctrl, pl in frames:
            if ctrl & ~hdlc.PF != hdlc.CTRL_XID:
                continue
            p = hdlc.xid_decode(pl)
            found.append(Device(p.get(hdlc.PI_UNIQUE_ID, b""),
                                p.get(hdlc.PI_DEVICE_TYPE, b"\xff")[0],
                                p.get(hdlc.PI_VENDOR_CODE)))
        return found

    def scan(self, max_depth: int = 24) -> list[Device]:
        """Full scan with binary bisection on collisions."""
        results = {}

        def probe(uid: bytes, mask: bytes, depth: int):
            try:
                for dev in self.scan_once(uid, mask):
                    results[dev.unique_id] = dev
            except hdlc.FrameError:
                if depth >= max_depth:
                    raise AisgError("scan collision could not be resolved")
                # split on the next unmasked bit
                nbytes = len(mask) + 1 if not mask or mask[-1] == 0xFF else len(mask)
                bit = 7 - ((depth) % 8)
                base_uid = uid.ljust(nbytes, b"\x00")
                base_mask = bytearray(mask.ljust(nbytes, b"\x00"))
                base_mask[-1] |= 1 << bit
                for v in (0, 1):
                    u = bytearray(base_uid)
                    if v:
                        u[-1] |= 1 << bit
                    probe(bytes(u), bytes(base_mask), depth + 1)

        probe(b"", b"", 0)
        return list(results.values())

    def assign_address(self, dev: Device, addr: int) -> Device:
        payload = hdlc.xid_encode([
            (hdlc.PI_UNIQUE_ID, dev.unique_id),
            (hdlc.PI_HDLC_ADDR, bytes([addr])),
        ])
        frames = self._command(hdlc.ADDR_BROADCAST, hdlc.CTRL_XID | hdlc.PF, payload)
        for faddr, ctrl, pl in frames:
            if ctrl & ~hdlc.PF == hdlc.CTRL_XID and faddr == addr:
                dev.address = addr
                return dev
        raise AisgError(f"no XID response to address assignment for {dev}")

    # --- connection management ---

    def connect(self, addr: int):
        frames = self._command(addr, hdlc.CTRL_SNRM | hdlc.PF)
        for faddr, ctrl, _pl in frames:
            if faddr == addr and ctrl & ~hdlc.PF == hdlc.CTRL_UA:
                # UA resets sequence state for THIS peer only.
                p = self._peer(addr)
                p.reset()
                p.connected = True
                return
        raise AisgError(f"no UA to SNRM from address 0x{addr:02X}")

    def disconnect(self, addr: int, timeout: float | None = None):
        self._command(addr, hdlc.CTRL_DISC | hdlc.PF, timeout=timeout)
        p = self._peer(addr)
        p.reset()
        p.connected = False

    def probe_address(self, addr: int, disconnect: bool = False) -> bool:
        """True if a secondary answers SNRM at this address.

        This *establishes* a link as a side effect. With disconnect=True the
        link is torn down again, which is what a long-lived session wants: a
        bare probe sweep otherwise leaves every answering secondary believing
        it holds a live link that nobody will ever poll.
        """
        try:
            self.connect(addr)
        except AisgError:
            return False
        if disconnect:
            try:
                self.disconnect(addr)
            except AisgError:
                pass
        return True

    def scan_assigned(self, first: int = 0x01, count: int = 8) -> list[Device]:
        """Find devices that already hold an HDLC address.

        The broadcast device scan only matches *unaddressed* secondaries
        (TS 25.462 4.8.4), so a device addressed by an earlier session is
        invisible to scan() until it is power-cycled. Poll the address range
        we would have assigned and keep whatever answers SNRM. The returned
        Devices carry only an address — identity needs a RETAP
        GetInformation, which is a layer above this one.
        """
        found = []
        for addr in range(first, first + count):
            if self.probe_address(addr):
                dev = Device(b"", None, None)
                dev.address = addr
                found.append(dev)
        return found

    # --- layer 7 exchange (stop-and-wait, window 1, NRM polling) ---

    def _write(self, frame: bytes):
        self._log(">>", frame)
        self.ser.write(frame)
        self.ser.flush()

    def _rr(self, addr: int, vr: int, poll: bool = True) -> bytes:
        """RR with N(R)=vr. poll=False clears P, so the secondary is *not*
        invited to transmit -- use that for the final ack of an exchange,
        otherwise the reply it sends gets discarded and an AlarmIndication
        can be lost."""
        return hdlc.build_frame(addr, 0x01 | (vr << 5) | (hdlc.PF if poll else 0))

    def _read_frames(self, budget: float, idle_gap: float = 0.05,
                     hard_cap: float = 3.0) -> list:
        """Pump: read for up to `budget` seconds, return whole frames.

        Never flushes the input buffer -- anything already on the wire is a
        frame somebody sent us, including a piggybacked AlarmIndication.

        Crucially, the budget is extended while a frame is still arriving.
        A 50-byte response takes ~52 ms at 9600 baud and lands in several
        USB reads, so returning on a fixed budget lets the caller send its
        next poll into the middle of it -- and on a half-duplex RS-485 bus
        that corrupts both directions. Observed for real: an RR emitted
        between two chunks of a GetInformation reply, followed by line
        garbage and a desynchronised link.
        """
        start = time.monotonic()
        end = start + budget
        hard_end = start + max(budget, hard_cap)
        frames = []
        last_rx = 0.0
        while True:
            waiting = self.ser.in_waiting
            chunk = self.ser.read(waiting if waiting else 1)
            now = time.monotonic()
            if chunk:
                self._log("<<", chunk)
                frames += self._deframer.feed(chunk)
                last_rx = now
            if frames:
                return frames
            if now >= hard_end:
                return frames
            if self._deframer.partial and now - last_rx < idle_gap:
                continue        # mid-frame: keep listening, do not transmit
            if now >= end:
                return frames

    def _apply_ack(self, p: PeerState, ctrl: int, sent_ns: int) -> bool:
        """Advance V(S) from the peer's N(R). The single writer of p.vs.

        The old code advanced V(S) in two places -- once from an RR's N(R)
        and again on the response I-frame -- so any procedure slow enough to
        be RR-polled while it ran left V(S) one too high. Verified on the
        wire: after a SetTilt, the next request went out with N(S)=2 while
        the RET's N(R) was still 1. That actuator is lenient and answered
        anyway; a strict one replies FRMR.
        """
        if hdlc.nr_of(ctrl) == ((sent_ns + 1) & 7):
            p.vs = (sent_ns + 1) & 7
            return True
        return False

    def exchange(self, addr: int, message: bytes, timeout: float = 3.0,
                 poll_interval: float = 0.05, router=accept_all,
                 on_poll=None, on_indication=None) -> bytes:
        """One layer-7 request/response, NRM stop-and-wait, window 1.

        `router` classifies each inbound I-frame payload as ACCEPT (it is our
        response), INDICATION (an unsolicited report -- hand to on_indication
        and keep polling) or UNEXPECTED (a stale response to a transaction we
        already abandoned -- ack it so the device frees its buffer, then drop
        it). `timeout` is the total budget.
        """
        p = self._peer(addr)
        ictrl = (p.vs << 1) | (p.vr << 5) | hdlc.PF
        sent_ns = p.vs
        acked = False
        # Set when the peer's N(R) still asks for the frame we sent, i.e. it
        # is telling us explicitly that it never arrived. That -- not silence
        # -- is the only safe trigger to resend.
        missing = False
        polls = 0
        frame = hdlc.build_frame(addr, ictrl, message)
        self._write(frame)
        now = time.monotonic()
        deadline = now + timeout
        retx_at = now + 1.0
        retx_left = 3

        while True:
            for faddr, ctrl, pl in self._read_frames(poll_interval):
                if faddr != addr:
                    self.stats["foreign"] += 1
                    continue
                k = hdlc.kind(ctrl)
                if k != hdlc.U_FRAME and hdlc.nr_of(ctrl) == sent_ns:
                    missing = True

                if k == hdlc.U_FRAME:
                    u = ctrl & ~hdlc.PF
                    if u in (hdlc.CTRL_DM, hdlc.CTRL_FRMR):
                        # Both used to fall through to a timeout. Neither is
                        # recoverable without a fresh SNRM from us.
                        p.reset()
                        p.connected = False
                        self.stats["link_resets"] += 1
                        why = "DM" if u == hdlc.CTRL_DM else f"FRMR {pl.hex(' ')}"
                        raise LinkReset(addr, why)
                    continue

                acked = acked or self._apply_ack(p, ctrl, sent_ns)

                if k in (hdlc.RR, hdlc.RNR):
                    if k == hdlc.RNR:
                        self.stats["rnr"] += 1
                    continue

                if k in (hdlc.REJ, hdlc.SREJ):
                    # Only ever resend a frame the device never acked --
                    # re-sending an acked SetTilt would command a second
                    # physical movement.
                    if not acked and retx_left:
                        retx_left -= 1
                        self.stats["retx"] += 1
                        self._write(frame)
                    continue

                # --- I-frame ---
                ns = hdlc.ns_of(ctrl)
                if ns != p.vr:
                    # Duplicate or out of sequence: discard the payload and
                    # re-ack, or a retransmitted AlarmIndication gets
                    # reported twice.
                    self.stats["dup_i"] += 1
                    self._write(self._rr(addr, p.vr))
                    continue
                p.vr = (ns + 1) & 7
                verdict = router(pl)
                if verdict == ACCEPT:
                    self._write(self._rr(addr, p.vr, poll=False))
                    if not acked:
                        p.vs = (sent_ns + 1) & 7
                    return pl
                self._write(self._rr(addr, p.vr))
                if verdict == INDICATION:
                    if on_indication:
                        on_indication(pl)
                    # Bus time went to someone else's traffic; give our own
                    # response a little longer, but bounded.
                    deadline = min(deadline + poll_interval * 4,
                                   time.monotonic() + timeout)
                else:
                    self.stats["unexpected"] += 1

            now = time.monotonic()
            if not acked and missing and now > retx_at and retx_left:
                # The old `retries` loop only ever extended the deadline and
                # sent more RR polls -- the I-frame itself was never resent,
                # so a single lost command was unrecoverable.
                #
                # But `not acked` is not evidence of loss: this RET answers
                # some procedures with the response I-frame and no
                # intervening RR, and if that response is lost we never see
                # the ack even though the device acted on the command.
                # Resending then earns a FRMR -- observed for real as
                # "FRMR 10 30 04", the device's V(R) already advanced past
                # the frame we were resending. So resend only when its N(R)
                # explicitly still asks for our frame; silence just keeps
                # polling until the budget runs out.
                retx_left -= 1
                retx_at = now + 1.0
                missing = False
                self.stats["retx"] += 1
                self._write(frame)
                continue
            if now > deadline:
                raise AisgTimeout(addr, sent_ns, acked, polls)
            polls += 1
            self.stats["polls"] += 1
            if on_poll:
                on_poll(polls, acked)
            time.sleep(poll_interval)
            self._write(self._rr(addr, p.vr))

    def request(self, addr: int, message: bytes, timeout: float = 3.0,
                retries: int = 3) -> bytes:
        """Send a RETAP/TMAAP message in an I-frame; poll until the response
        I-frame arrives; ACK it. Returns the layer-7 response bytes.

        Compatibility wrapper over exchange(). The old `retries` only extended
        the deadline rather than retransmitting, so `timeout * (retries + 1)`
        reproduces its wall-clock budget exactly; real, ack-aware
        retransmission now happens inside exchange().
        """
        try:
            return self.exchange(addr, message, timeout=timeout * (retries + 1))
        except AisgTimeout:
            raise AisgError(f"no layer-7 response from 0x{addr:02X}")
