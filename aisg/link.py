"""AISG v2.0 primary-station link layer over a serial port.

Implements device scan, address assignment, SNRM connection and
stop-and-wait I-frame exchange (window size 1) per 3GPP TS 25.462.
"""

import time

import serial

from . import hdlc


class AisgError(Exception):
    pass


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
                 timeout: float = 1.0, debug: bool = False):
        self.ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                                 timeout=0.05)
        if dtr is not None:
            self.ser.dtr = dtr
        self.timeout = timeout
        self.debug = debug
        self.vs = 0  # send sequence N(S)
        self.vr = 0  # receive sequence N(R)

    def close(self):
        self.ser.close()

    def _log(self, direction, data):
        if self.debug:
            print(f"{direction} {data.hex(' ')}")

    def _xfer(self, frame: bytes, timeout: float | None = None) -> bytes:
        """Send one frame, collect raw response bytes until idle."""
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
                self.vs = self.vr = 0
                return
        raise AisgError(f"no UA to SNRM from address 0x{addr:02X}")

    def disconnect(self, addr: int):
        self._command(addr, hdlc.CTRL_DISC | hdlc.PF)

    def probe_address(self, addr: int) -> bool:
        """True if a secondary answers SNRM at this address."""
        try:
            self.connect(addr)
            return True
        except AisgError:
            return False

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

    def request(self, addr: int, message: bytes, timeout: float = 3.0,
                retries: int = 3) -> bytes:
        """Send a RETAP/TMAAP message in an I-frame; poll until the response
        I-frame arrives; ACK it. Returns the layer-7 response bytes."""
        ictrl = (self.vs << 1) | (self.vr << 5) | hdlc.PF
        attempt = 0
        frames = self._command(addr, ictrl, message)
        deadline = time.monotonic() + timeout
        while True:
            for faddr, ctrl, pl in frames:
                if faddr != addr:
                    continue
                if ctrl & 1 == 0:  # I-frame from secondary
                    ns = (ctrl >> 1) & 7
                    self.vr = (ns + 1) & 7
                    self.vs = (self.vs + 1) & 7
                    # ACK with RR so the device can release its buffer
                    rr = 0x01 | (self.vr << 5) | hdlc.PF
                    self._command(addr, rr, timeout=0.3)
                    return pl
                if ctrl & 0x0F == 0x01:  # RR: nothing to send yet, keep polling
                    ack = (ctrl >> 5) & 7
                    if ack == ((self.vs + 1) & 7):
                        self.vs = ack  # our I-frame was received
            if time.monotonic() > deadline:
                attempt += 1
                if attempt > retries:
                    raise AisgError(f"no layer-7 response from 0x{addr:02X}")
                deadline = time.monotonic() + timeout
            time.sleep(0.05)
            rr = 0x01 | (self.vr << 5) | hdlc.PF
            frames = self._command(addr, rr)
