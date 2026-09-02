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

    One-shot: the whole stream must be present. For a long-lived session use
    Deframer, which resynchronises instead of raising.
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


class Deframer:
    """Incremental HDLC deframer for a continuously read stream.

    parse_frames() is fine for one exchange but wrong for a session: it needs
    the entire stream at once, and it raises on the first bad FCS, which
    discards every good frame in the same buffer. Here a corrupt frame is
    counted and skipped, and a partial tail is kept for the next feed() --
    so noise costs one frame instead of the exchange.
    """

    def __init__(self, max_frame: int = 512):
        self._buf = bytearray()
        self._max = max_frame
        self.fcs_errors = 0
        self.overruns = 0

    @property
    def partial(self) -> bool:
        """True when a frame is part-way through arriving.

        The caller must not transmit while this holds: the bus is
        half-duplex. Trailing flag bytes do not count -- a closing flag is
        deliberately left in the buffer so back-to-back frames can share it.
        """
        buf = self._buf
        i = 0
        while i < len(buf) and buf[i] == FLAG:
            i += 1
        return i < len(buf)

    def feed(self, data: bytes) -> list:
        """Consume bytes, return [(addr, ctrl, payload), ...] for whole frames."""
        self._buf += data
        out = []
        while True:
            i = self._buf.find(FLAG)
            if i < 0:
                # No flag at all: nothing framed yet. Cap the buffer so line
                # noise on an idle bus cannot grow it without bound.
                if len(self._buf) > self._max:
                    del self._buf[:-1]
                    self.overruns += 1
                return out
            j = self._buf.find(FLAG, i + 1)
            if j < 0:
                del self._buf[:i]  # drop leading junk, keep the partial frame
                if len(self._buf) > self._max:
                    del self._buf[:1]
                    self.overruns += 1
                return out
            chunk = bytes(self._buf[i + 1:j])
            # Leave the closing flag in place: back-to-back frames may share it.
            del self._buf[:j]
            if not chunk:
                # Two adjacent flags: the del above already consumed the
                # first one, and the second opens the next frame. Deleting
                # again here would eat it.
                continue
            body = unstuff(chunk)
            if len(body) < 4:
                continue
            if fcs16(body[:-2]) == body[-2] | (body[-1] << 8):
                out.append((body[0], body[1], body[2:-2]))
            else:
                self.fcs_errors += 1


# --- control-field classification ---
#
# The old inline tests (`ctrl & 1 == 0` for I, `ctrl & 0x0F == 0x01` for RR)
# silently ignore every other frame type: FRMR (0x87) and DM (0x0F) match
# neither test and used to fall through to a timeout, and RNR/REJ were read
# as unknown. A long-lived link has to act on all of them.

I_FRAME = "I"
RR = "RR"
RNR = "RNR"
REJ = "REJ"
SREJ = "SREJ"
U_FRAME = "U"

_S_TYPES = {0: RR, 1: RNR, 2: REJ, 3: SREJ}


def kind(ctrl: int) -> str:
    """Classify a control byte as an I-, S- or U-frame."""
    if not ctrl & 0x01:
        return I_FRAME
    if ctrl & 0x03 == 0x01:
        return _S_TYPES[(ctrl >> 2) & 0x03]
    return U_FRAME


def ns_of(ctrl: int) -> int:
    """Send sequence number N(S) of an I-frame."""
    return (ctrl >> 1) & 7


def nr_of(ctrl: int) -> int:
    """Receive sequence number N(R) of an I- or S-frame."""
    return (ctrl >> 5) & 7


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
