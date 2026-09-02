"""RETAP layer-7 procedures per 3GPP TS 25.466 (AISG v2.0).

Message: procedure code (1 octet) | number of data octets (2, little-endian) | data.
Multi-octet integers are little-endian; tilt values are int16 in 0.1-degree units.
"""

import struct

from .link import AisgError

# Common procedure set
RESET_SOFTWARE = 0x03
GET_ALARM_STATUS = 0x04
GET_INFORMATION = 0x05
CLEAR_ACTIVE_ALARMS = 0x06
ALARM_INDICATION = 0x07
SELF_TEST = 0x0A
SET_DEVICE_DATA = 0x0E
GET_DEVICE_DATA = 0x0F
READ_USER_DATA = 0x10
WRITE_USER_DATA = 0x11
ALARM_SUBSCRIBE = 0x12
# Single-antenna RET
CALIBRATE = 0x31
SEND_CONFIG_DATA = 0x32
SET_TILT = 0x33
GET_TILT = 0x34
# Software download
DOWNLOAD_START = 0x40
DOWNLOAD_APPLICATION = 0x41
DOWNLOAD_END = 0x42
# Multi-antenna RET
ANT_CALIBRATE = 0x80
ANT_SET_TILT = 0x81
ANT_GET_TILT = 0x82
ANT_SET_DEVICE_DATA = 0x83
ANT_GET_DEVICE_DATA = 0x84
ANT_ALARM_INDICATION = 0x85
ANT_CLEAR_ACTIVE_ALARMS = 0x86
ANT_GET_ALARM_STATUS = 0x87
ANT_GET_NUM_ANTENNAS = 0x88
ANT_SEND_CONFIG_DATA = 0x89

RETURN_CODES = {
    0x00: "OK",
    0x02: "MotorJam",
    0x03: "ActuatorJam",
    0x05: "Busy",
    0x06: "ChecksumError",
    0x0B: "FAIL",
    0x0E: "NotCalibrated",
    0x0F: "NotConfigured",
    0x11: "HardwareError",
    0x13: "OutOfRange",
    0x19: "UnknownProcedure",
    0x1A: "MinorTMAFault",
    0x1B: "MajorTMAFault",
    0x1C: "UnsupportedValue",
    0x1D: "ReadOnly",
    0x1E: "UnknownParameter",
    0x21: "WorkingSoftwareMissing",
    0x22: "InvalidFileContent",
    0x24: "FormatError",
    0x25: "UnsupportedProcedure",
    0x26: "InvalidProcedureSequence",
    0x27: "ActuatorInterference",
}


def rc_name(code: int) -> str:
    return RETURN_CODES.get(code, f"0x{code:02X}")


class RetError(AisgError):
    def __init__(self, proc, code):
        self.code = code
        super().__init__(f"procedure 0x{proc:02X} failed: {rc_name(code)}")


class Ret:
    """Single-antenna RET device (set antenna= for multi-antenna units)."""

    def __init__(self, link, addr: int, antenna: int | None = None):
        self.link = link
        self.addr = addr
        self.antenna = antenna

    def _proc(self, code: int, data: bytes = b"", timeout: float = 3.0) -> bytes:
        prefix = bytes([self.antenna]) if self.antenna is not None else b""
        msg = bytes([code]) + struct.pack("<H", len(prefix) + len(data)) + prefix + data
        resp = self.link.request(self.addr, msg, timeout=timeout)
        if len(resp) < 3 or resp[0] != code:
            raise AisgError(f"malformed response: {resp.hex(' ')}")
        (dlen,) = struct.unpack("<H", resp[1:3])
        body = resp[3 : 3 + dlen]
        if self.antenna is not None:
            body = body[1:]  # strip echoed antenna number
        if not body:
            raise AisgError("empty response body")
        if body[0] != 0x00:
            raise RetError(code, body[0])
        return body[1:]

    # -- common procedures --

    def get_information(self) -> dict:
        d = self._proc(GET_INFORMATION)
        out, i = {}, 0
        for key in ("product_number", "serial_number", "hardware_version",
                    "software_version"):
            if i >= len(d):
                break
            n = d[i]
            out[key] = d[i + 1 : i + 1 + n].decode("ascii", "replace")
            i += 1 + n
        return out

    def get_alarm_status(self) -> list[str]:
        code = ANT_GET_ALARM_STATUS if self.antenna is not None else GET_ALARM_STATUS
        return [rc_name(c) for c in self._proc(code)]

    def clear_alarms(self):
        self._proc(ANT_CLEAR_ACTIVE_ALARMS if self.antenna is not None
                   else CLEAR_ACTIVE_ALARMS)

    def alarm_subscribe(self):
        self._proc(ALARM_SUBSCRIBE)

    def self_test(self):
        self._proc(SELF_TEST, timeout=30.0)

    def reset(self):
        self._proc(RESET_SOFTWARE)

    # -- RET procedures --

    def calibrate(self):
        code = ANT_CALIBRATE if self.antenna is not None else CALIBRATE
        self._proc(code, timeout=120.0)

    def set_tilt(self, degrees: float):
        code = ANT_SET_TILT if self.antenna is not None else SET_TILT
        self._proc(code, struct.pack("<h", round(degrees * 10)), timeout=60.0)

    def get_tilt(self) -> float:
        code = ANT_GET_TILT if self.antenna is not None else GET_TILT
        d = self._proc(code)
        return struct.unpack("<h", d[:2])[0] / 10.0

    def num_antennas(self) -> int:
        saved, self.antenna = self.antenna, None
        try:
            return self._proc(ANT_GET_NUM_ANTENNAS)[0]
        finally:
            self.antenna = saved

    def get_device_data(self, field: int) -> bytes:
        code = ANT_GET_DEVICE_DATA if self.antenna is not None else GET_DEVICE_DATA
        return self._proc(code, bytes([field]))

    def send_config_data(self, blob: bytes):
        code = (ANT_SEND_CONFIG_DATA if self.antenna is not None
                else SEND_CONFIG_DATA)
        self._proc(code, blob, timeout=30.0)
