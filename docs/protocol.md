# AISG v2.0 on the wire

Three layers, two specs. Everything here is implemented in `aisg/`, and every
byte sequence shown was captured from the RET21-AS155D on the bench.

## Physical

RS-485 half-duplex over the antenna cable, **9600 8N1**. DTR is asserted on
open, mirroring what ATC Lite does via `FT_SetDTR` (`--no-dtr` to suppress).
Some adapters instead gate the transceiver with RTS, worth trying if the bus
is silent.

**Half-duplex is not a footnote.** A ~50-byte reply takes ~52 ms at 9600 baud
and arrives across several USB reads. Transmitting before it has finished
corrupts both directions. `AisgLink._read_frames()` therefore extends its read
budget while bytes keep flowing and refuses to return mid-frame; see
`hdlc.Deframer.partial`. Getting this wrong produced a real FRMR — the wire
trace is in `docs/hardware.md`.

## Data link — HDLC, 3GPP TS 25.462 (`aisg/hdlc.py`)

```
0x7E | ADDR | CTRL | payload | FCS(2, little-endian) | 0x7E
```

- Byte stuffing: `0x7E -> 0x7D 0x5E`, `0x7D -> 0x7D 0x5D` (escape, then XOR `0x20`)
- FCS: **CRC-16/X.25** — reflected poly `0x1021` (table built with `0x8408`),
  init `0xFFFF`, xorout `0xFFFF`
- Addresses: `0xFF` broadcast, `0x00` "no device"; secondaries get 1..n assigned
- U-frames: SNRM `0x83`, XID `0xAF`, UA `0x63`, DISC `0x43`, DM `0x0F`,
  FRMR `0x87`; the P/F bit is `0x10`
- **Normal Response Mode**: the primary polls, the secondary may only answer.
  Window size 1 (stop-and-wait) — one I-frame outstanding, ACKed with RR.

`hdlc.kind()` classifies a control byte. This matters more than it looks:
FRMR (`0x87`) and DM (`0x0F`) satisfy neither `ctrl & 1 == 0` (I-frame) nor
`ctrl & 0x0F == 0x01` (RR), so a naive classifier ignores them both and waits
out a full timeout instead of re-establishing the link.

### Sequence numbers

`V(S)` must advance exactly **once per transmitted I-frame**, driven by the
peer's `N(R)` (`AisgLink._apply_ack` is the only writer). Advancing it again on
the response I-frame leaves it one too high after any procedure slow enough to
be RR-polled — which the bench actuator tolerates and a strict device answers
with FRMR. State is **per peer** (`PeerState`): AISG allows several addressed
secondaries with independent sequence state, so one shared pair breaks the
moment a second RET is driven.

Retransmission is triggered **only** by a peer `N(R)` that still asks for our
frame. Silence is ambiguous — the reply may have been lost *after* the device
acted — so resending on silence risks a second physical actuator movement.

## Device scan and address assignment (TS 25.462 §4.8.3–4.8.4)

Scan uses **XID with the user-defined parameter set** `FI=0x81, GI=0xF0`, then
TLV parameters: `1` UniqueID, `2` HDLCAddress, `3` Bitmask, `4` DeviceType,
`5` ProtocolVersion, `6` VendorCode, `7` ResetDevice.

A broadcast XID carrying **UniqueID with PL=0 and Bitmask with PL=0** matches
every unaddressed secondary:

```
7e ff bf 81 f0 04 01 00 03 00 a6 58 7e
   │  │  │  │  │  │           └─ FCS
   │  │  │  │  │  └─ PI=01 PL=00 (UniqueID, empty) · PI=03 PL=00 (Bitmask, empty)
   │  │  │  │  └─ GL = 4
   │  │  │  └─ GI = F0 (user-defined set)
   │  │  └─ FI = 81
   │  └─ CTRL = XID(AF) | P(10)
   └─ ADDR = FF broadcast
```

A reply from an unaddressed device answering at `addr=0x00` (a real capture,
with the unit's unique ID swapped for a placeholder and the FCS recomputed to
match):

```
7e 00 bf 81 f0 1c 01 13 41 53 31 32 33 34 35 36 37 38 39 30 31 32 33 30 30 32 30
      06 02 41 53 04 01 01 36 7d 5d 7e
   -> params {1: b'AS12345678901230020', 6: b'AS', 4: b'\x01'}
```

Note the tail `36 7d 5d 7e`: an FCS byte of `0x7d` that had to be unescaped.

If several devices answer at once the replies collide and the FCS check fails,
so **a bad FCS during a scan is a positive signal**, not an error — walk the
unique-ID space by bisection, adding one bit to the bitmask per level, until
each probe matches exactly one device (`AisgLink.scan`, `max_depth=24`).

This is also why `hdlc.parse_frames()` is kept alongside `hdlc.Deframer`: the
scan path *needs* the exception, while a long-lived session needs a deframer
that resynchronises instead of raising.

## Application — RETAP, 3GPP TS 25.466 (`aisg/retap.py`)

Layer-7 messages ride inside I-frames:

```
procedure code (1) | number of data octets (2, little-endian) | data
```

All multi-octet integers little-endian. **Tilt is `int16` in 0.1° units**, so
4.5° on the wire is `2d 00`.

Implemented: `0x03` ResetSoftware, `0x04` GetAlarmStatus, `0x05`
GetInformation, `0x06` ClearActiveAlarms, `0x0A` SelfTest, `0x0E`/`0x0F`
Set/GetDeviceData, `0x10`/`0x11` Read/WriteUserData, `0x12` AlarmSubscribe,
`0x31` Calibrate, `0x32` SendConfigData, `0x33` SetTilt, `0x34` GetTilt.
Multi-antenna (MRET) mirrors these at `0x80+` with the antenna number
prefixed to the data — `0x88` GetNumberOfAntennas.

Every response opens with a return code: `0x00` OK, and the failure set
includes `MotorJam`, `ActuatorJam`, `Busy`, `NotCalibrated`, `NotConfigured`,
`OutOfRange`, `ActuatorInterference`, `UnsupportedProcedure`,
`InvalidProcedureSequence`.

**Not implemented:** software download (`0x40`–`0x42`) and Andrew
vendor-specific procedures (`0x90`+). Capture them with
`utils/sniff_bridge.py` if they are ever needed.

### Alarm reports are still polled

`AlarmSubscribe` (`0x12`) is accepted by this RET (rc `0x00`), after which an
`AlarmIndication` (`0x07`, or `0x85` for MRET) can arrive in place of the
response to an outstanding request — which is what `session.RetapRouter` and
`AisgLink.exchange`'s router argument exist to separate.

But NRM means the device still cannot speak unbidden: an indication rides a
poll response. So the keepalive interval is the alarm-latency floor no matter
which mechanism is used, and subscription buys bus economy rather than
immediacy.
