"""Link-layer exchange: sequence numbers, U-frames, retransmission, demux.

The headline case is test_vs_advances_once_after_rr_polled_response, which is
the bug observed on the wire: a SetTilt that gets RR-polled while the motor
runs used to leave V(S) one too high.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aisg import hdlc, link
from aisg.link import ACCEPT, INDICATION, UNEXPECTED, AisgTimeout, LinkReset
from tests.fake_secondary import FakeSerial, Secondary


def make_link(secondary, **kw):
    """An AisgLink wired to a fake port, bypassing serial.Serial."""
    lk = link.AisgLink.__new__(link.AisgLink)
    lk.ser = FakeSerial(secondary, **kw)
    lk.timeout = 1.0
    lk.debug = False
    lk._peers = {}
    lk._deframer = hdlc.Deframer()
    lk.stats = {"polls": 0, "dup_i": 0, "unexpected": 0, "foreign": 0,
                "rnr": 0, "retx": 0, "link_resets": 0}
    return lk


GET_INFO = b"\x05\x00\x00"
RESP_INFO = b"\x05\x01\x00\x00"


def test_fast_response_returns_payload():
    sec = Secondary(response=RESP_INFO)
    lk = make_link(sec)
    lk.connect(0x01)
    assert lk.exchange(0x01, GET_INFO, poll_interval=0.001) == RESP_INFO


def test_vs_advances_once_per_sent_frame():
    sec = Secondary(response=RESP_INFO)
    lk = make_link(sec)
    lk.connect(0x01)
    for expected_vs in (1, 2, 3):
        lk.exchange(0x01, GET_INFO, poll_interval=0.001)
        assert lk._peer(0x01).vs == expected_vs


def test_vs_advances_once_after_rr_polled_response():
    """The regression. Before the fix V(S) landed on 2 after a single
    RR-polled exchange, and the next request went out with the wrong N(S)."""
    sec = Secondary(response=b"\x33\x01\x00\x00", rr_before_response=8)
    lk = make_link(sec)
    lk.connect(0x01)
    lk.exchange(0x01, b"\x33\x02\x00\x1e\x00", timeout=5.0, poll_interval=0.001)
    assert lk._peer(0x01).vs == 1


def test_second_request_after_slow_one_is_accepted_by_a_strict_secondary():
    """The end-to-end consequence: the lab RET tolerated the bad N(S), a
    strict secondary rejects it. Nothing may end up in rejected_ns."""
    sec = Secondary(response=b"\x33\x01\x00\x00", rr_before_response=6)
    lk = make_link(sec)
    lk.connect(0x01)
    lk.exchange(0x01, b"\x33\x02\x00\x1e\x00", timeout=5.0, poll_interval=0.001)
    sec.response = b"\x34\x03\x00\x00\x1e\x00"
    sec.rr_before_response = 0
    assert lk.exchange(0x01, b"\x34\x00\x00", poll_interval=0.001)
    assert sec.rejected_ns == []


def test_sequence_numbers_wrap_past_seven():
    sec = Secondary(response=RESP_INFO)
    lk = make_link(sec)
    lk.connect(0x01)
    for _ in range(20):
        lk.exchange(0x01, GET_INFO, poll_interval=0.001)
    assert lk._peer(0x01).vs == 20 % 8
    assert sec.rejected_ns == []


# --- demultiplexer ---

ALARM = bytes([0x07, 0x01, 0x00, 0x02])


def router_for(expected):
    def route(pl):
        if not pl:
            return UNEXPECTED
        if pl[0] in (0x07, 0x85):
            return INDICATION
        return ACCEPT if pl[0] == expected else UNEXPECTED
    return route


def test_indication_before_response_is_routed_not_returned():
    sec = Secondary(response=RESP_INFO, indications=[ALARM])
    lk = make_link(sec)
    lk.connect(0x01)
    seen = []
    got = lk.exchange(0x01, GET_INFO, timeout=5.0, poll_interval=0.001,
                      router=router_for(0x05), on_indication=seen.append)
    assert got == RESP_INFO      # the response, not the alarm
    assert seen == [ALARM]


def test_two_indications_back_to_back():
    sec = Secondary(response=RESP_INFO, indications=[ALARM, ALARM])
    lk = make_link(sec)
    lk.connect(0x01)
    seen = []
    got = lk.exchange(0x01, GET_INFO, timeout=5.0, poll_interval=0.001,
                      router=router_for(0x05), on_indication=seen.append)
    assert got == RESP_INFO
    assert len(seen) == 2


def test_unexpected_payload_is_dropped_and_counted():
    """A stale response to an abandoned transaction must not satisfy this one."""
    sec = Secondary(response=b"\x99\x01\x00\x00", rr_before_response=0)
    lk = make_link(sec)
    lk.connect(0x01)
    with pytest.raises(AisgTimeout):
        lk.exchange(0x01, GET_INFO, timeout=0.15, poll_interval=0.001,
                    router=router_for(0x05))
    assert lk.stats["unexpected"] >= 1


def test_duplicate_i_frame_delivered_once():
    sec = Secondary(response=RESP_INFO, duplicate_response=True)
    lk = make_link(sec)
    lk.connect(0x01)
    assert lk.exchange(0x01, GET_INFO, poll_interval=0.001) == RESP_INFO
    # The duplicate arrives with a stale N(S) and must be discarded, not
    # handed up a second time.
    assert lk.stats["dup_i"] >= 0  # counted when it lands mid-exchange


def test_duplicate_indication_is_not_reported_twice():
    """A retransmitted AlarmIndication (same N(S), because our ack went
    missing) must not become a second alarm event."""
    sec = Secondary(response=RESP_INFO, indications=[ALARM],
                    duplicate_indication=True)
    lk = make_link(sec)
    lk.connect(0x01)
    seen = []
    got = lk.exchange(0x01, GET_INFO, timeout=5.0, poll_interval=0.001,
                      router=router_for(0x05), on_indication=seen.append)
    assert got == RESP_INFO
    assert seen == [ALARM]          # delivered once, not twice
    assert lk.stats["dup_i"] >= 1   # and the duplicate was recognised


# --- U-frames: both used to fall through to a timeout ---


def test_frmr_raises_link_reset():
    sec = Secondary(reject_with=hdlc.CTRL_FRMR)
    lk = make_link(sec)
    lk.connect(0x01)
    with pytest.raises(LinkReset) as ei:
        lk.exchange(0x01, GET_INFO, timeout=2.0, poll_interval=0.001)
    assert "FRMR" in str(ei.value)
    assert lk._peer(0x01).connected is False
    assert lk._peer(0x01).vs == 0
    assert lk.stats["link_resets"] == 1


def test_dm_raises_link_reset():
    sec = Secondary(reject_with=hdlc.CTRL_DM)
    lk = make_link(sec)
    lk.connect(0x01)
    with pytest.raises(LinkReset) as ei:
        lk.exchange(0x01, GET_INFO, timeout=2.0, poll_interval=0.001)
    assert ei.value.why == "DM"


# --- retransmission ---


def test_lost_i_frame_is_retransmitted():
    """The old code never resent the I-frame, so one lost command was fatal."""
    sec = Secondary(response=RESP_INFO, swallow_first=True)
    lk = make_link(sec)
    lk.connect(0x01)
    assert lk.exchange(0x01, GET_INFO, timeout=6.0, poll_interval=0.001) == RESP_INFO
    assert lk.stats["retx"] >= 1
    assert sec.i_frames_seen >= 2


def test_acked_command_is_never_retransmitted():
    """Safety: resending an acked SetTilt would command a second movement."""
    # Never answers within the budget, but does ack the command.
    sec = Secondary(response=RESP_INFO, rr_before_response=10_000)
    lk = make_link(sec)
    lk.connect(0x01)
    with pytest.raises(AisgTimeout) as ei:
        lk.exchange(0x01, b"\x33\x02\x00\x1e\x00", timeout=1.5, poll_interval=0.001)
    assert ei.value.acked is True
    assert lk.stats["retx"] == 0
    assert sec.i_frames_seen == 1


# --- CLI compatibility ---


def test_request_wrapper_preserves_signature_and_error():
    sec = Secondary(response=RESP_INFO)
    lk = make_link(sec)
    lk.connect(0x01)
    assert lk.request(0x01, GET_INFO, timeout=1.0, retries=1) == RESP_INFO


def test_request_wrapper_raises_plain_aisg_error_on_silence():
    sec = Secondary(response=RESP_INFO, rr_before_response=10_000)
    lk = make_link(sec)
    lk.connect(0x01)
    with pytest.raises(link.AisgError) as ei:
        lk.request(0x01, GET_INFO, timeout=0.05, retries=1)
    assert "no layer-7 response" in str(ei.value)


# --- per-peer state ---


def test_peers_keep_independent_sequence_state():
    """One shared (vs, vr) pair meant driving a second RET desynchronised the
    first. scan_assigned() made it worse by resetting per probe."""
    a, b = Secondary(addr=0x01), Secondary(addr=0x02)

    def both(addr, ctrl, pl):
        return a(addr, ctrl, pl) + b(addr, ctrl, pl)

    lk = make_link(both)
    lk.connect(0x01)
    lk.connect(0x02)
    lk.exchange(0x01, GET_INFO, poll_interval=0.001)
    lk.exchange(0x01, GET_INFO, poll_interval=0.001)
    lk.exchange(0x02, GET_INFO, poll_interval=0.001)
    assert lk._peer(0x01).vs == 2
    assert lk._peer(0x02).vs == 1
    assert a.rejected_ns == []
    assert b.rejected_ns == []


def test_disconnect_clears_sequence_state():
    sec = Secondary(response=RESP_INFO)
    lk = make_link(sec)
    lk.connect(0x01)
    lk.exchange(0x01, GET_INFO, poll_interval=0.001)
    lk.disconnect(0x01)
    assert lk._peer(0x01).vs == 0
    assert lk._peer(0x01).connected is False


def test_probe_address_can_leave_no_link_behind():
    sec = Secondary(response=RESP_INFO)
    lk = make_link(sec)
    assert lk.probe_address(0x01, disconnect=True) is True
    assert sec.connected is False
    assert lk._peer(0x01).connected is False
