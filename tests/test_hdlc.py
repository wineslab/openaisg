"""Checks against the worked examples in 3GPP TS 25.462 Annex D."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from aisg import hdlc


def test_fcs_reference_vector():
    # CRC-16/X.25 check value for "123456789"
    assert hdlc.fcs16(b"123456789") == 0x906E


def test_stuff_roundtrip():
    data = bytes(range(256))
    assert hdlc.unstuff(hdlc.stuff(data)) == data
    assert hdlc.FLAG not in hdlc.stuff(data)


def test_frame_roundtrip():
    frame = hdlc.build_frame(0x17, 0xBF, b"\x81\xf0\x00")
    [(addr, ctrl, pl)] = list(hdlc.parse_frames(frame))
    assert (addr, ctrl, pl) == (0x17, 0xBF, b"\x81\xf0\x00")


def test_annex_d_address_assignment_command():
    # TS 25.462 Table D.1: ADDR=0xFF CTRL=0xBF FI=0x81 GI=0xF0 GL=0x10
    # PI=1 PL=7 uid, PI=2 PL=1 0x17, PI=6 PL=2 "XY"
    uid = bytes([0x58, 0x59, 0x7B, 0x20, 0x41, 0x42, 0x43])
    payload = hdlc.xid_encode([
        (hdlc.PI_UNIQUE_ID, uid),
        (hdlc.PI_HDLC_ADDR, b"\x17"),
        (hdlc.PI_VENDOR_CODE, b"XY"),
    ])
    expected = bytes([0x81, 0xF0, 0x10,
                      0x01, 0x07]) + uid + bytes([0x02, 0x01, 0x17,
                      0x06, 0x02]) + b"XY"
    assert payload == expected
    # and it must survive the framing layer
    frame = hdlc.build_frame(0xFF, hdlc.CTRL_XID | hdlc.PF, payload)
    [(addr, ctrl, pl)] = list(hdlc.parse_frames(frame))
    assert addr == 0xFF and ctrl == 0xBF
    params = hdlc.xid_decode(pl)
    assert params[hdlc.PI_UNIQUE_ID] == uid
    assert params[hdlc.PI_HDLC_ADDR] == b"\x17"
    assert params[hdlc.PI_VENDOR_CODE] == b"XY"


def test_annex_d_response_decode():
    # Table D.2: response from the secondary
    uid = bytes([0x58, 0x59, 0x7B, 0x20, 0x41, 0x42, 0x43])
    payload = bytes([0x81, 0xF0, 0x0C, 0x01, 0x07]) + uid + bytes([0x04, 0x01, 0x01])
    params = hdlc.xid_decode(payload)
    assert params[hdlc.PI_UNIQUE_ID] == uid
    assert params[hdlc.PI_DEVICE_TYPE] == b"\x01"


def test_bad_fcs_raises():
    frame = bytearray(hdlc.build_frame(0x01, 0xBF, b"\x81\xf0\x00"))
    frame[3] ^= 0xFF
    try:
        list(hdlc.parse_frames(bytes(frame)))
    except hdlc.FrameError:
        return
    raise AssertionError("FrameError not raised")
