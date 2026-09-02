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

## aisgd -- the HTTP/WebSocket service

`server/server.py` is a FastAPI service that owns the serial port and exposes
the RET over HTTP, so a tilt sweep or a closed-loop experiment is an HTTP
client away instead of an `oc exec`. Deployed on `devkit06sno` in ns `aisg`:

    https://aisgd-aisg.apps.devkit06sno.spectranet.open6g.net
    /docs        OpenAPI browser

    GET  /healthz /readyz
    GET  /api/v1/device            identity + antenna parameters + tilt limits
    GET  /api/v1/status            cached state (?refresh=true forces a read)
    GET  /api/v1/tilt              current and target angle
    PUT  /api/v1/tilt              {"degrees": 4.5}  (?wait=true to block)
    GET  /api/v1/alarms
    POST /api/v1/alarms/clear
    POST /api/v1/calibrate         {"confirm": true}
    POST /api/v1/self-test         {"confirm": true}
    POST /api/v1/reset             {"confirm": true}
    POST /api/v1/refresh           re-read identity and device data
    GET  /api/v1/jobs /api/v1/jobs/{id}
    POST /api/v1/lease             {"seconds": 300}   release the port
    DEL  /api/v1/lease
    WS   /api/v1/events            snapshot, then state/alarm/job events

`GET`s answer from cache and cost no bus time. A request that outlives
`AISG_SYNC_BUDGET` comes back as a job (202) and finishes in the background --
jobs are detachable, not cancellable, because an actuator move cannot be
un-done. Tilt writes are checked against the antenna's own limits (0.0-10.0
on the AW3161-E-F-V2) before anything touches the wire.

    oc apply -f server/deployment.yaml
    oc -n aisg start-build aisgctl --from-dir=. -F

### Monitoring is a mode, and here is why

Holding the link open to watch for alarms and releasing the port so the CLI
can use it are mutually exclusive. AISG runs in Normal Response Mode: the
secondary may only transmit when polled, so "watching" means polling forever
and the idle timer never fires. `AISG_MONITOR` picks the trade:

| mode | alarm latency | port availability |
|---|---|---|
| `off` | none | released after `AISG_IDLE_RELEASE` |
| `duty-cycled` (default) | ~`AISG_MONITOR_PERIOD` (60 s) | free ~97% of the time |
| `continuous` | ~`AISG_KEEPALIVE` (1 s) | pinned; needs a lease to borrow |

`AlarmSubscribe` (0x12) is accepted by the RET21-AS155D (rc 0x00), so alarms
do arrive piggybacked on polls -- but the poll interval is still the latency
floor. The service falls back to `GetAlarmStatus` polling automatically if a
device rejects subscribe.

### Sharing the port with the CLI

`AisgLink` takes an advisory `flock` (`exclusive=True`) by default, so the CLI
and the service cannot silently interleave HDLC onto the same half-duplex bus
-- contention is an error, not corruption. In `duty-cycled` mode the port is
usually closed and `aisgctl` just works. To guarantee it:

    curl -sk -X POST https://<host>/api/v1/lease -d '{"seconds":300}'
    oc -n aisg exec deploy/aisgd -- ./aisgctl -p /dev/ttyUSB0 tilt
    curl -sk -X DELETE https://<host>/api/v1/lease

### Testing without hardware

`tests/fake_secondary.py` is a strict NRM secondary that can inject the cases
the real RET cannot be made to produce: intervening RR polls, duplicate
I-frames, FRMR, DM, a lost command, and an AlarmIndication mid-transaction.
The lab actuator is lenient about sequence numbers -- it answers even when
N(S) is wrong -- so it cannot prove the link layer correct. `pytest tests/`

## Notes / unknowns to confirm on hardware

- The ATC200-LITE-USB powers the RET from its own supply; DTR is asserted on
  open (`--no-dtr` to disable) since ATC Lite manipulates DTR via FT_SetDTR.
- Firmware download (0x40–0x42) and Andrew vendor-specific procedures
  (0x90+) are not implemented; capture them with the sniffer if needed.

## Confirmed on hardware (RET21-AS155D on an Amphenol AW3161-E-F-V2)

- Device data fields 0x01-0x0B exist, 0x0C+ answer `FAIL`. 0x01 is the antenna
  model, 0x02 its serial, 0x04 beamwidth (65 deg x3 bands), 0x05 gain
  (18.0 dBi x3), 0x07/0x06 min/max tilt (0.0/10.0 deg, 0.1 deg steps).
- `AlarmSubscribe` (0x12) is supported: rc 0x00.
- The actuator is **lenient about sequence numbers** -- it answered an I-frame
  with N(S)=2 while its own N(R) was 1. Do not use it to validate the link
  layer; use the fake secondary.
- It does not answer the broadcast scan once addressed (TS 25.462 4.8.4), and
  appears to drop its address on link loss. DISC releases it immediately.
- The FTDI adapter can wedge at the USB level: every open then fails while the
  device node still exists, with `ftdi_sio ttyUSB0: failed to set flow
  control: -71` in the host log. Recover with a driver rebind on the node:

      echo -n 1-6:1.0 > /sys/bus/usb/drivers/ftdi_sio/unbind
      echo -n 1-6:1.0 > /sys/bus/usb/drivers/ftdi_sio/bind
