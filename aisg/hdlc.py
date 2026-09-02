"""HDLC framing for AISG v2.0 / 3GPP TS 25.462 (async, ISO/IEC 13239).

Frame: 0x7E | ADDR | CTRL | [payload] | FCS(2, little-endian) | 0x7E
Byte stuffing: 0x7E -> 0x7D 0x5E, 0x7D -> 0x7D 0x5D (escape XOR 0x20).
FCS: CRC-16/X.25 (reflected 0x1021, init 0xFFFF, xorout 0xFFFF).
"""

FLAG = 0x7E
ESC = 0x7D
ESC_XOR = 0x20

ADDR_BROADCAST = 0xFF
ADDR_NO_DEVICE = 0x00

# U-frame control bytes (P/F bit = 0x10)
CTRL_SNRM = 0x83
CTRL_XID = 0xAF
CTRL_UA = 0x63
CTRL_DISC = 0x43
CTRL_DM = 0x0F
CTRL_FRMR = 0x87
PF = 0x10

_crc_table = []
for _b in range(256):
    _c = _b
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _crc_table.append(_c)


def fcs16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _crc_table[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFF


def stuff(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (FLAG, ESC):
            out.append(ESC)
            out.append(b ^ ESC_XOR)
        else:
            out.append(b)
    return bytes(out)


def unstuff(data: bytes) -> bytes:
    out = bytearray()
    esc = False
    for b in data:
        if esc:
            out.append(b ^ ESC_XOR)
            esc = False
        elif b == ESC:
            esc = True
        else:
            out.append(b)
    return bytes(out)


def build_frame(addr: int, ctrl: int, payload: bytes = b"") -> bytes:
    body = bytes([addr, ctrl]) + payload
    crc = fcs16(body)
    body += bytes([crc & 0xFF, crc >> 8])
    return bytes([FLAG]) + stuff(body) + bytes([FLAG])


class FrameError(Exception):
    pass


def parse_frames(raw: bytes):
    """Yield (addr, ctrl, payload) for each valid frame in a raw byte stream.

    Raises FrameError on an FCS mismatch (useful as a bus-collision signal
    during device scans), after yielding any earlier valid frames.
    """
    for chunk in raw.split(bytes([FLAG])):
        if not chunk:
            continue
        body = unstuff(chunk)
        if len(body) < 4:
            continue
        if fcs16(body[:-2]) != body[-2] | (body[-1] << 8):
            raise FrameError(f"bad FCS in frame: {body.hex(' ')}")
        yield body[0], body[1], body[2:-2]


# --- XID parameter encoding (FI=0x81, GI=0xF0 user-defined set) ---

XID_FI = 0x81
XID_GI = 0xF0

PI_UNIQUE_ID = 1
PI_HDLC_ADDR = 2
PI_BITMASK = 3
PI_DEVICE_TYPE = 4
PI_PROTOCOL_VERSION = 5
PI_VENDOR_CODE = 6
PI_RESET_DEVICE = 7


def xid_encode(params: list) -> bytes:
    """params: list of (pi, bytes) tuples, order preserved."""
    field = bytearray()
    for pi, pv in params:
        field += bytes([pi, len(pv)]) + pv
    return bytes([XID_FI, XID_GI, len(field)]) + bytes(field)


def xid_decode(payload: bytes) -> dict:
    if len(payload) < 3 or payload[0] != XID_FI or payload[1] != XID_GI:
        raise FrameError(f"not an AISG XID payload: {payload.hex(' ')}")
    gl = payload[2]
    field = payload[3 : 3 + gl]
    params = {}
    i = 0
    while i + 2 <= len(field):
        pi, pl = field[i], field[i + 1]
        params[pi] = field[i + 2 : i + 2 + pl]
        i += 2 + pl
    return params
