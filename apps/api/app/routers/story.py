"""Story intake, analysis, and storyboard generation."""
from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, select

from ..auth import CurrentUser, DbSession
from ..db.ids import uuid7
from ..db.models import (Job, JobStatus, Project, StoryAnalysisDoc,
                         StoryboardDoc, StoryInput)
from ..errors import DomainError, NotFound, StagePreconditionFailed
from ..jobs import get_queue
from ..jobs import service as jobs
from ..schemas.api.story import (JobAccepted, StoryboardApplyRequest,
                                 StoryboardGenerateRequest, StoryRead,
                                 StoryWrite)
from ..services.materialize import ApplyRefused, apply_storyboard
from ..services.story_service import MAX_STORY_WORDS

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["story"])


async def _owned(session: DbSession, user, pid: uuid.UUID) -> Project:
    project = await session.get(Project, pid)
    if project is None or project.owner_id != user.id:
        raise NotFound(f"project {pid} not found")
    return project


async def _accept(session, project: Project, kind: str, input_hash: str,
                  payload: dict | None = None) -> JobAccepted:
    job, created = await jobs.enqueue(
        session, project_id=project.id, kind=kind, input_hash=input_hash,
        payload=payload or {})
    await session.commit()
    # Push to the broker whenever the row is still QUEUED, not only when it was
    # just created. A crash between the commit and the enqueue would otherwise
    # strand the job forever, and re-enqueuing is harmless: claim() is atomic,
    # so a duplicate delivery finds nothing to take.
    if created or job.status == JobStatus.QUEUED:
        await get_queue().enqueue(kind, job.id)
    return JobAccepted(job_id=job.id, kind=kind, status=str(job.status),
                       created=created)


@router.put("/story", response_model=StoryRead)
async def put_story(project_id: uuid.UUID, body: StoryWrite,
                    session: DbSession, user: CurrentUser) -> StoryRead:
    project = await _owned(session, user, project_id)
    text = body.raw_text.strip()
    words = len(text.split())
    if words > MAX_STORY_WORDS:
        raise DomainError(
            f"story is {words} words; the limit is {MAX_STORY_WORDS}",
            code="validation_failed")

    version = ((await session.execute(
        select(func.coalesce(func.max(StoryInput.version), 0))
        .where(StoryInput.project_id == project.id))).scalar_one()) + 1

    story = StoryInput(id=uuid7(), project_id=project.id, version=version,
                       raw_text=text, word_count=words,
                       text_hash=hashlib.sha256(text.encode()).hexdigest())
    session.add(story)
    await session.flush()
    return StoryRead.model_validate(story)


@router.get("/story", response_model=StoryRead)
async def get_story(project_id: uuid.UUID, session: DbSession,
                    user: CurrentUser) -> StoryRead:
    await _owned(session, user, project_id)
    story = (await session.execute(
        select(StoryInput).where(StoryInput.project_id == project_id)
        .order_by(StoryInput.version.desc()).limit(1))).scalar_one_or_none()
    if story is None:
        raise NotFound("no story has been saved for this project")
    return StoryRead.model_validate(story)


@router.post("/story:analyze", response_model=JobAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def analyze(project_id: uuid.UUID, session: DbSession,
                  user: CurrentUser) -> JobAccepted:
    project = await _owned(session, user, project_id)
    story = (await session.execute(
        select(StoryInput).where(StoryInput.project_id == project_id)
        .order_by(StoryInput.version.desc()).limit(1))).scalar_one_or_none()
    if story is None:
        raise StagePreconditionFailed("save a story before analysing it")
    return await _accept(session, project, "story.analyze", story.text_hash)


@router.get("/analysis")
async def get_analysis(project_id: uuid.UUID, session: DbSession,
                       user: CurrentUser) -> dict:
    await _owned(session, user, project_id)
    doc = (await session.execute(
        select(StoryAnalysisDoc).where(StoryAnalysisDoc.project_id == project_id)
        .order_by(StoryAnalysisDoc.created_at.desc()).limit(1))).scalar_one_or_none()
    if doc is None:
        raise NotFound("this project has not been analysed yet")
    return {"id": str(doc.id), "created_at": doc.created_at.isoformat(),
            "model": doc.model, "repaired": doc.repaired,
            "document": doc.document}


@router.post("/storyboard:generate", response_model=JobAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def generate(project_id: uuid.UUID, body: StoryboardGenerateRequest,
                   session: DbSession, user: CurrentUser) -> JobAccepted:
    project = await _owned(session, user, project_id)
    analysis = (await session.execute(
        select(StoryAnalysisDoc).where(StoryAnalysisDoc.project_id == project_id)
        .order_by(StoryAnalysisDoc.created_at.desc()).limit(1))).scalar_one_or_none()
    if analysis is None:
        raise StagePreconditionFailed(
            "analyse the story before generating a storyboard")
    # Regenerating with different direction is different work, so it gets a
    # different idempotency key rather than returning the previous job.
    seed = f"{analysis.id}|{body.target_length_s}|{body.notes}"
    return await _accept(session, project, "storyboard.generate",
                         hashlib.sha256(seed.encode()).hexdigest(),
                         {"target_length_s": body.target_length_s,
                          "notes": body.notes})


@router.get("/storyboards")
async def list_storyboards(project_id: uuid.UUID, session: DbSession,
                           user: CurrentUser) -> dict:
    await _owned(session, user, project_id)
    rows = (await session.execute(
        select(StoryboardDoc).where(StoryboardDoc.project_id == project_id)
        .order_by(StoryboardDoc.version.desc()))).scalars().all()
    return {"items": [{"id": str(r.id), "version": r.version,
                       "model": r.model, "repaired": r.repaired,
                       "applied_at": r.applied_at.isoformat() if r.applied_at else None,
                       "scenes": len(r.document.get("scenes", [])),
                       "created_at": r.created_at.isoformat()} for r in rows],
            "total": len(rows)}


@router.get("/storyboards/{storyboard_id}")
async def get_storyboard(project_id: uuid.UUID, storyboard_id: uuid.UUID,
                         session: DbSession, user: CurrentUser) -> dict:
    await _owned(session, user, project_id)
    doc = await session.get(StoryboardDoc, storyboard_id)
    if doc is None or doc.project_id != project_id:
        raise NotFound("storyboard not found")
    return {"id": str(doc.id), "version": doc.version, "document": doc.document}


@router.post("/storyboards/{storyboard_id}:apply")
async def apply(project_id: uuid.UUID, storyboard_id: uuid.UUID,
                body: StoryboardApplyRequest, session: DbSession,
                user: CurrentUser) -> dict:
    await _owned(session, user, project_id)
    doc = await session.get(StoryboardDoc, storyboard_id)
    if doc is None or doc.project_id != project_id:
        raise NotFound("storyboard not found")
    try:
        result = await apply_storyboard(session, doc, force=body.force)
    except ApplyRefused as exc:
        raise DomainError(str(exc), code="apply_would_destroy_work",
                          status_code=status.HTTP_409_CONFLICT,
                          **exc.counts) from exc
    doc.applied_at = jobs.now()
    await jobs.notify_entity(project_id, "storyboard", doc.id, "applied")
    return {"applied": True, **result.__dict__}
