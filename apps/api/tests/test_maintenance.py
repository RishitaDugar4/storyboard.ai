"""The reaper: the only thing that recovers a job whose worker died."""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

os.environ.setdefault("SESSION_SECRET", "test-secret")

from app.auth import create_user                       # noqa: E402
from app.db.ids import uuid7                            # noqa: E402
from app.db.models import Job, JobStatus, Project, User  # noqa: E402
from app.db.session import (dispose_engine, get_engine,  # noqa: E402
                            get_sessionmaker)
from app.jobs import service as jobs                     # noqa: E402
from app.jobs.maintenance import (DEFAULT_STUCK_AFTER_S, reap_stuck_jobs,  # noqa: E402
                                  stuck_after)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def project_id():
    async with get_sessionmaker()() as s:
        user = (await s.execute(
            select(User).where(User.email == "reap@local"))).scalar_one_or_none()
        if user is None:
            user = await create_user(s, email="reap@local", display_name="Reap",
                                     passphrase="x")
        p = Project(id=uuid7(), owner_id=user.id, title="reaper")
        s.add(p)
        await s.commit()
        pid = p.id
    yield pid
    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM jobs"))
        await s.execute(text("DELETE FROM projects"))
        await s.commit()
    await dispose_engine()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def _stuck_job(project_id, *, kind="story.analyze", age_s: int,
                     attempt: int = 1, max_attempts: int = 3,
                     status=JobStatus.RUNNING) -> uuid.UUID:
    async with get_sessionmaker()() as s:
        job = Job(id=uuid7(), project_id=project_id, kind=kind, status=status,
                  idempotency_key=uuid7().hex, attempt=attempt,
                  max_attempts=max_attempts, queued_at=jobs.now(),
                  started_at=jobs.now() - timedelta(seconds=age_s))
        s.add(job)
        await s.commit()
        return job.id


async def _status(job_id) -> Job:
    async with get_sessionmaker()() as s:
        return await s.get(Job, job_id)


def test_timeouts_are_per_kind():
    # A provider poll legitimately runs far longer than a text call; one global
    # timeout would either kill polls or never reap anything else.
    assert stuck_after("motion.poll") > stuck_after("story.analyze")
    assert stuck_after("something.unknown") == DEFAULT_STUCK_AFTER_S


async def test_a_fresh_running_job_is_left_alone(project_id):
    jid = await _stuck_job(project_id, age_s=5)
    assert await reap_stuck_jobs() == 0
    assert (await _status(jid)).status is JobStatus.RUNNING


async def test_a_stuck_job_is_requeued_while_attempts_remain(project_id):
    jid = await _stuck_job(project_id, age_s=10_000, attempt=1, max_attempts=3)
    assert await reap_stuck_jobs() == 1
    job = await _status(jid)
    # A killed worker is exactly the transient failure a retry exists for.
    assert job.status is JobStatus.QUEUED
    assert job.error_code == "timeout"
    assert "died mid-flight" in job.error_detail


async def test_a_stuck_job_fails_once_attempts_are_exhausted(project_id):
    jid = await _stuck_job(project_id, age_s=10_000, attempt=3, max_attempts=3)
    assert await reap_stuck_jobs() == 1
    job = await _status(jid)
    # Otherwise a job that reliably kills its worker loops forever.
    assert job.status is JobStatus.FAILED
    assert job.finished_at is not None


async def test_awaiting_provider_is_also_reaped(project_id):
    jid = await _stuck_job(project_id, kind="motion.poll", age_s=99_999,
                           status=JobStatus.AWAITING_PROVIDER, attempt=3,
                           max_attempts=3)
    assert await reap_stuck_jobs() == 1
    assert (await _status(jid)).status is JobStatus.FAILED


async def test_terminal_jobs_are_never_touched(project_id):
    async with get_sessionmaker()() as s:
        job = Job(id=uuid7(), project_id=project_id, kind="story.analyze",
                  status=JobStatus.SUCCEEDED, idempotency_key=uuid7().hex,
                  queued_at=jobs.now(),
                  started_at=jobs.now() - timedelta(days=2))
        s.add(job)
        await s.commit()
        jid = job.id
    assert await reap_stuck_jobs() == 0
    assert (await _status(jid)).status is JobStatus.SUCCEEDED


async def test_reaping_is_idempotent(project_id):
    await _stuck_job(project_id, age_s=10_000, attempt=3, max_attempts=3)
    assert await reap_stuck_jobs() == 1
    assert await reap_stuck_jobs() == 0      # nothing left in a live state
