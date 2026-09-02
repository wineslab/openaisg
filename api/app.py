#!/usr/bin/env python3
"""aisgd -- HTTP/WebSocket control for an AISG v2.0 RET actuator.

One process owns /dev/ttyUSB0 (the device is exclusive: allocatable=1) and
exposes the antenna's status, controls and alarms so a tilt sweep or a
closed-loop experiment is an HTTP client away.

GET endpoints answer from the worker's cached state and never touch the bus,
so polling this API is free and health probes cannot queue behind a 150s
calibrate. Writes go through the worker's queue, one at a time, because the
bus is half-duplex and the actuator can only do one thing at once.

Configuration (env var -- default):
  AISG_PORT            -- /dev/ttyUSB0     serial port
  AISG_BAUD            -- 9600
  AISG_MONITOR         -- duty-cycled      off | duty-cycled | continuous
  AISG_MONITOR_PERIOD  -- 60               seconds between alarm checks
  AISG_KEEPALIVE       -- 1.0              RR interval in continuous mode
  AISG_IDLE_RELEASE    -- 60               seconds before releasing the port
  AISG_FIRST_ADDRESS   -- 1                first HDLC address to assign
  AISG_PROBE_COUNT     -- 4                addresses to probe when scan is empty
  AISG_SYNC_BUDGET     -- 8.0              seconds before a request becomes a job
  AISG_HOST/AISG_PORT_HTTP -- 0.0.0.0/8080
  AISG_DEBUG           -- 0                hex-dump frames to the log
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (FastAPI, HTTPException, Query, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from aisg.link import AisgError, AisgTimeout, LinkReset, PortBusy, PortLost
from aisg.session import (COMMAND_TIMEOUTS, DEFAULT_COMMAND_TIMEOUT,
                          MonitorMode, PRIORITY_CONTROL, PRIORITY_USER,
                          SerialWorker, SessionError, WRITE_COMMANDS)
from api.events import EventBus, WorkerBridge
from api.jobs import Job, JobStore

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("AISG_DEBUG") == "1" else logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("aisg.server")

PORT = os.environ.get("AISG_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("AISG_BAUD", "9600"))
MONITOR = os.environ.get("AISG_MONITOR", "duty-cycled")
MONITOR_PERIOD = float(os.environ.get("AISG_MONITOR_PERIOD", "60"))
KEEPALIVE = float(os.environ.get("AISG_KEEPALIVE", "1.0"))
IDLE_RELEASE = float(os.environ.get("AISG_IDLE_RELEASE", "60"))
FIRST_ADDRESS = int(os.environ.get("AISG_FIRST_ADDRESS", "1"))
PROBE_COUNT = int(os.environ.get("AISG_PROBE_COUNT", "4"))
SYNC_BUDGET = float(os.environ.get("AISG_SYNC_BUDGET", "8.0"))
DEBUG = os.environ.get("AISG_DEBUG") == "1"

# WebSocket close codes, so a client can tell why it was refused.
CLOSE_BAD_SINCE = 4000


# --- app -------------------------------------------------------------------

bus = EventBus()
bridge = WorkerBridge(bus)
jobs = JobStore()
worker: SerialWorker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    bridge.bind(asyncio.get_running_loop())
    worker = SerialWorker(
        PORT, baud=BAUD, monitor=MonitorMode(MONITOR),
        monitor_period=MONITOR_PERIOD, keepalive=KEEPALIVE,
        idle_release=IDLE_RELEASE, first_address=FIRST_ADDRESS,
        probe_count=PROBE_COUNT, emit=bridge.emit, debug=DEBUG,
    )
    worker.start()
    logger.info("aisgd up: port=%s monitor=%s", PORT, MONITOR)
    try:
        yield
    finally:
        logger.info("aisgd shutting down; releasing the AISG link")
        worker.stop()
        # Give an in-flight operation a chance to finish and the link to be
        # closed cleanly, or the device is left holding an address.
        await asyncio.get_running_loop().run_in_executor(None, worker.join, 20.0)


app = FastAPI(
    title="aisgd",
    description=__doc__,
    version="1.0.0",
    lifespan=lifespan,
)


class TiltBody(BaseModel):
    degrees: float = Field(..., description="electrical downtilt in degrees")


class ConfirmBody(BaseModel):
    confirm: bool = Field(False, description="must be true; these move or reset hardware")


class LeaseBody(BaseModel):
    seconds: float = Field(300, gt=0, le=3600,
                           description="how long to keep the port released")


def _worker() -> SerialWorker:
    if worker is None:
        raise HTTPException(503, "worker not started")
    return worker


def _tilt_limits(snap: dict) -> tuple[float, float] | None:
    """Min/max tilt from the antenna's device data, in degrees.

    Fields 0x07/0x06 are min/max in the same 0.1-degree units SetTilt uses
    (0.0-10.0 on the AW3161-E-F-V2). Read from the device rather than
    hardcoded, and used to reject an out-of-range write before it costs a
    bus round trip.
    """
    dd = getattr(_worker(), "device_data", {}) or {}
    try:
        lo = int.from_bytes(bytes.fromhex(dd["0x07"].replace(" ", "")), "little") / 10
        hi = int.from_bytes(bytes.fromhex(dd["0x06"].replace(" ", "")), "little") / 10
    except (KeyError, ValueError):
        return None
    if hi <= lo:
        return None
    return lo, hi


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PortBusy):
        return HTTPException(409, str(exc))
    if isinstance(exc, PortLost):
        return HTTPException(503, str(exc))
    if isinstance(exc, SessionError):
        return HTTPException(503, str(exc))
    if isinstance(exc, LinkReset):
        return HTTPException(502, str(exc))
    if isinstance(exc, AisgTimeout):
        return HTTPException(504, str(exc))
    if isinstance(exc, AisgError):
        return HTTPException(502, str(exc))
    return HTTPException(500, str(exc))


async def _run(command: str, args: dict | None = None, *,
               wait: bool = False, priority: int = PRIORITY_USER):
    """Submit to the worker; answer inline if it is quick, else hand back a job.

    No endpoint has to be classified long-or-short in advance: a get_tilt on
    a healthy bus returns 200, the same call during a reconnect becomes a
    job. Cancelling the awaiting coroutine does not stop the worker, which is
    correct -- an actuator movement cannot be un-done.
    """
    w = _worker()
    args = args or {}
    try:
        fut = w.submit(command, args, priority=priority)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    budget = COMMAND_TIMEOUTS.get(command, DEFAULT_COMMAND_TIMEOUT) if wait else SYNC_BUDGET
    aio = asyncio.wrap_future(fut)
    try:
        return await asyncio.wait_for(asyncio.shield(aio), timeout=budget), None
    except asyncio.TimeoutError:
        job = jobs.create(command, args)

        def finish(f):
            try:
                job.result = f.result()
                job.state = "done"
            except Exception as e:  # noqa: BLE001 - recorded, not raised
                job.error = str(e)
                job.state = "failed"
            job.finished_at = time.time()
            bridge.emit({"type": "job", **job.as_dict()})

        fut.add_done_callback(finish)
        return None, job
    except Exception as e:  # noqa: BLE001 - translated to a status code
        raise _map_error(e) from e


# ---- read endpoints: cache only, no bus traffic ----

@app.get("/healthz")
async def healthz():
    """Liveness: is the worker thread iterating? An unpowered RET is not a
    reason to restart the pod."""
    w = _worker()
    snap = w.snapshot()
    alive = w.is_alive() and snap["heartbeat_age"] < 30
    body = {
        "ok": alive,
        "state": snap["state"],
        "heartbeat_age": snap["heartbeat_age"],
        "queue_depth": snap["queue_depth"],
        "ws_clients": bus.subscriber_count,
    }
    return JSONResponse(body, status_code=200 if alive else 503)


@app.get("/readyz")
async def readyz():
    w = _worker()
    ready = w.is_alive()
    return JSONResponse({"ok": ready, "state": w.snapshot()["state"]},
                        status_code=200 if ready else 503)


@app.get("/api/v1/status")
async def status(refresh: bool = Query(False, description="force a bus read")):
    if refresh:
        result, job = await _run("get_tilt")
        if job:
            return JSONResponse({"status": _worker().snapshot(), "job": job.as_dict()},
                                status_code=202)
    snap = _worker().snapshot()
    limits = _tilt_limits(snap)
    if limits:
        snap["tilt_limits"] = {"min": limits[0], "max": limits[1]}
    return snap


@app.get("/api/v1/device")
async def device():
    """Antenna and actuator identity, plus the raw device-data block."""
    w = _worker()
    snap = w.snapshot()
    limits = _tilt_limits(snap)
    return {
        "identity": snap["identity"],
        "device_data": w.device_data,
        "tilt_limits": ({"min": limits[0], "max": limits[1], "step": 0.1}
                        if limits else None),
        "address": snap["address"],
    }


@app.get("/api/v1/tilt")
async def get_tilt():
    snap = _worker().snapshot()
    return {"tilt": snap["tilt"], "target": snap["target_tilt"]}


@app.get("/api/v1/alarms")
async def get_alarms():
    snap = _worker().snapshot()
    return {"active": snap["alarms"], "mode": snap["alarm_mode"]}


@app.get("/api/v1/jobs")
async def list_jobs():
    return {"jobs": jobs.list()}


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    return job.as_dict()


# ---- write endpoints ----

@app.put("/api/v1/tilt")
async def set_tilt(body: TiltBody,
                   wait: bool = Query(False, description="block until the move completes")):
    limits = _tilt_limits(_worker().snapshot())
    if limits and not (limits[0] <= body.degrees <= limits[1]):
        raise HTTPException(
            400,
            f"tilt {body.degrees} outside the antenna's range "
            f"{limits[0]}-{limits[1]} degrees (from its device data)",
        )
    result, job = await _run("set_tilt", {"degrees": body.degrees}, wait=wait)
    if job:
        return JSONResponse(job.as_dict(), status_code=202)
    return result


@app.post("/api/v1/alarms/clear")
async def clear_alarms():
    result, job = await _run("clear_alarms")
    return JSONResponse(job.as_dict(), status_code=202) if job else {"ok": True}


@app.post("/api/v1/calibrate")
async def calibrate(body: ConfirmBody,
                    wait: bool = Query(False)):
    if not body.confirm:
        raise HTTPException(400, "calibrate drives the actuator to its end "
                                 "stops; pass {\"confirm\": true}")
    result, job = await _run("calibrate", wait=wait)
    return JSONResponse(job.as_dict(), status_code=202) if job else {"ok": True}


@app.post("/api/v1/self-test")
async def self_test(body: ConfirmBody, wait: bool = Query(False)):
    if not body.confirm:
        raise HTTPException(400, "pass {\"confirm\": true}")
    result, job = await _run("self_test", wait=wait)
    return JSONResponse(job.as_dict(), status_code=202) if job else {"ok": True}


@app.post("/api/v1/reset")
async def reset(body: ConfirmBody, wait: bool = Query(False)):
    if not body.confirm:
        raise HTTPException(400, "reset restarts the device software; pass "
                                 "{\"confirm\": true}")
    result, job = await _run("reset", wait=wait)
    return JSONResponse(job.as_dict(), status_code=202) if job else {"ok": True}


@app.post("/api/v1/refresh")
async def refresh_device_data():
    """Re-read identity and the antenna parameter block from the device."""
    info, job1 = await _run("get_information", wait=True)
    dd, job2 = await _run("get_device_data", wait=True)
    return {"identity": info, "device_data": dd}


# ---- port handoff ----

@app.post("/api/v1/lease")
async def take_lease(body: LeaseBody):
    """Release the port and refuse to reopen it, so the CLI can be used.

    The idle timer alone is not enough: any request or monitor tick reopens
    the port, and an operator typing `aisgctl` loses that race.
    """
    until = _worker().lease(body.seconds)
    return {"leased": True, "seconds": body.seconds,
            "until_monotonic": round(until, 1)}


@app.delete("/api/v1/lease")
async def drop_lease():
    _worker().unlease()
    return {"leased": False}


# ---- events ----

@app.websocket("/api/v1/events")
async def events(ws: WebSocket, since: int = 0):
    """Snapshot on connect, then every state change, alarm edge and job event.

    Under NRM the device only speaks when polled, so alarm latency is the
    keepalive/monitor interval -- this stream looks like push to a client but
    is not instantaneous at the wire.
    """
    await ws.accept()
    sub = bus.subscribe()
    try:
        await ws.send_json({"type": "snapshot", "ts": time.time(),
                            **_worker().snapshot()})
        if since:
            for ev in bus.replay_since(since):
                await ws.send_json(ev)
        while True:
            ev = await sub.q.get()
            if sub.dropped:
                # Tell the client its incremental view is untrustworthy
                # rather than letting it silently miss an alarm.
                await ws.send_json({"type": "resync", "dropped": sub.dropped})
                sub.dropped = 0
                await ws.send_json({"type": "snapshot", **_worker().snapshot()})
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - never take the server down with a client
        logger.exception("websocket")
    finally:
        bus.unsubscribe(sub)


# ---- test UI ----
#
# Mounted last so it cannot shadow an API path. Same origin as the API, so
# no CORS and the WebSocket URL is derived from location.host.

_UI = os.path.join(_ROOT, "ui")

if os.path.isdir(_UI):
    app.mount("/ui", StaticFiles(directory=_UI, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(os.path.join(_UI, "index.html"))


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=os.environ.get("AISG_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AISG_PORT_HTTP", "8080")))
    return ap.parse_args(argv)


def main():
    args = _parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
