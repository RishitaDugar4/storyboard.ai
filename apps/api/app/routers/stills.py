"""Shots and stills: generate candidates, approve one, or upload your own."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, File, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import CurrentUser, DbSession
from ..db.ids import uuid7
from ..db.models import (Asset, AssetKind, AssetSource, Character,
                         JobStatus, Project, Scene, Shot)
from ..errors import DomainError, NotFound, StagePreconditionFailed
from ..jobs import get_queue
from ..jobs import service as jobs
from ..ai.registry import get_image_port
from ..schemas.api.story import JobAccepted
from ..services.still_service import (BudgetExceeded, PriceUnknown,
                                       cached_asset, check_budget, plan_still,
                                       recompute_image_hash, still_is_fresh)
from ..storage import asset_key, get_storage

router = APIRouter(prefix="/api/v1", tags=["stills"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class ShotUpdate(BaseModel):
    """Editing a shot without regenerating the whole storyboard.

    Changing `action` alters the composed prompt, which makes any existing
    still stale -- correctly, and visibly, rather than silently.
    """

    action: str | None = Field(default=None, min_length=1, max_length=400)
    composition_note: str | None = Field(default=None, max_length=240)
    camera_move: str | None = None
    subject_motion: str | None = Field(default=None, max_length=300)
    target_duration_s: float | None = Field(default=None, ge=2.5, le=12.0)
    motion_priority: str | None = Field(default=None, pattern="^(low|medium|high)$")
    subject_slugs: list[str] | None = Field(default=None, max_length=3)
    prompt_override: str | None = Field(default=None, max_length=2000)


class GenerateStill(BaseModel):
    n: int = Field(default=2, ge=1, le=4)
    prompt_override: str | None = Field(default=None, max_length=2000)


async def _owned_shot(session, user, shot_id: uuid.UUID) -> tuple[Shot, Project]:
    shot = await session.get(Shot, shot_id)
    if shot is None:
        raise NotFound("shot not found")
    project = await session.get(Project, shot.project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("shot not found")
    return shot, project


def _asset_read(a: Asset, selected: bool) -> dict:
    return {"id": str(a.id), "url": get_storage().url(a.storage_key),
            "width": a.width, "height": a.height, "source": str(a.source),
            "provider": a.provider, "model": a.model,
            "cost_cents": float(a.cost_cents), "selected": selected,
            "created_at": a.created_at.isoformat()}


@router.get("/projects/{project_id}/shots")
async def list_shots(project_id: uuid.UUID, session: DbSession,
                     user: CurrentUser) -> dict:
    project = await session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("project not found")

    # Ordered by scene first: shot.sort_order is assigned within its scene, so
    # with one shot per scene every value is 0 and ordering by it alone is
    # arbitrary -- which silently shuffles the whole film in the UI.
    rows = (await session.execute(
        select(Shot, Scene).join(Scene, Scene.id == Shot.scene_id)
        .where(Shot.project_id == project_id)
        .order_by(Scene.sort_order, Shot.sort_order))).all()
    shots = [r[0] for r in rows]
    scenes = {r[1].id: r[1] for r in rows}

    items = []
    for sh in shots:
        asset = (await session.get(Asset, sh.selected_image_id)
                 if sh.selected_image_id else None)
        scene = scenes.get(sh.scene_id)
        items.append({
            "id": str(sh.id), "scene_title": scene.title if scene else "",
            "scene_index": scene.sort_order // 1000 if scene else 0,
            "shot_type": str(sh.shot_type), "camera_move": str(sh.camera_move),
            "action": sh.action, "subject_slugs": sh.subject_slugs,
            "motion_priority": sh.motion_priority,
            "target_duration_s": float(sh.target_duration_s),
            "motion_mode": str(sh.motion_mode),
            "still": _asset_read(asset, True) if asset else None,
            # Derived, never stored: a still is stale exactly when the prompt
            # that would produce it no longer matches the one that did.
            "still_fresh": still_is_fresh(sh, asset),
        })
    return {"items": items, "total": len(items)}


@router.patch("/shots/{shot_id}")
async def patch_shot(shot_id: uuid.UUID, body: ShotUpdate, session: DbSession,
                     user: CurrentUser) -> dict:
    shot, project = await _owned_shot(session, user, shot_id)
    changes = body.model_dump(exclude_unset=True)

    if (slugs := changes.get("subject_slugs")) is not None:
        known = {c.slug for c in (await session.execute(
            select(Character).where(Character.project_id == project.id))).scalars()}
        if unknown := set(slugs) - known:
            raise DomainError(
                f"unknown character slug(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known)) or 'none'}.",
                code="validation_failed")

    for field, value in changes.items():
        if value is not None or field == "prompt_override":
            setattr(shot, field, value)
    await session.flush()

    # The prompt may have changed, so the shot's idea of "current" must be
    # refreshed before freshness is reported.
    await recompute_image_hash(session, shot, project)
    await session.flush()

    asset = (await session.get(Asset, shot.selected_image_id)
             if shot.selected_image_id else None)
    await jobs.notify_entity(project.id, "shot", shot.id, "edited")
    return {"id": str(shot.id), "action": shot.action,
            "camera_move": str(shot.camera_move),
            "target_duration_s": float(shot.target_duration_s),
            "motion_priority": shot.motion_priority,
            "subject_slugs": shot.subject_slugs,
            "still_fresh": still_is_fresh(shot, asset)}


@router.get("/shots/{shot_id}/prompt")
async def get_prompt(shot_id: uuid.UUID, session: DbSession,
                     user: CurrentUser) -> dict:
    """The exact composed prompt, with each fragment's origin.

    The most useful debugging surface in the app: when a still comes back
    wrong, this shows whether the style bible, the character canon or the shot
    action is responsible.
    """
    shot, project = await _owned_shot(session, user, shot_id)
    port = get_image_port()
    plan = await plan_still(session, shot, project,
                            provider=getattr(port, "provider", "fake"),
                            model=getattr(port, "model", "unknown"),
                            cost_per_image_cents=getattr(
                                port, "cost_per_image_cents", None))
    return {"positive": plan.prompt.positive, "negative": plan.prompt.negative,
            "fragments": [{"origin": o, "text": t} for o, t in plan.prompt.fragments],
            "size": plan.size, "seed": plan.seed, "model": plan.model,
            "input_hash": plan.input_hash,
            "characters": plan.characters,
            "estimated_cost_cents": round(plan.estimated_cost_cents, 2),
            "current_hash": shot.image_input_hash,
            "would_reuse_cache": bool(
                await cached_asset(session, project.id, plan.input_hash))}


@router.get("/shots/{shot_id}/images")
async def list_images(shot_id: uuid.UUID, session: DbSession,
                      user: CurrentUser) -> dict:
    shot, project = await _owned_shot(session, user, shot_id)
    rows = (await session.execute(
        select(Asset).where(Asset.project_id == project.id,
                            Asset.kind == AssetKind.IMAGE)
        .order_by(Asset.created_at.desc()).limit(24))).scalars().all()
    hashes = {shot.image_input_hash}
    candidates = [a for a in rows
                  if a.input_hash in hashes or a.id == shot.selected_image_id]
    return {"items": [_asset_read(a, a.id == shot.selected_image_id)
                      for a in candidates]}


@router.post("/shots/{shot_id}/image:generate", response_model=JobAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def generate_still(shot_id: uuid.UUID, body: GenerateStill,
                         session: DbSession, user: CurrentUser) -> JobAccepted:
    shot, project = await _owned_shot(session, user, shot_id)
    if body.prompt_override is not None:
        shot.prompt_override = body.prompt_override or None
        await session.flush()

    port = get_image_port()
    plan = await plan_still(session, shot, project,
                            provider=getattr(port, "provider", "fake"),
                            model=getattr(port, "model", "unknown"),
                            cost_per_image_cents=getattr(
                                port, "cost_per_image_cents", None))
    try:
        # Refused before the job is created, so a doomed request never becomes
        # a queued job that fails later for a reason the user already knew.
        check_budget(project, plan.estimated_cost_cents * body.n,
                     price_known=plan.price_is_known, model=plan.model)
    except BudgetExceeded as exc:
        raise DomainError(str(exc), code="budget_exceeded",
                          status_code=402) from exc
    except PriceUnknown as exc:
        raise DomainError(str(exc), code="price_unknown",
                          status_code=409) from exc

    job, created = await jobs.enqueue(
        session, project_id=project.id, kind="asset.image",
        input_hash=f"{plan.input_hash}:{body.n}",
        target_type="shot", target_id=shot.id, payload={"n": body.n})
    await session.commit()
    if created or job.status == JobStatus.QUEUED:
        await get_queue().enqueue("asset.image", job.id,
                                  attempt=job.attempt)
    return JobAccepted(job_id=job.id, kind="asset.image",
                       status=str(job.status), created=created)


@router.post("/shots/{shot_id}/image:select")
async def select_still(shot_id: uuid.UUID, body: dict, session: DbSession,
                       user: CurrentUser) -> dict:
    """The approval checkpoint.

    Choosing between candidates is the real character-consistency mechanism in
    the MVP -- no automated score replaces it.
    """
    shot, project = await _owned_shot(session, user, shot_id)
    asset = await session.get(Asset, uuid.UUID(str(body["asset_id"])))
    if asset is None or asset.project_id != project.id:
        raise NotFound("asset not found")
    if asset.kind is not AssetKind.IMAGE:
        raise DomainError("that asset is not an image", code="validation_failed")
    shot.selected_image_id = asset.id
    if asset.source is AssetSource.GENERATED and asset.input_hash:
        shot.image_input_hash = asset.input_hash
    await session.flush()
    await jobs.notify_entity(project.id, "shot", shot.id, "still_selected")
    return {"selected": True, "asset": _asset_read(asset, True)}


@router.post("/shots/{shot_id}/image:upload")
async def upload_still(shot_id: uuid.UUID, session: DbSession,
                       user: CurrentUser,
                       file: UploadFile = File(...)) -> dict:
    """Deadline insurance: drop in your own picture for one shot.

    Uploaded assets are permanently fresh -- they were not produced by a
    prompt, so no prompt change can invalidate them.
    """
    shot, project = await _owned_shot(session, user, shot_id)
    data = await file.read()
    if not data:
        raise DomainError("the uploaded file is empty", code="validation_failed")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DomainError(f"file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024}MB",
                          code="validation_failed")
    if not (file.content_type or "").startswith("image/"):
        raise DomainError(f"expected an image, got {file.content_type}",
                          code="validation_failed")

    aid = uuid7()
    ext = (file.filename or "upload.png").rsplit(".", 1)[-1][:6] or "png"
    key = asset_key(project.id, "image", aid, ext)
    blob = await get_storage().put(key, data)
    asset = Asset(id=aid, project_id=project.id, kind=AssetKind.IMAGE,
                  source=AssetSource.MANUAL, storage_key=key,
                  mime=file.content_type or "image/png", bytes=blob.bytes,
                  checksum=blob.checksum, params={"filename": file.filename})
    session.add(asset)
    await session.flush()
    shot.selected_image_id = asset.id
    await jobs.notify_entity(project.id, "shot", shot.id, "still_uploaded")
    return {"uploaded": True, "asset": _asset_read(asset, True)}


@router.get("/assets/content/{path:path}")
async def asset_content(path: str, session: DbSession, user: CurrentUser):
    """Serve a stored blob.

    Ownership is checked through the asset row rather than trusting the path,
    so one user cannot read another's media by guessing a key.
    """
    from fastapi.responses import FileResponse
    asset = (await session.execute(
        select(Asset).where(Asset.storage_key == path))).scalar_one_or_none()
    if asset is None:
        raise NotFound("asset not found")
    project = await session.get(Project, asset.project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("asset not found")
    local = get_storage().local_path(asset.storage_key)
    if local is None:
        raise NotFound("asset file is missing from storage")
    return FileResponse(local, media_type=asset.mime)
