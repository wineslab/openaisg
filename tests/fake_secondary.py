"""A fake AISG secondary station, for testing the link layer off-hardware.

The RET in the lab is lenient about sequence numbers, so it cannot be used to
prove the sequence handling is correct -- it answers even when N(S) is wrong.
This fake is strict, and can be told to inject the cases that are otherwise
impossible to stage: intervening RR polls, duplicate I-frames, FRMR, DM, and
an AlarmIndication arriving in the middle of a transaction.
"""

import time

from aisg import hdlc


class FakeSerial:
    """Enough of serial.Serial for AisgLink, driven by a responder callback."""

    def __init__(self, responder, timeout: float = 0.005):
        self.responder = responder
        self.timeout = timeout
        self.rx = bytearray()
        self.sent = []          # every frame the link wrote, as (addr, ctrl, payload)
        self.raw_sent = []
        self.is_open = True
        self.dtr = False
        self.rts = False
        self._deframer = hdlc.Deframer()

    # -- serial.Serial surface --

    @property
    def in_waiting(self):
        return len(self.rx)

    def write(self, data):
        self.raw_sent.append(bytes(data))
        for addr, ctrl, pl in self._deframer.feed(bytes(data)):
            self.sent.append((addr, ctrl, pl))
            for frame in self.responder(addr, ctrl, pl) or []:
                self.rx += frame
        return len(data)

    def read(self, n=1):
        if not self.rx:
            time.sleep(self.timeout)
            return b""
        take = min(n, len(self.rx))
        out = bytes(self.rx[:take])
        del self.rx[:take]
        return out

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.rx.clear()

    def close(self):
        self.is_open = False


def retap_response(code: int, data: bytes = b"", rc: int = 0x00) -> bytes:
    """A well-formed RETAP response: code | len16 | rc | data."""
    body = bytes([rc]) + data
    return bytes([code]) + len(body).to_bytes(2, "little") + body


def _lp(s: str) -> bytes:
    """Length-prefixed ASCII, as GetInformation returns."""
    return bytes([len(s)]) + s.encode()


def default_responses() -> dict:
    """A plausible RET21-AS155D, matching the real device's answers."""
    from aisg import retap as _r
    info = (_lp("RET21-AS155D") + _lp("21707700215001131")
            + _lp("2.00") + _lp("2.6.6"))
    return {
        _r.GET_INFORMATION: retap_response(_r.GET_INFORMATION, info),
        _r.GET_TILT: retap_response(_r.GET_TILT, (30).to_bytes(2, "little")),
        _r.SET_TILT: retap_response(_r.SET_TILT),
        _r.GET_ALARM_STATUS: retap_response(_r.GET_ALARM_STATUS),
        _r.CLEAR_ACTIVE_ALARMS: retap_response(_r.CLEAR_ACTIVE_ALARMS),
        _r.ALARM_SUBSCRIBE: retap_response(_r.ALARM_SUBSCRIBE),
        _r.GET_DEVICE_DATA: retap_response(_r.GET_DEVICE_DATA, b"\x00\x64"),
        _r.CALIBRATE: retap_response(_r.CALIBRATE),
        _r.SELF_TEST: retap_response(_r.SELF_TEST),
        _r.RESET_SOFTWARE: retap_response(_r.RESET_SOFTWARE),
    }


class Secondary:
    """Strict NRM secondary: answers polls, enforces N(S), never speaks first.

    Parameters mirror the failure modes we need to reproduce:
      rr_before_response -- how many RR "still working" answers to give before
                            the response I-frame (the slow-SetTilt shape)
      indications        -- payloads to deliver as unsolicited reports, each
                            piggybacked on the next poll
      duplicate_response -- send the response I-frame twice with the same N(S)
      reject_with        -- answer the next I-frame with FRMR or DM instead
      swallow_first      -- drop the first I-frame entirely (lost to noise)
    """

    def __init__(self, addr=0x01, response=None, responses=None,
                 rr_before_response=0, indications=(), duplicate_response=False,
                 reject_with=None, swallow_first=False, strict=True,
                 duplicate_indication=False):
        self.addr = addr
        # `response` answers every procedure with one canned payload (handy
        # for link-layer tests); `responses` maps procedure code -> payload,
        # which is what the session layer needs since it asks for identity,
        # device data, alarms and tilt in sequence.
        self.response = response
        self.responses = responses if responses is not None else (
            None if response is not None else default_responses())
        self.rr_before_response = rr_before_response
        self.indications = list(indications)
        self.duplicate_response = duplicate_response
        self.reject_with = reject_with
        self.swallow_first = swallow_first
        self.strict = strict
        self.duplicate_indication = duplicate_indication
        self._owed_rr = 0

        self.vs = 0             # our N(S)
        self.vr = 0             # what we expect from the primary
        self.connected = False
        self.pending = None     # response owed to the primary
        self.polls_seen = 0
        self.rejected_ns = []   # N(S) values we refused
        self.i_frames_seen = 0

    def _i(self, payload):
        ctrl = (self.vs << 1) | (self.vr << 5) | hdlc.PF
        self.vs = (self.vs + 1) & 7
        return hdlc.build_frame(self.addr, ctrl, payload)

    def _rr(self):
        return hdlc.build_frame(self.addr, 0x01 | (self.vr << 5) | hdlc.PF)

    def __call__(self, addr, ctrl, payload):
        if addr != self.addr and addr != hdlc.ADDR_BROADCAST:
            return []
        k = hdlc.kind(ctrl)

        if k == hdlc.U_FRAME:
            u = ctrl & ~hdlc.PF
            if u == hdlc.CTRL_SNRM:
                self.connected = True
                self.vs = self.vr = 0
                self.pending = None
                return [hdlc.build_frame(self.addr, hdlc.CTRL_UA | hdlc.PF)]
            if u == hdlc.CTRL_DISC:
                self.connected = False
                return [hdlc.build_frame(self.addr, hdlc.CTRL_UA | hdlc.PF)]
            return []

        if k == hdlc.I_FRAME:
            self.i_frames_seen += 1
            if self.swallow_first and self.i_frames_seen == 1:
                return []
            if self.reject_with is not None:
                what = self.reject_with
                self.reject_with = None
                return [hdlc.build_frame(self.addr, what | hdlc.PF)]
            ns = hdlc.ns_of(ctrl)
            if self.strict and ns != self.vr:
                # A strict secondary discards it and, being in NRM, can only
                # say so by continuing to answer polls with the old N(R).
                self.rejected_ns.append(ns)
                return [self._rr()]
            self.vr = (ns + 1) & 7
            self.pending = self._response_for(payload)
            self._owed_rr = self.rr_before_response
            return self._next()

        # Supervisory frame from the primary. In NRM the secondary may only
        # transmit when the poll bit invites it -- the final ack of an
        # exchange clears P precisely so nothing more comes back.
        if not ctrl & hdlc.PF:
            return []
        self.polls_seen += 1
        return self._next()

    def _response_for(self, request: bytes) -> bytes:
        if self.responses is not None and request:
            code = request[0]
            if code in self.responses:
                return self.responses[code]
            # Unknown procedure: what a real device answers.
            return retap_response(code, rc=0x19)
        return self.response

    def _next(self):
        """What we owe the primary, in priority order.

        Indications go out before the response so they land *inside* the
        transaction -- that is the case the demultiplexer exists for.
        """
        if self.indications:
            payload = self.indications.pop(0)
            frames = [self._i(payload)]
            if self.duplicate_indication:
                self.vs = (self.vs - 1) & 7   # same N(S): a retransmission
                frames.append(self._i(payload))
            return frames
        if self.pending is not None:
            if self._owed_rr > 0:
                self._owed_rr -= 1
                return [self._rr()]
            return self._deliver()
        return [self._rr()]

    def _deliver(self):
        payload, self.pending = self.pending, None
        frames = [self._i(payload)]
        if self.duplicate_response:
            # Same N(S): a retransmission because our ack went missing.
            self.vs = (self.vs - 1) & 7
            frames.append(self._i(payload))
        return frames
