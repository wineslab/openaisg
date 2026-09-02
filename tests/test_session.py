"""Session layer: demux routing, alarm edges, state machine, port handoff."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openaisg import hdlc, link, retap, session
from openaisg.link import ACCEPT, INDICATION, UNEXPECTED
from openaisg.session import (AlarmTracker, LinkState, MonitorMode, RetapRouter,
                          SerialTransport, SerialWorker, SessionError)
from tests.fake_secondary import FakeSerial, Secondary
from tests.test_exchange import make_link


# --- router ---

def test_router_accepts_matching_procedure_code():
    assert RetapRouter(0x05)(b"\x05\x01\x00\x00") == ACCEPT


def test_router_flags_indications_regardless_of_expectation():
    assert RetapRouter(0x05)(bytes([retap.ALARM_INDICATION, 0, 0])) == INDICATION
    assert RetapRouter(0x34)(bytes([retap.ANT_ALARM_INDICATION, 0, 0])) == INDICATION


def test_router_rejects_stale_response():
    """A late GetTilt answer must not satisfy a GetInformation."""
    assert RetapRouter(0x05)(b"\x34\x03\x00\x00") == UNEXPECTED


def test_router_handles_empty_payload():
    assert RetapRouter(0x05)(b"") == UNEXPECTED


# --- transport keeps Ret unchanged ---

def test_ret_works_through_the_transport_unmodified():
    sec = Secondary()
    lk = make_link(sec)
    lk.connect(0x01)
    ret = retap.Ret(SerialTransport(lk), 0x01)
    info = ret.get_information()
    assert info["product_number"] == "RET21-AS155D"


def test_transport_routes_indications_away_from_the_response():
    alarm = bytes([retap.ALARM_INDICATION, 0x01, 0x00, 0x02])
    sec = Secondary(indications=[alarm])
    lk = make_link(sec)
    lk.connect(0x01)
    seen = []
    ret = retap.Ret(SerialTransport(lk, on_indication=seen.append), 0x01)
    assert ret.get_tilt() == 3.0
    assert seen == [alarm]


# --- alarm tracker ---

def test_alarm_tracker_emits_raised_and_cleared_edges():
    t = AlarmTracker()
    assert t.snapshot(["OK"]) == []
    evs = t.snapshot(["MotorJam"])
    assert [(e["action"], e["name"]) for e in evs] == [("raised", "MotorJam")]
    evs = t.snapshot([])
    assert [(e["action"], e["name"]) for e in evs] == [("cleared", "MotorJam")]


def test_alarm_tracker_is_idempotent():
    t = AlarmTracker()
    t.snapshot(["MotorJam"])
    assert t.snapshot(["MotorJam"]) == []


def test_alarm_tracker_baseline_suppresses_events():
    """A reconnect must not replay already-active alarms as fresh ones."""
    t = AlarmTracker()
    assert t.snapshot(["ActuatorJam"], emit=False) == []
    assert t.active == {"ActuatorJam"}
    assert t.snapshot(["ActuatorJam"]) == []


def test_alarm_tracker_merges_an_indication_with_the_known_set():
    t = AlarmTracker()
    t.snapshot(["MotorJam"], emit=False)
    evs = t.indicate(bytes([retap.ALARM_INDICATION, 0x02, 0x00, 0x00, 0x03]))
    assert [e["name"] for e in evs] == ["ActuatorJam"]
    assert t.active == {"MotorJam", "ActuatorJam"}


def test_alarm_tracker_drops_ok_sentinel():
    t = AlarmTracker()
    t.snapshot(["OK", "MotorJam"])
    assert t.active == {"MotorJam"}


# --- worker ---

def make_worker(secondary, monkeypatch, **kw):
    """A SerialWorker whose AisgLink is backed by the fake secondary."""
    def fake_link(port, baud=9600, dtr=True, debug=False, **_):
        return make_link(secondary)

    monkeypatch.setattr(session, "AisgLink", fake_link)
    kw.setdefault("monitor", MonitorMode.OFF)
    return SerialWorker("/dev/fake", **kw)


def drive(w, seconds=2.0, until=None):
    """Run the worker until `until` is true or the budget expires."""
    if not w.is_alive():
        w.start()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until and until():
            return True
        time.sleep(0.01)
    return until() if until else False


def test_worker_connects_and_serves_a_read(monkeypatch):
    sec = Secondary()
    w = make_worker(sec, monkeypatch)
    try:
        fut = w.submit("get_tilt")
        assert drive(w, 5.0, lambda: fut.done())
        assert fut.result()["tilt"] == 3.0
        assert w.snapshot()["state"] == LinkState.CONNECTED.value
    finally:
        w.stop()
        w.join(timeout=5)


def test_snapshot_never_blocks_on_the_queue(monkeypatch):
    sec = Secondary()
    w = make_worker(sec, monkeypatch)
    try:
        w.start()
        # No command submitted, no link yet: snapshot must still answer.
        snap = w.snapshot()
        assert "state" in snap and "heartbeat_age" in snap
        assert snap["queue_depth"] == 0
    finally:
        w.stop()
        w.join(timeout=5)


def test_unknown_command_is_rejected_before_the_bus(monkeypatch):
    sec = Secondary()
    w = make_worker(sec, monkeypatch)
    with pytest.raises(ValueError):
        w.submit("rm_minus_rf")
    w.stop()


def test_no_devices_fails_fast_instead_of_hanging(monkeypatch):
    """A silent bus must produce an error, not a parked request."""
    def silent(addr, ctrl, pl):
        return []

    w = make_worker(silent, monkeypatch)
    try:
        fut = w.submit("get_tilt", link_wait=0.5)
        assert drive(w, 8.0, lambda: fut.done())
        with pytest.raises(SessionError):
            fut.result()
    finally:
        w.stop()
        w.join(timeout=5)


def test_lease_releases_the_port_and_blocks_reopen(monkeypatch):
    sec = Secondary()
    w = make_worker(sec, monkeypatch)
    try:
        fut = w.submit("get_tilt")
        assert drive(w, 5.0, lambda: fut.done())
        w.lease(30)
        assert drive(w, 3.0, lambda: w.snapshot()["state"] == LinkState.LEASED.value)
        snap = w.snapshot()
        assert snap["leased_for"] > 0
        fut2 = w.submit("get_tilt", link_wait=0.3)
        assert drive(w, 5.0, lambda: fut2.done())
        with pytest.raises(SessionError):
            fut2.result()
    finally:
        w.stop()
        w.join(timeout=5)


def test_unlease_restores_service(monkeypatch):
    sec = Secondary()
    w = make_worker(sec, monkeypatch)
    try:
        w.lease(60)
        w.start()
        time.sleep(0.3)
        w.unlease()
        fut = w.submit("get_tilt")
        end = time.monotonic() + 6
        while not fut.done() and time.monotonic() < end:
            time.sleep(0.01)
        assert fut.result()["tilt"] == 3.0
    finally:
        w.stop()
        w.join(timeout=5)


def test_subscribe_rejection_falls_back_to_polling(monkeypatch):
    """AlarmSubscribe is unverified on the real RET, so the fallback is the
    load-bearing path."""
    sec = Secondary()

    def responder(addr, ctrl, pl):
        if hdlc.kind(ctrl) == hdlc.I_FRAME and pl and pl[0] == retap.ALARM_SUBSCRIBE:
            # rc 0x19 = UnknownProcedure
            sec.pending = bytes([retap.ALARM_SUBSCRIBE, 0x01, 0x00, 0x19])
            sec._owed_rr = 0
            sec.vr = (hdlc.ns_of(ctrl) + 1) & 7
            sec.i_frames_seen += 1
            return sec._deliver()
        return sec(addr, ctrl, pl)

    w = make_worker(responder, monkeypatch)
    try:
        fut = w.submit("get_tilt")
        assert drive(w, 6.0, lambda: fut.done())
        assert w.snapshot()["alarm_mode"] == "polled"
        assert w._subscribe_unsupported is True
    finally:
        w.stop()
        w.join(timeout=5)


def test_events_are_emitted_for_state_and_status(monkeypatch):
    sec = Secondary()
    events = []
    w = make_worker(sec, monkeypatch, emit=events.append)
    try:
        fut = w.submit("get_tilt")
        assert drive(w, 6.0, lambda: fut.done())
        kinds = {e["type"] for e in events}
        assert "link" in kinds
        assert "status" in kinds
        assert any(e.get("state") == LinkState.CONNECTED.value for e in events)
    finally:
        w.stop()
        w.join(timeout=5)


def test_set_tilt_records_target_and_reads_back(monkeypatch):
    sec = Secondary(response=b"\x33\x01\x00\x00", rr_before_response=3)

    def responder(addr, ctrl, pl):
        if hdlc.kind(ctrl) == hdlc.I_FRAME and pl and pl[0] == retap.GET_TILT:
            sec.response = b"\x34\x03\x00\x00\x14\x00"   # 2.0 deg
            sec.rr_before_response = 0
        elif hdlc.kind(ctrl) == hdlc.I_FRAME and pl and pl[0] == retap.SET_TILT:
            sec.response = b"\x33\x01\x00\x00"
        return sec(addr, ctrl, pl)

    w = make_worker(responder, monkeypatch)
    try:
        fut = w.submit("set_tilt", {"degrees": 2.0})
        assert drive(w, 10.0, lambda: fut.done())
        assert fut.result()["tilt"] == 2.0
        assert w.snapshot()["target_tilt"] == 2.0
    finally:
        w.stop()
        w.join(timeout=5)
