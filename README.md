# aisg-ret — native Linux AISG v2.0 RET control

A from-scratch replacement for ATC Lite's control path, implementing the open
AISG v2.0 protocol (3GPP TS 25.462 transport + TS 25.466 RETAP) directly over
a Linux serial port — plus an HTTP/WebSocket service and a web UI, so antenna
downtilt is a controllable variable in an experiment rather than a Windows GUI
someone has to click.

Runs against an **Alpha Wireless AW3161-E-F-V2** antenna with a
**RET21-AS155D** actuator, via an **ATC200-LITE-USB** modem (FTDI,
`/dev/ttyUSB0` through `ftdi_sio`; no D2XX driver needed). Runs on a plain
Linux host, or on OpenShift via `deploy/`.

```
                    ui/            index.html      ── browser
                     │
                    api/           FastAPI + WS    ── curl / scripts
                     │
       aisg/session.py             one owner of the port, one queue
                     │
   aisg/link.py, hdlc.py, retap.py HDLC · XID scan · RETAP
                     │
                /dev/ttyUSB0       RS-485, 9600 8N1, half-duplex
```

## Layout

| Path | What lives there |
|---|---|
| `aisg/` | the protocol library — framing, link layer, RETAP, session |
| `api/` | the HTTP/WebSocket service (`app.py`, `events.py`, `jobs.py`) |
| `ui/` | single-page test UI, served by the API at `/` |
| `cli/` | `aisgctl`, the one-shot command-line tool |
| `utils/` | bench tools: `preflight.py`, `sniff_bridge.py` |
| `deploy/` | OpenShift manifests (`device-plugin.yaml`, `aisgd.yaml`) |
| `tests/` | 63 tests, no hardware required |
| `docs/` | protocol, hardware findings, deployment, validation |

Inside `aisg/`:

- `hdlc.py` — HDLC async framing, CRC-16/X.25 FCS, XID parameter coding, plus
  `Deframer` (incremental and resynchronising) and `kind()` (control-field
  classification)
- `link.py` — primary-station link: device scan with collision bisection,
  address assignment, SNRM connect, per-peer sequence state, and `exchange()`
  (stop-and-wait with a demultiplexer for unsolicited reports)
- `retap.py` — RETAP elementary procedures and return codes
- `session.py` — the long-lived session: single-owner worker thread, link
  state machine, idle release, alarm tracking, monitoring modes

## Quickstart

The service (what you normally want):

```bash
pip install -r requirements.txt
python3 -m api.app                 # :8080, UI at /, OpenAPI at /docs
```

The CLI, straight at the port:

```bash
cli/aisgctl -p /dev/ttyUSB0 scan          # discover devices on the bus
cli/aisgctl -p /dev/ttyUSB0 info          # product / serial / hw / sw
cli/aisgctl -p /dev/ttyUSB0 tilt          # read tilt, degrees
cli/aisgctl -p /dev/ttyUSB0 tilt 4.5      # set tilt
cli/aisgctl -p /dev/ttyUSB0 calibrate
cli/aisgctl -p /dev/ttyUSB0 alarms
cli/aisgctl -d ...                        # hex-dump every frame
cli/aisgctl -a 2 ...                      # MRET: antenna number
```

Before trusting a silent bus, `utils/preflight.py /dev/ttyUSB0` checks the
port opens and dumps a raw XID probe, so you can tell a wiring or power
problem from a protocol mismatch.

## Deploying

```bash
oc apply -f deploy/device-plugin.yaml     # once
oc apply -f deploy/aisgd.yaml
oc -n aisg start-build aisgctl --from-dir=. -F

# the Route host is assigned by the cluster; web UI at /, OpenAPI at /docs
oc get route aisgd -n aisg -o jsonpath='{.spec.host}{"\n"}'
```

See **[docs/deployment.md](docs/deployment.md)** for why this needs a device
plugin rather than a privileged pod, and the gotchas that cost real time.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/            # 63 tests, ~45s, no hardware
```

`tests/fake_secondary.py` is a strict NRM secondary standing in for the RET.
It exists because **the real actuator is lenient about sequence numbers** — it
answered an I-frame with `N(S)=2` while its own `N(R)` was 1 — so it cannot be
used to prove the link layer correct. The fake injects what the hardware will
not: intervening RR polls, duplicate I-frames, FRMR, DM, a lost command, an
AlarmIndication mid-transaction, and a dribbling UART that delivers a reply a
few bytes at a time.

## Docs

| | |
|---|---|
| [docs/protocol.md](docs/protocol.md) | AISG v2.0 on the wire, with real captures |
| [docs/service.md](docs/service.md) | the API, monitoring modes, port leases |
| [docs/deployment.md](docs/deployment.md) | OpenShift, the device plugin, recovery |
| [docs/hardware.md](docs/hardware.md) | what this specific antenna told us |
| [docs/validating.md](docs/validating.md) | diffing against ATC Lite under Wine |
