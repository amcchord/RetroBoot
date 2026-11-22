"""Long-running jobs with progress and exclusive resource ownership.

A job owns zero or more named resources (``guest``, ``machines``, ``install``).
Starting a job whose kind is already running returns the running job
(idempotent duplicate requests).  A different job needing a busy resource is
rejected with :class:`Busy`, unless the new job may preempt the holder (force
stop and force restart preempt a clean lifecycle request).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .util import new_id, wall

log = logging.getLogger("retroboot.jobs")

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"

# What a finished job says when its body did not set an outcome (``message`` is the
# current step while running, then the outcome).
OUTCOMES = {
    "guest.start": "Mac OS is running.",
    "guest.restart": "Mac OS restarted.",
    "guest.shutdown": "Mac OS shut down.",
    "guest.force-stop": "Mac OS was stopped.",
    "guest.force-restart": "Mac OS was restarted.",
    "host.poweroff": "RetroBoot is turning off.",
    "host.reboot": "RetroBoot is restarting.",
    "settings.apply": "The settings are applied.",
    "machines.select": "The startup Mac is selected.",
    "machines.import": "The Mac is installed.",
    "machines.remove": "The Mac is removed.",
    "checkpoint.create": "The checkpoint is saved.",
    "checkpoint.restore": "The checkpoint is restored.",
    "diagnostics.bundle": "The evidence bundle is ready.",
    "wifi.scan": "The Wi-Fi networks are up to date.",
    "wifi.connect": "Wi-Fi is connected.",
    "installer.install": "Installation finished.",
    "installer.update": "RetroBoot is updated.",
    "update.install": "The update is ready. Restart to update.",
    "update.check": "Software Update checked for updates.",
    "update.restart": "RetroBoot is restarting to finish the update.",
    "update.rollback": "RetroBoot is restarting with the previous version.",
}

# How a job kind is named to people (never the internal kind).
LABELS = {
    "guest.start": "Start Mac OS", "guest.restart": "Restart", "guest.shutdown": "Shut Down",
    "guest.force-stop": "Force Stop", "guest.force-restart": "Force Restart",
    "host.poweroff": "Turn Off", "host.reboot": "Restart RetroBoot",
    "update.install": "Software Update", "update.check": "Check for Updates",
    "update.restart": "Restart to Update", "update.rollback": "Go Back to the Previous Version",
}


class Busy(Exception):
    def __init__(self, job: "Job"):
        super().__init__(f"{job.kind} is in progress")
        self.job = job


class JobFailed(Exception):
    """Raise inside a job body with a user-facing message."""

    def __init__(self, message: str, result: Any = None, code: str = "failed"):
        super().__init__(message)
        self.message = message
        self.result = result
        self.code = code


@dataclass
class Job:
    id: str
    kind: str
    resources: tuple[str, ...]
    params: dict = field(default_factory=dict)
    state: str = QUEUED
    progress: float | None = None
    message: str = ""
    result: Any = None
    error: dict | None = None
    outcome: str | None = None     # final message on success (set by the body)
    created: str = field(default_factory=wall)
    started: str | None = None
    finished: str | None = None
    preemptible: bool = False
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def update(self, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, float(progress)))
        if message is not None:
            self.message = message

    def public(self) -> dict:
        err = None
        if self.error:
            err = dict(self.error, code=str(self.error.get("code", "failed")).replace("_", "-"))
        return {"id": self.id, "kind": self.kind, "state": self.state,
                "progress": None if self.progress is None else round(self.progress, 4),
                "message": self.message, "result": self.result, "error": err,
                "created": self.created, "started": self.started, "finished": self.finished,
                "created_at": self.created, "updated_at": self.finished or self.started or self.created}


Body = Callable[[Job], Awaitable[Any]]


class JobManager:
    def __init__(self, history: int = 200):
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.history = history
        self.listeners: list[Callable[[Job], None]] = []
        self.last_created = False   # False when start() returned an already-running job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.state in (QUEUED, RUNNING)]

    def holder(self, resource: str) -> Job | None:
        for j in self.active():
            if resource in j.resources:
                return j
        return None

    def start(self, kind: str, body: Body, resources: tuple[str, ...] = (), params: dict | None = None,
              preempts: tuple[str, ...] = (), preemptible: bool = False,
              dedupe: bool = True) -> Job:
        self.last_created = False
        if dedupe:
            for j in self.active():
                if j.kind == kind:
                    return j
        for r in resources:
            h = self.holder(r)
            if h is None:
                continue
            if h.preemptible and h.kind in preempts:
                self._cancel(h, f"Replaced by {LABELS.get(kind, 'another request')}.")
                continue
            raise Busy(h)
        job = Job(id=new_id(6), kind=kind, resources=tuple(resources), params=params or {},
                  preemptible=preemptible)
        self.jobs[job.id] = job
        self.order.append(job.id)
        self._trim()
        job.task = asyncio.get_running_loop().create_task(self._run(job, body), name=f"job-{kind}")
        self.last_created = True
        return job

    def _cancel(self, job: Job, message: str) -> None:
        if job.task and not job.task.done():
            job.task.cancel()
        job.state = FAILED
        job.error = {"code": "superseded", "message": message}
        job.message = message
        job.finished = wall()
        job.done.set()
        self._notify(job)

    async def _run(self, job: Job, body: Body) -> None:
        job.state = RUNNING
        job.started = wall()
        self._notify(job)
        try:
            result = await body(job)
            job.result = result
            job.state = SUCCEEDED
            job.progress = 1.0
            if job.outcome:
                job.message = job.outcome
            elif not job.message or job.message.endswith("…") or job.message == "Done.":
                job.message = OUTCOMES.get(job.kind, "Done.")
        except asyncio.CancelledError:
            if job.state != FAILED:
                job.state = FAILED
                job.error = {"code": "cancelled", "message": "Cancelled."}
            job.finished = job.finished or wall()
            job.done.set()
            self._notify(job)
            return
        except JobFailed as exc:
            job.state = FAILED
            job.error = {"code": exc.code, "message": exc.message}
            job.result = exc.result
            job.message = exc.message
        except Exception as exc:  # unexpected: keep details in logs, not the UI
            log.exception("job %s (%s) crashed", job.id, job.kind)
            job.state = FAILED
            job.error = {"code": "internal", "message": "The operation failed unexpectedly."}
            job.message = job.error["message"]
            job.result = {"exception": type(exc).__name__}
        job.finished = wall()
        job.done.set()
        self._notify(job)

    def _notify(self, job: Job) -> None:
        for fn in list(self.listeners):
            try:
                fn(job)
            except Exception:  # pragma: no cover
                log.exception("job listener failed")

    def _trim(self) -> None:
        while len(self.order) > self.history:
            old = self.order[0]
            j = self.jobs.get(old)
            if j and j.state in (QUEUED, RUNNING):
                break
            self.order.pop(0)
            self.jobs.pop(old, None)

    def list(self) -> list[dict]:
        return [self.jobs[i].public() for i in reversed(self.order) if i in self.jobs]
