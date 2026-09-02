"""Job records for operations that outlive their HTTP request.

A tilt move cannot be aborted once the device has acknowledged it, so a
client that disconnects must still be able to learn the outcome -- and a
DELETE on a running job would be a lie. Jobs are therefore detachable, not
cancellable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    id: str
    command: str
    args: dict
    state: str = "running"          # running | done | failed
    result: Any = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_dict(self) -> dict:
        d = {
            "id": self.id, "command": self.command, "args": self.args,
            "state": self.state, "started_at": round(self.started_at, 3),
        }
        if self.result is not None:
            d["result"] = self.result
        if self.error:
            d["error"] = self.error
        if self.finished_at:
            d["finished_at"] = round(self.finished_at, 3)
            d["duration"] = round(self.finished_at - self.started_at, 2)
        return d


class JobStore:
    """Outlives the HTTP request that created it.

    A tilt move cannot be aborted once the device has acked it, so a client
    disconnecting must not lose the outcome -- and DELETE on a running job
    would be a lie. Jobs are therefore detachable, not cancellable.
    """

    def __init__(self, limit: int = 200):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._limit = limit

    def create(self, command: str, args: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], command=command, args=args)
        self._jobs[job.id] = job
        self._order.append(job.id)
        while len(self._order) > self._limit:
            self._jobs.pop(self._order.pop(0), None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [self._jobs[i].as_dict() for i in reversed(self._order)]
