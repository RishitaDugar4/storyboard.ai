"""Job lifecycle: enqueue, claim, progress, settle.

Two properties do the heavy lifting:

* **Idempotent enqueue.** `idempotency_key = sha256(kind|target|input_hash)` is
  UNIQUE, so asking for the same work twice returns the first job instead of
  paying twice. At image and clip prices that is a financial control, not an
  optimisation.

* **Atomic claim.** Status moves queued -> running in a single conditional
  UPDATE, so at-least-once delivery from the broker is harmless: the second
  delivery finds nothing to claim and returns.
"""
from __future__ import annotations

import hashlib
import logging
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.ids import uuid7
from ..db.models import ACTIVE, Job, JobEvent, JobStatus
from .events import Event, get_bus

log = logging.getLogger("hbz.jobs")


def now() -> datetime:
    return datetime.now(timezone.utc)


def idempotency_key(project_id: uuid.UUID, kind: str,
                    target_id: uuid.UUID | None, input_hash: str) -> str:
    """Identity of a unit of work.

    The project MUST be part of it. Without it, two projects whose inputs hash
    the same -- the same story pasted twice, the same prompt for the same stock
    shot -- collide, and the second silently receives the first one's job and is
    never processed at all.
    """
    return hashlib.sha256(
        f"{project_id}|{kind}|{target_id or '-'}|{input_hash}".encode()
    ).hexdigest()


@asynccontextmanager
async def _publish_job(job: Job):
    yield
    await get_bus().publish(Event(
        type="job", project_id=str(job.project_id),
        data={"job_id": str(job.id), "kind": job.kind,
              "target_type": job.target_type,
              "target_id": str(job.target_id) if job.target_id else None,
              "status": str(job.status), "progress": job.progress,
              "message": job.message}))


async def enqueue(
    session: AsyncSession, *, project_id: uuid.UUID, kind: str,
    input_hash: str, target_type: str | None = None,
    target_id: uuid.UUID | None = None, payload: dict | None = None,
    parent_job_id: uuid.UUID | None = None, max_attempts: int = 3,
) -> tuple[Job, bool]:
    """Insert a job unless the identical work already exists.

    Returns (job, created). `created=False` means an identical job is already
    queued, running, or finished -- the caller should surface that one rather
    than making a second.
    """
    key = idempotency_key(project_id, kind, target_id, input_hash)
    stmt = (insert(Job)
            .values(id=uuid7(), project_id=project_id, kind=kind,
                    target_type=target_type, target_id=target_id,
                    parent_job_id=parent_job_id, status=JobStatus.QUEUED,
                    idempotency_key=key, payload=payload or {},
                    max_attempts=max_attempts, queued_at=now())
            .on_conflict_do_nothing(index_elements=[Job.idempotency_key])
            .returning(Job.id))
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is None:
        existing = (await session.execute(
            select(Job).where(Job.idempotency_key == key))).scalar_one()
        return existing, False

    job = await session.get(Job, new_id)
    async with _publish_job(job):
        pass
    return job, True


async def claim(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    """queued -> running, atomically. None means someone else has it."""
    claimed = (await session.execute(
        update(Job)
        .where(Job.id == job_id,
               Job.status.in_([JobStatus.QUEUED, JobStatus.AWAITING_PROVIDER]))
        .values(status=JobStatus.RUNNING, started_at=now(),
                attempt=Job.attempt + 1)
        .returning(Job.id))).scalar_one_or_none()
    if claimed is None:
        return None
    await session.commit()
    return await session.get(Job, job_id)


async def progress(session: AsyncSession, job: Job, pct: int, message: str,
                   *, level: str = "info", data: dict | None = None) -> None:
    job.progress = max(0, min(100, pct))
    job.message = message[:200]
    session.add(JobEvent(job_id=job.id, at=now(), level=level,
                         message=message, data=data))
    await session.commit()
    async with _publish_job(job):
        pass


async def succeed(session: AsyncSession, job: Job,
                  result: dict | None = None) -> None:
    job.status, job.result = JobStatus.SUCCEEDED, result or {}
    job.progress, job.finished_at = 100, now()
    job.message = "done"
    await session.commit()
    async with _publish_job(job):
        pass


async def fail(session: AsyncSession, job: Job, code: str, detail: str,
               *, retryable: bool = False) -> None:
    """Record a failure, and requeue only if attempts remain and it is the
    kind of error that a retry could plausibly fix."""
    job.error_code, job.error_detail = code[:64], detail[:4000]
    if retryable and job.attempt < job.max_attempts:
        job.status, job.message = JobStatus.QUEUED, f"retrying after {code}"
    else:
        job.status, job.finished_at = JobStatus.FAILED, now()
        job.message = f"failed: {code}"
    session.add(JobEvent(job_id=job.id, at=now(), level="error",
                         message=f"{code}: {detail[:500]}"))
    await session.commit()
    async with _publish_job(job):
        pass


#: Attempts granted by an explicit retry, on top of whatever the job has
#: already spent. Budget, not history: the attempt counter has to keep rising
#: or the broker cannot tell one delivery from another (see queue.py).
RETRY_ATTEMPTS = 3


def revive(job: Job) -> bool:
    """Reset a terminal job so the same request can run it again.

    Returns False for a job that is already queued or running -- a second
    click should join the work in flight, not restart it.

    The attempt counter is deliberately left alone and the budget extended
    instead. Zeroing it would read as a fresh start, but it would also hand
    the broker a key it has already seen, and the requeue would be accepted
    here and dropped there.
    """
    if job.status in ACTIVE:
        return False
    job.status = JobStatus.QUEUED
    job.error_code = job.error_detail = None
    job.finished_at = None
    job.max_attempts = job.attempt + RETRY_ATTEMPTS
    job.message = "requeued"
    job.queued_at = now()
    return True


async def notify_entity(project_id: uuid.UUID, entity: str,
                        entity_id: uuid.UUID | None, reason: str) -> None:
    """Tell listeners something changed so they refetch it."""
    await get_bus().publish(Event(
        type="entity", project_id=str(project_id),
        data={"type": entity, "id": str(entity_id) if entity_id else None,
              "reason": reason}))


@asynccontextmanager
async def running(session: AsyncSession, job_id: uuid.UUID):
    """Claim a job and guarantee it reaches a terminal state.

    An unhandled exception inside a handler must never leave a job stuck in
    `running` -- that is the state nothing ever cleans up.
    """
    job = await claim(session, job_id)
    if job is None:
        log.info("job %s was not claimable (already taken or terminal)", job_id)
        yield None
        return
    try:
        yield job
    except Exception as exc:                        # noqa: BLE001
        log.exception("job %s (%s) failed", job_id, job.kind)
        await session.rollback()
        job = await session.get(Job, job_id)
        await fail(session, job, type(exc).__name__,
                   f"{exc}\n{traceback.format_exc()[-1500:]}")
        raise
