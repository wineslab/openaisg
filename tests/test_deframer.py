"""Deframer and control-field classification.

These are the cases a one-shot parse never meets but a 24/7 session does:
frames split across reads, frames sharing a flag, junk on the line, and a
bad FCS in the middle of an otherwise good buffer.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aisg import hdlc


def frame(addr, ctrl, payload=b""):
    return hdlc.build_frame(addr, ctrl, payload)


def test_single_frame():
    d = hdlc.Deframer()
    assert d.feed(frame(0x01, 0x31)) == [(0x01, 0x31, b"")]


def test_frame_split_across_feeds():
    d = hdlc.Deframer()
    raw = frame(0x01, 0x30, b"\x05\x01\x00\x00")
    for b in raw[:-1]:
        assert d.feed(bytes([b])) == []
    assert d.feed(raw[-1:]) == [(0x01, 0x30, b"\x05\x01\x00\x00")]


def test_two_frames_one_feed():
    d = hdlc.Deframer()
    out = d.feed(frame(0x01, 0x31) + frame(0x01, 0x30, b"\x33\x01\x00\x00"))
    assert out == [(0x01, 0x31, b""), (0x01, 0x30, b"\x33\x01\x00\x00")]


def test_frames_sharing_a_flag():
    """`...FCS 7e 01 ctrl...` -- one flag closing and opening at once."""
    d = hdlc.Deframer()
    a = frame(0x01, 0x31)
    b = frame(0x02, 0x31)
    shared = a + b[1:]  # drop b's opening flag
    assert d.feed(shared) == [(0x01, 0x31, b""), (0x02, 0x31, b"")]


def test_leading_junk_is_discarded():
    d = hdlc.Deframer()
    assert d.feed(b"\x00\xff\xaa" + frame(0x01, 0x31)) == [(0x01, 0x31, b"")]


def test_flag_fill_between_frames():
    d = hdlc.Deframer()
    raw = frame(0x01, 0x31) + b"\x7e\x7e\x7e" + frame(0x02, 0x31)
    assert d.feed(raw) == [(0x01, 0x31, b""), (0x02, 0x31, b"")]


def test_bad_fcs_is_counted_not_raised_and_later_frames_survive():
    """The regression that matters: parse_frames() would raise here and the
    caller's list() would discard the good frame that follows."""
    d = hdlc.Deframer()
    bad = bytearray(frame(0x01, 0x31))
    # Flip one bit in the FCS. A whole-byte XOR could synthesise a 0x7e/0x7d
    # and break the framing instead of just the checksum, which is a
    # different test.
    bad[-2] ^= 0x01
    out = d.feed(bytes(bad) + frame(0x02, 0x31))
    assert out == [(0x02, 0x31, b"")]
    assert d.fcs_errors == 1


def test_escaped_bytes_round_trip():
    """0x7d in the payload must survive stuffing (the real scan reply had an
    FCS byte of 0x7d, which is how this path first got exercised)."""
    d = hdlc.Deframer()
    payload = b"\x7e\x7d\x00\x7e"
    assert d.feed(frame(0x01, 0x30, payload)) == [(0x01, 0x30, payload)]


def test_overrun_is_bounded():
    d = hdlc.Deframer(max_frame=16)
    d.feed(b"\x00" * 64)
    assert d.overruns > 0
    assert len(d._buf) <= 16


def test_partial_tail_survives_a_bad_frame():
    d = hdlc.Deframer()
    good = frame(0x01, 0x31)
    d.feed(b"\x7e\x01")  # opening flag plus a partial body
    assert d.feed(b"\x7e" + good[1:]) == [(0x01, 0x31, b"")]


# --- control-field classification ---


def test_kind_i_frame():
    assert hdlc.kind(0x10) == hdlc.I_FRAME
    assert hdlc.kind(0x30) == hdlc.I_FRAME
    assert hdlc.kind(0x54) == hdlc.I_FRAME


def test_kind_supervisory():
    assert hdlc.kind(0x01) == hdlc.RR
    assert hdlc.kind(0x31) == hdlc.RR
    assert hdlc.kind(0x05) == hdlc.RNR
    assert hdlc.kind(0x09) == hdlc.REJ


def test_kind_unnumbered_covers_frmr_and_dm():
    """Both used to fall through every branch and cost a full timeout."""
    assert hdlc.kind(hdlc.CTRL_FRMR) == hdlc.U_FRAME
    assert hdlc.kind(hdlc.CTRL_DM) == hdlc.U_FRAME
    assert hdlc.kind(hdlc.CTRL_SNRM) == hdlc.U_FRAME
    assert hdlc.kind(hdlc.CTRL_UA) == hdlc.U_FRAME
    assert hdlc.kind(hdlc.CTRL_XID) == hdlc.U_FRAME


def test_sequence_number_extraction():
    # observed on the wire: 0x30 = I, N(S)=0, N(R)=1
    assert hdlc.ns_of(0x30) == 0
    assert hdlc.nr_of(0x30) == 1
    # 0x52 = I, N(S)=1, N(R)=2
    assert hdlc.ns_of(0x52) == 1
    assert hdlc.nr_of(0x52) == 2
    # 0x31 = RR, N(R)=1
    assert hdlc.nr_of(0x31) == 1
