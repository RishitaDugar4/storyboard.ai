"""Job status and the live event stream."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..auth import CurrentUser, DbSession
from ..db.models import ACTIVE, Job, JobEvent, JobStatus, Project
from ..errors import NotFound
from ..jobs import get_queue
from ..jobs import service as jobs
from ..jobs.events import Event, get_bus

router = APIRouter(prefix="/api/v1", tags=["jobs"])

#: A comment-only line keeps proxies and browsers from closing an idle stream.
HEARTBEAT_S = 20


def _job_dict(j: Job) -> dict:
    return {"id": str(j.id), "kind": j.kind, "status": str(j.status),
            "progress": j.progress, "message": j.message,
            "attempt": j.attempt, "target_type": j.target_type,
            "target_id": str(j.target_id) if j.target_id else None,
            "error_code": j.error_code, "error_detail": j.error_detail,
            "result": j.result,
            "queued_at": j.queued_at.isoformat() if j.queued_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


async def _owned_job(session, user, job_id: uuid.UUID) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFound("job not found")
    project = await session.get(Project, job.project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("job not found")
    return job


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, session: DbSession,
                  user: CurrentUser) -> dict:
    return _job_dict(await _owned_job(session, user, job_id))


@router.get("/jobs/{job_id}/events")
async def job_log(job_id: uuid.UUID, session: DbSession,
                  user: CurrentUser) -> dict:
    await _owned_job(session, user, job_id)
    rows = (await session.execute(
        select(JobEvent).where(JobEvent.job_id == job_id)
        .order_by(JobEvent.at))).scalars().all()
    return {"items": [{"at": e.at.isoformat(), "level": e.level,
                       "message": e.message, "data": e.data} for e in rows]}


@router.get("/projects/{project_id}/jobs")
async def list_jobs(project_id: uuid.UUID, session: DbSession,
                    user: CurrentUser,
                    status: str | None = Query(default=None),
                    limit: int = Query(default=50, le=200)) -> dict:
    project = await session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("project not found")
    q = select(Job).where(Job.project_id == project_id)
    if status == "active":
        q = q.where(Job.status.in_(list(ACTIVE)))
    elif status:
        q = q.where(Job.status == JobStatus(status))
    rows = (await session.execute(
        q.order_by(Job.queued_at.desc()).limit(limit))).scalars().all()
    return {"items": [_job_dict(j) for j in rows], "total": len(rows)}


@router.post("/jobs/{job_id}:retry")
async def retry_job(job_id: uuid.UUID, session: DbSession,
                    user: CurrentUser) -> dict:
    job = await _owned_job(session, user, job_id)
    if job.status in ACTIVE:
        return {"requeued": False, "reason": "job is still active"}
    job.status = JobStatus.QUEUED
    job.error_code = job.error_detail = None
    job.finished_at = None
    job.attempt = 0                       # an explicit retry is a fresh start
    job.message = "requeued"
    job.queued_at = jobs.now()
    await session.commit()
    await get_queue().enqueue(job.kind, job.id)
    return {"requeued": True, "job_id": str(job.id)}


@router.post("/jobs/{job_id}:cancel")
async def cancel_job(job_id: uuid.UUID, session: DbSession,
                     user: CurrentUser) -> dict:
    job = await _owned_job(session, user, job_id)
    if job.status not in ACTIVE:
        return {"cancelled": False, "reason": f"job is already {job.status}"}
    job.status = JobStatus.CANCELLED
    job.finished_at = jobs.now()
    job.message = "cancelled"
    await session.commit()
    return {"cancelled": True}


@router.get("/projects/{project_id}/events")
async def stream(project_id: uuid.UUID, session: DbSession,
                 user: CurrentUser) -> StreamingResponse:
    """Server-sent events for one project.

    SSE rather than WebSockets: the traffic is one-directional, it survives
    proxies, and the browser reconnects on its own. Events carry invalidations
    rather than entity bodies, so a dropped message costs a refetch and never
    leaves the client holding a stale copy it believes is fresh.
    """
    project = await session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("project not found")

    bus = get_bus()

    async def gen():
        # Subscribe before the first yield so no event is missed in the gap
        # between the client connecting and the loop starting.
        async with bus.subscribe(str(project_id)) as sub:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(sub.__anext__(),
                                                   timeout=HEARTBEAT_S)
                    yield event.sse()
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                except (asyncio.CancelledError, StopAsyncIteration):
                    break

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",     # nginx/proxies must not buffer a stream
    })
