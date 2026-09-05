"""Periodic sweeps.

Without these a job can occupy `running` forever: a worker that is killed mid
-flight never gets to write its own failure, so nothing else will either. The
reaper is the only thing standing between a crash and a project that appears
to be working on something indefinitely.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Job, JobStatus
from ..db.session import get_sessionmaker
from . import service as jobs

log = logging.getLogger("hbz.jobs.maintenance")

#: How long a job may sit in a non-terminal state before it is presumed dead.
#: Per kind, because a text call and a video poll have very different shapes.
STUCK_AFTER_S: dict[str, int] = {
    "story.analyze": 600,
    "storyboard.generate": 900,
    "asset.image": 600,
    "motion.submit": 300,
    "motion.poll": 2400,        # provider polling legitimately runs for ages
    "motion.download": 900,
    "render.preview": 1800,
    "render.final": 2400,
}
DEFAULT_STUCK_AFTER_S = 900


def stuck_after(kind: str) -> int:
    return STUCK_AFTER_S.get(kind, DEFAULT_STUCK_AFTER_S)


async def reap_stuck_jobs(session: AsyncSession | None = None) -> int:
    """Fail (or requeue) jobs whose worker died without recording an outcome.

    Requeues when attempts remain -- a killed worker is exactly the transient
    failure a retry is for -- and fails permanently once they are exhausted, so
    a job that reliably kills its worker cannot loop forever.
    """
    own_session = session is None
    maker = get_sessionmaker()
    session = session or maker()
    reaped = 0
    try:
        candidates = (await session.execute(
            select(Job).where(Job.status.in_(
                [JobStatus.RUNNING, JobStatus.AWAITING_PROVIDER])))).scalars().all()

        now = jobs.now()
        for job in candidates:
            started = job.started_at or job.queued_at
            if started is None:
                continue
            age = (now - started).total_seconds()
            limit = stuck_after(job.kind)
            if age < limit:
                continue

            retryable = job.attempt < job.max_attempts
            log.warning("reaping %s (%s) stuck %.0fs in %s; %s",
                        job.id, job.kind, age, job.status,
                        "requeueing" if retryable else "failing")
            await jobs.fail(
                session, job, "timeout",
                f"no worker reported an outcome after {age:.0f}s "
                f"(limit {limit}s for {job.kind}). The worker most likely died "
                f"mid-flight.",
                retryable=retryable)
            reaped += 1
    finally:
        if own_session:
            await session.close()
    return reaped


#: A row can sit in QUEUED with nothing working on it: the process died between
#: the commit and the enqueue, or a retryable failure requeued it without
#: pushing it back to the broker. The database is the source of truth and the
#: broker is a hint, so something has to reconcile them.
REQUEUE_AFTER_S = 60


async def requeue_stranded_jobs(session: AsyncSession | None = None) -> int:
    """Push QUEUED rows back to the broker if they have been waiting too long.

    Safe to do blindly: claim() is atomic, so a duplicate delivery finds
    nothing to take.
    """
    from . import get_queue

    own_session = session is None
    session = session or get_sessionmaker()()
    requeued = 0
    try:
        cutoff = jobs.now() - timedelta(seconds=REQUEUE_AFTER_S)
        stranded = (await session.execute(
            select(Job).where(Job.status == JobStatus.QUEUED,
                              Job.queued_at < cutoff))).scalars().all()
        queue = get_queue()
        for job in stranded:
            log.warning("requeueing stranded job %s (%s), queued %s",
                        job.id, job.kind, job.queued_at.isoformat())
            await queue.enqueue(job.kind, job.id, attempt=job.attempt)
            # Move the clock forward so a job the broker is genuinely holding
            # is not re-pushed every single minute.
            job.queued_at = jobs.now()
            requeued += 1
        if requeued:
            await session.commit()
    finally:
        if own_session:
            await session.close()
    return requeued


async def cron_reap_stuck_jobs(ctx) -> None:
    """arq cron entrypoint."""
    n = await reap_stuck_jobs()
    if n:
        log.info("reaped %d stuck job(s)", n)
    r = await requeue_stranded_jobs()
    if r:
        log.info("requeued %d stranded job(s)", r)
