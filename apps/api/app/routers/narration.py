"""Narration lines and renders."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, File, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..ai.registry import get_speech_port
from ..auth import CurrentUser, DbSession
from ..db.ids import uuid7
from ..db.ids import uuid7
from ..db.models import (Asset, AssetKind, AssetSource, JobStatus,
                         NarrationLine, Project, Render, RenderProfile, Scene,
                         Shot)
from ..errors import DomainError, NotFound
from ..jobs import get_queue
from ..jobs import service as jobs
from ..render.timeline import Profile
from ..schemas.api.story import JobAccepted
from ..services.narration_service import audio_is_fresh, evaluate_fit
from ..services.timeline_builder import build_timeline
from ..storage import asset_key, get_storage

router = APIRouter(prefix="/api/v1", tags=["narration"])


class LineUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=400)
    delivery: str | None = Field(default=None, max_length=16)


class RenderRequest(BaseModel):
    profile: str = Field(default="preview", pattern="^(preview|final)$")
    subtitles: bool = True


async def _owned(session, user, pid: uuid.UUID) -> Project:
    project = await session.get(Project, pid)
    if project is None or project.owner_id != user.id:
        raise NotFound("project not found")
    return project


@router.get("/voices")
async def voices(user: CurrentUser) -> dict:
    port = get_speech_port()
    return {"items": port.voices(), "model": getattr(port, "model", "unknown")}


@router.get("/projects/{project_id}/narration")
async def list_narration(project_id: uuid.UUID, session: DbSession,
                         user: CurrentUser) -> dict:
    await _owned(session, user, project_id)
    rows = (await session.execute(
        select(Shot, Scene).join(Scene, Scene.id == Shot.scene_id)
        .where(Shot.project_id == project_id)
        .order_by(Scene.sort_order, Shot.sort_order))).all()
    lines = (await session.execute(
        select(NarrationLine).where(NarrationLine.project_id == project_id)
        .order_by(NarrationLine.sort_order))).scalars().all()
    assets = {a.id: a for a in (await session.execute(
        select(Asset).where(Asset.id.in_(
            [l.audio_asset_id for l in lines if l.audio_asset_id] or
            [uuid.UUID(int=0)])))).scalars()}

    by_shot: dict[uuid.UUID, list[NarrationLine]] = {}
    by_scene: dict[uuid.UUID, list[NarrationLine]] = {}
    for l in lines:
        (by_shot.setdefault(l.shot_id, []) if l.shot_id
         else by_scene.setdefault(l.scene_id, [])).append(l)

    items = []
    for shot, scene in rows:
        mine = list(by_shot.get(shot.id, []))
        if shot.sort_order == 0:
            mine = by_scene.get(scene.id, []) + mine
        fit = evaluate_fit(shot, mine)
        items.append({
            "shot_id": str(shot.id), "scene_title": scene.title,
            "scene_index": scene.sort_order // 1000,
            "target_duration_s": float(shot.target_duration_s),
            "fit": {"status": str(fit.status), "message": fit.message(),
                    "words": fit.words, "word_budget": fit.word_budget,
                    "slack_ms": fit.slack_ms,
                    "tail_freeze_ms": fit.tail_freeze_ms,
                    "blocks_render": fit.blocks_render},
            "lines": [{
                "id": str(l.id), "text": l.text, "speaker": l.speaker_slug,
                "delivery": l.delivery, "duration_ms": l.duration_ms,
                "audio_url": (get_storage().url(assets[l.audio_asset_id].storage_key)
                              if l.audio_asset_id in assets else None),
                "fresh": audio_is_fresh(l, assets.get(l.audio_asset_id)),
            } for l in mine],
        })
    return {"items": items, "total": len(items)}


@router.patch("/narration-lines/{line_id}")
async def patch_line(line_id: uuid.UUID, body: LineUpdate, session: DbSession,
                     user: CurrentUser) -> dict:
    line = await session.get(NarrationLine, line_id)
    if line is None:
        raise NotFound("line not found")
    await _owned(session, user, line.project_id)
    for f, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(line, f, v)
    await session.flush()
    return {"id": str(line.id), "text": line.text, "delivery": line.delivery}


@router.post("/narration-lines/{line_id}/audio:generate",
             response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def generate_audio(line_id: uuid.UUID, session: DbSession,
                         user: CurrentUser) -> JobAccepted:
    line = await session.get(NarrationLine, line_id)
    if line is None:
        raise NotFound("line not found")
    project = await _owned(session, user, line.project_id)
    job, created = await jobs.enqueue(
        session, project_id=project.id, kind="narration.tts",
        input_hash=f"{line.id}:{hash(line.text)}:{line.delivery}",
        target_type="narration_line", target_id=line.id)
    await session.commit()
    if created or job.status == JobStatus.QUEUED:
        await get_queue().enqueue("narration.tts", job.id,
                                  attempt=job.attempt)
    return JobAccepted(job_id=job.id, kind="narration.tts",
                       status=str(job.status), created=created)


@router.post("/projects/{project_id}/narration:generate_all")
async def generate_all(project_id: uuid.UUID, session: DbSession,
                       user: CurrentUser) -> dict:
    project = await _owned(session, user, project_id)
    lines = (await session.execute(
        select(NarrationLine).where(NarrationLine.project_id == project_id)
        .order_by(NarrationLine.sort_order))).scalars().all()
    queued = []
    for line in lines:
        job, created = await jobs.enqueue(
            session, project_id=project.id, kind="narration.tts",
            input_hash=f"{line.id}:{hash(line.text)}:{line.delivery}",
            target_type="narration_line", target_id=line.id)
        if created or job.status == JobStatus.QUEUED:
            queued.append((job.kind, job.id, job.attempt))
    await session.commit()
    for kind, jid, attempt in queued:
        await get_queue().enqueue(kind, jid, attempt=attempt)
    return {"queued": len(queued), "lines": len(lines)}


# ---- music ----------------------------------------------------------------
MAX_MUSIC_BYTES = 30 * 1024 * 1024


@router.post("/projects/{project_id}/music")
async def upload_music(project_id: uuid.UUID, session: DbSession,
                       user: CurrentUser, file: UploadFile = File(...)) -> dict:
    """Attach a music bed.

    Mixed roughly 24dB under the narration, with fades at both ends. Bring your
    own file: generating music is out of scope, and a licence you already hold
    is the only kind worth shipping in a gift.
    """
    project = await _owned(session, user, project_id)
    data = await file.read()
    if not data:
        raise DomainError("the uploaded file is empty", code="validation_failed")
    if len(data) > MAX_MUSIC_BYTES:
        raise DomainError(
            f"file is larger than {MAX_MUSIC_BYTES // 1024 // 1024}MB",
            code="validation_failed")
    if not (file.content_type or "").startswith("audio/"):
        raise DomainError(f"expected audio, got {file.content_type or 'nothing'}",
                          code="validation_failed")

    aid = uuid7()
    ext = (file.filename or "bed.mp3").rsplit(".", 1)[-1][:6] or "mp3"
    key = asset_key(project.id, "music", aid, ext)
    blob = await get_storage().put(key, data)
    session.add(Asset(
        id=aid, project_id=project.id, kind=AssetKind.AUDIO,
        source=AssetSource.MANUAL, storage_key=key,
        mime=file.content_type or "audio/mpeg", bytes=blob.bytes,
        checksum=blob.checksum,
        params={"filename": file.filename, "role": "music"}))
    project.music_track_key = key
    await session.flush()
    return {"attached": True, "filename": file.filename,
            "bytes": blob.bytes, "url": get_storage().url(key)}


@router.delete("/projects/{project_id}/music")
async def remove_music(project_id: uuid.UUID, session: DbSession,
                       user: CurrentUser) -> dict:
    project = await _owned(session, user, project_id)
    project.music_track_key = None
    await session.flush()
    return {"attached": False}


@router.get("/projects/{project_id}/music")
async def get_music(project_id: uuid.UUID, session: DbSession,
                    user: CurrentUser) -> dict:
    project = await _owned(session, user, project_id)
    if not project.music_track_key:
        return {"attached": False, "url": None, "filename": None}
    asset = (await session.execute(
        select(Asset).where(Asset.storage_key == project.music_track_key)
    )).scalar_one_or_none()
    return {"attached": True,
            "url": get_storage().url(project.music_track_key),
            "filename": (asset.params or {}).get("filename") if asset else None,
            "bytes": asset.bytes if asset else None}


# ---- renders --------------------------------------------------------------
@router.post("/projects/{project_id}/preflight")
async def preflight(project_id: uuid.UUID, body: RenderRequest,
                    session: DbSession, user: CurrentUser) -> dict:
    project = await _owned(session, user, project_id)
    result = await build_timeline(session, project,
                                  profile=Profile(body.profile),
                                  subtitles=body.subtitles)
    return {
        "ok": result.ok,
        "blocking": [{"code": p.code, "message": p.message, "shot_id": p.shot_id}
                     for p in result.blocking],
        "advisory": [{"code": p.code, "message": p.message, "shot_id": p.shot_id}
                     for p in result.advisory],
        "duration_ms": result.timeline.total_duration_ms if result.timeline else None,
        "clips": len(result.timeline.clips) if result.timeline else 0,
    }


@router.post("/projects/{project_id}/renders", response_model=JobAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def create_render(project_id: uuid.UUID, body: RenderRequest,
                        session: DbSession, user: CurrentUser) -> JobAccepted:
    project = await _owned(session, user, project_id)
    result = await build_timeline(session, project,
                                  profile=Profile(body.profile),
                                  subtitles=body.subtitles)
    if not result.ok:
        # Refused before a job exists: a six-minute render that ends in a black
        # frame is worse than a clear refusal now.
        raise DomainError(
            "; ".join(p.message for p in result.blocking) or "nothing to render",
            code="preflight_failed", status_code=409,
            blocking=[p.__dict__ for p in result.blocking])

    timeline = result.timeline
    render = Render(id=uuid7(), project_id=project.id,
                    profile=RenderProfile(body.profile),
                    timeline=timeline.model_dump(mode="json"),
                    timeline_hash=timeline.hash(), status="queued")
    session.add(render)
    await session.flush()

    job, created = await jobs.enqueue(
        session, project_id=project.id, kind="render.preview",
        input_hash=timeline.hash(), target_type="render", target_id=render.id)
    await session.commit()
    if created or job.status == JobStatus.QUEUED:
        await get_queue().enqueue("render.preview", job.id,
                                  attempt=job.attempt)
    return JobAccepted(job_id=job.id, kind="render.preview",
                       status=str(job.status), created=created)


@router.get("/projects/{project_id}/renders")
async def list_renders(project_id: uuid.UUID, session: DbSession,
                       user: CurrentUser) -> dict:
    await _owned(session, user, project_id)
    rows = (await session.execute(
        select(Render).where(Render.project_id == project_id)
        .order_by(Render.created_at.desc()).limit(20))).scalars().all()
    storage = get_storage()
    out = []
    for r in rows:
        video = await session.get(Asset, r.video_asset_id) if r.video_asset_id else None
        poster = await session.get(Asset, r.poster_asset_id) if r.poster_asset_id else None
        out.append({
            "id": str(r.id), "profile": str(r.profile), "status": r.status,
            "duration_ms": r.duration_ms, "error": r.error,
            "created_at": r.created_at.isoformat(),
            "video_url": storage.url(video.storage_key) if video else None,
            "poster_url": storage.url(poster.storage_key) if poster else None,
            "clips": len(r.timeline.get("clips", [])),
        })
    return {"items": out, "total": len(out)}
