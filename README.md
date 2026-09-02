# aisgctl — native Linux AISG v2.0 RET controller

A from-scratch replacement for ATC Lite's control path, implementing the open
AISG v2.0 protocol (3GPP TS 25.462 transport + TS 25.466 RETAP) directly over
a Linux serial port. Works with FTDI-based AISG modems such as the
**ATC200-LITE-USB** (enumerates as `/dev/ttyUSB0` via `ftdi_sio`; no D2XX
driver needed).

## Usage

```bash
./aisgctl -p /dev/ttyUSB0 scan          # discover devices (XID device scan)
./aisgctl -p /dev/ttyUSB0 info          # product / serial / hw / sw version
./aisgctl -p /dev/ttyUSB0 tilt          # read electrical tilt (degrees)
./aisgctl -p /dev/ttyUSB0 tilt 4.5      # set tilt
./aisgctl -p /dev/ttyUSB0 calibrate
./aisgctl -p /dev/ttyUSB0 alarms
./aisgctl -d ... # hex-dump every frame on the wire
```

Multi-antenna (MRET) units: add `-a <antenna-number>`.

## Layout

- `aisg/hdlc.py` — HDLC async framing, CRC-16/X.25 FCS, XID parameter coding
- `aisg/link.py` — primary-station link: device scan (with collision
  bisection), address assignment, SNRM connect, stop-and-wait I-frame exchange
- `aisg/retap.py` — RETAP elementary procedures and return codes
- `tools/sniff_bridge.py` — pty bridge that lets ATC Lite (under Wine, serial
  mode) drive the real adapter while logging decoded AISG frames — use it to
  compare this implementation against the vendor tool byte-for-byte
- `tests/test_hdlc.py` — checks against the worked frame examples in
  3GPP TS 25.462 Annex D (`pytest tests/`)

## Validating against ATC Lite

1. `python3 tools/sniff_bridge.py /dev/ttyUSB0` — prints a pty path
2. `ln -sf <pty> ~/.wine-atc/dosdevices/com1`
3. Run ATC Lite (Wine) in serial mode and do a scan/get-info/set-tilt
4. Compare `aisg_capture.log` with `./aisgctl -d` output for the same actions

## Notes / unknowns to confirm on hardware

- The ATC200-LITE-USB powers the RET from its own supply; DTR is asserted on
  open (`--no-dtr` to disable) since ATC Lite manipulates DTR via FT_SetDTR.
- Firmware download (0x40–0x42) and Andrew vendor-specific procedures
  (0x90+) are not implemented; capture them with the sniffer if needed.
