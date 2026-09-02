"""Half-duplex discipline: never transmit while a frame is still arriving.

A response is ~50 bytes, which at 9600 baud is ~52 ms and arrives in several
reads. If the pump returns on a fixed budget mid-frame, the caller polls on
top of the device's transmission and corrupts the bus in both directions.
This was observed on real hardware before it was fixed.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aisg import hdlc, link
from tests.fake_secondary import Secondary, default_responses
from tests.test_exchange import make_link


class DribblingSerial:
    """Delivers the reply a few bytes at a time, like a slow UART, and records
    whether the primary ever wrote while a frame was in flight."""

    def __init__(self, responder, chunk=4, gap=0.004):
        self.responder = responder
        self.chunk = chunk
        self.gap = gap
        self.timeout = 0.002
        self.pending = bytearray()
        self.inflight = False
        self.collisions = 0
        self.is_open = True
        self.dtr = False
        self.rts = False
        self._next_at = 0.0
        self._deframer = hdlc.Deframer()

    @property
    def in_waiting(self):
        self._tick()
        return len(self._ready)

    _ready = b""

    def _tick(self):
        now = time.monotonic()
        if self.pending and now >= self._next_at:
            take = min(self.chunk, len(self.pending))
            self._ready = bytes(self._ready) + bytes(self.pending[:take])
            del self.pending[:take]
            self._next_at = now + self.gap
            self.inflight = bool(self.pending)

    def write(self, data):
        if self.inflight or self.pending:
            self.collisions += 1
        for addr, ctrl, pl in self._deframer.feed(bytes(data)):
            for frame in self.responder(addr, ctrl, pl) or []:
                self.pending += frame
        self.inflight = bool(self.pending)
        self._next_at = time.monotonic()
        return len(data)

    def read(self, n=1):
        self._tick()
        if not self._ready:
            time.sleep(self.timeout)
            self._tick()
        if not self._ready:
            return b""
        take = min(n, len(self._ready))
        out = bytes(self._ready[:take])
        self._ready = bytes(self._ready[take:])
        return out

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._ready = b""

    def close(self):
        self.is_open = False


def make_dribbling_link(secondary, **kw):
    lk = link.AisgLink.__new__(link.AisgLink)
    lk.ser = DribblingSerial(secondary, **kw)
    lk.timeout = 1.0
    lk.debug = False
    lk._peers = {}
    lk._deframer = hdlc.Deframer()
    lk.stats = {"polls": 0, "dup_i": 0, "unexpected": 0, "foreign": 0,
                "rnr": 0, "retx": 0, "link_resets": 0}
    return lk


def test_no_write_while_a_frame_is_in_flight():
    sec = Secondary()
    lk = make_dribbling_link(sec, chunk=3, gap=0.003)
    lk.connect(0x01)
    info = lk.exchange(0x01, b"\x05\x00\x00", timeout=5.0, poll_interval=0.01)
    assert info[0] == 0x05
    assert lk.ser.collisions == 0


def test_long_reply_arrives_intact_despite_chunking():
    sec = Secondary()
    lk = make_dribbling_link(sec, chunk=2, gap=0.002)
    lk.connect(0x01)
    got = lk.exchange(0x01, b"\x05\x00\x00", timeout=5.0, poll_interval=0.01)
    assert got == default_responses()[0x05]
    assert lk.fcs_errors == 0


def test_many_consecutive_exchanges_stay_clean():
    """The service does ~15 exchanges back to back on connect, which is how
    this surfaced."""
    sec = Secondary()
    lk = make_dribbling_link(sec, chunk=3, gap=0.002)
    lk.connect(0x01)
    for _ in range(12):
        lk.exchange(0x01, b"\x34\x00\x00", timeout=5.0, poll_interval=0.01)
    assert lk.ser.collisions == 0
    assert lk.stats["link_resets"] == 0
    assert lk.fcs_errors == 0


def test_deframer_reports_a_partial_frame():
    d = hdlc.Deframer()
    assert d.partial is False
    d.feed(b"\x7e\x01\x31")
    assert d.partial is True
    d.feed(b"\x95\x36\x7e")
    assert d.partial is False
