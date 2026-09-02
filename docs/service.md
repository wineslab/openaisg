# aisgd — the HTTP/WebSocket service

`api/app.py` is a FastAPI service that owns the serial port and exposes
the RET over HTTP, so a tilt sweep or a closed-loop experiment is an HTTP
client away instead of an `oc exec`. Deployed in namespace `aisg`; the Route
host is whatever the cluster assigns:

    oc get route aisgd -n aisg -o jsonpath='{.spec.host}{"\n"}'

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

    oc apply -f deploy/aisgd.yaml
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
    oc -n aisg exec deploy/aisgd -- aisgctl -p /dev/ttyUSB0 tilt
    curl -sk -X DELETE https://<host>/api/v1/lease

### Testing without hardware

`tests/fake_secondary.py` is a strict NRM secondary that can inject the cases
the real RET cannot be made to produce: intervening RR polls, duplicate
I-frames, FRMR, DM, a lost command, and an AlarmIndication mid-transaction.
The lab actuator is lenient about sequence numbers -- it answers even when
N(S) is wrong -- so it cannot prove the link layer correct. `pytest tests/`
