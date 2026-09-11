"""Motion: turn approved stills into generated clips.

The plan endpoint exists so the cost and the constraints are visible *before*
anything is bought. A generated clip is roughly fourteen times the price of a
still, so "generate and see" is not an acceptable interaction.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..ai.catalog import CATALOG, ModelStatus
from ..db.models import Project, Scene, Shot
from ..errors import DomainError, NotFound
from ..auth import CurrentUser, DbSession
from ..jobs import get_queue
from ..jobs import service as jobs
from ..schemas.api.story import JobAccepted
from ..services.motion_service import (DEFAULT_MODEL_KEY, plan_film_durations,
                                       plan_motion, resolve_model_key,
                                       selectable_models)
from ..services.still_service import (BudgetExceeded, PriceUnknown,
                                      check_budget)

router = APIRouter(prefix="/api/v1", tags=["motion"])


class GenerateMotion(BaseModel):
    model_key: str | None = None
    duration_s: float | None = Field(default=None, ge=1.0, le=20.0)
    motion_override: str | None = Field(default=None, max_length=2000)
    #: An EXPERIMENTAL model's request shape has never been verified against
    #: the live API, so enabling one has to be a deliberate act.
    allow_experimental: bool = False


async def _owned_shot(session, user, shot_id: uuid.UUID):
    shot = await session.get(Shot, shot_id)
    if shot is None:
        raise NotFound("shot not found")
    project = await session.get(Project, shot.project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("shot not found")
    return shot, project


def _caps_read(caps) -> dict:
    return {
        "model_key": caps.model_key,
        "display_name": caps.display_name,
        "provider": caps.provider,
        "tier": caps.tier.value,
        "status": caps.status.value,
        "durations": list(caps.durations.values) or None,
        "duration_description": caps.durations.describe(),
        "resolutions": list(caps.resolutions),
        "aspect_ratios": list(caps.aspect_ratios),
        "max_reference_images": caps.max_reference_images,
        "supports_negative_prompt": caps.supports_negative_prompt,
        "supports_seed": caps.supports_seed,
        "supports_last_frame": caps.supports_last_frame,
        "audio": caps.audio.value,
        "capability_chips": caps.capability_chips(),
        "price_confidence": caps.pricing.confidence.value,
        "price_source": caps.pricing.source,
        "typical_latency_s": caps.typical_latency_s,
        "is_default": caps.model_key == DEFAULT_MODEL_KEY,
        "notes": caps.notes,
    }


@router.get("/video-models")
async def list_video_models(user: CurrentUser,
                            allow_premium: bool = False,
                            allow_experimental: bool = False) -> dict:
    """The catalogue, rendered. Adding a model is a data change; this endpoint
    and any picker built on it need no edit."""
    return {"default": DEFAULT_MODEL_KEY,
            "items": [_caps_read(c) for c in selectable_models(
                allow_premium=allow_premium,
                allow_experimental=allow_experimental)]}


@router.post("/shots/{shot_id}/motion:plan")
async def plan_shot_motion(shot_id: uuid.UUID, body: GenerateMotion,
                           session: DbSession, user: CurrentUser) -> dict:
    """Price and validate without spending. Warnings are sentences a human can
    act on; only genuine impossibilities block."""
    shot, project = await _owned_shot(session, user, shot_id)
    plan = await plan_motion(session, shot, project, model_key=body.model_key,
                             duration_s=body.duration_s,
                             allow_experimental=body.allow_experimental)
    return {
        "shot_id": str(shot.id),
        "model_key": plan.model_key,
        "display_name": plan.caps.display_name,
        "prompt": plan.prompt.positive,
        "negative_prompt": plan.prompt.negative,
        "fragments": [{"origin": o, "text": t} for o, t in plan.prompt.fragments],
        "requested_duration_s": plan.requested_duration_s,
        "resolved_duration_s": plan.resolved_duration_s,
        "resolution": plan.resolved_resolution,
        "estimated_cost_cents": plan.estimated_cost_cents,
        "price_is_known": plan.price_is_known,
        "input_hash": plan.input_hash,
        # Resolved, not requested: a project set to 'auto' on a model with no
        # closing-frame input reports 'chained' here, and a shot with no
        # neighbour to join reports 'none'. What the picker shows is what the
        # generation will do.
        "continuity": plan.continuity.value,
        "ok": plan.ok,
        "warnings": [{"code": n.code, "message": n.message} for n in plan.warnings],
        "blocking": [{"code": n.code, "message": n.message} for n in plan.blocking],
    }


@router.post("/projects/{project_id}/motion:plan")
async def plan_film_motion(project_id: uuid.UUID, body: GenerateMotion,
                           session: DbSession, user: CurrentUser) -> dict:
    """The whole film's motion, priced together.

    Per-shot planning cannot answer the question that matters -- what the film
    costs and how long it ends up -- because every provider rounds a duration
    UP, and that error compounds across the shot list.
    """
    project = await session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("project not found")

    rows = (await session.execute(
        select(Shot, Scene).join(Scene, Scene.id == Shot.scene_id)
        .where(Shot.project_id == project.id)
        .order_by(Scene.sort_order, Shot.sort_order))).all()
    if not rows:
        raise DomainError("apply a storyboard first", code="no_shots")

    key = body.model_key or resolve_model_key(project)
    caps = CATALOG[key]
    shots, total_cents, blocked = [], 0, 0
    for shot, scene in rows:
        plan = await plan_motion(session, shot, project, model_key=key,
                                 allow_experimental=body.allow_experimental)
        total_cents += plan.estimated_cost_cents
        blocked += 0 if plan.ok else 1
        shots.append({
            "shot_id": str(shot.id), "scene": scene.title,
            "duration_s": plan.resolved_duration_s,
            "cost_cents": plan.estimated_cost_cents,
            "prompt": plan.prompt.positive,
            "ok": plan.ok,
            "warnings": [n.code for n in plan.warnings],
            "blocking": [n.message for n in plan.blocking],
        })

    allocation = plan_film_durations(
        [(s.id, float(s.target_duration_s), 0) for s, _ in rows], caps,
        sum(float(s.target_duration_s) for s, _ in rows))
    return {
        "model_key": key, "display_name": caps.display_name,
        "shots": shots, "shot_count": len(shots),
        "blocked_count": blocked,
        "estimated_cost_cents": total_cents,
        "estimated_runtime_s": allocation.planned_total_s,
        "price_confidence": caps.pricing.confidence.value,
        "price_source": caps.pricing.source,
    }


@router.post("/shots/{shot_id}/motion:generate", response_model=JobAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def generate_motion(shot_id: uuid.UUID, body: GenerateMotion,
                          session: DbSession, user: CurrentUser) -> JobAccepted:
    shot, project = await _owned_shot(session, user, shot_id)
    if body.motion_override is not None:
        shot.motion_override = body.motion_override or None
        await session.flush()

    plan = await plan_motion(session, shot, project, model_key=body.model_key,
                             duration_s=body.duration_s,
                             allow_experimental=body.allow_experimental)
    if plan.blocking:
        raise DomainError(" ".join(n.message for n in plan.blocking),
                          code="motion_blocked", status_code=409)
    try:
        # Refused before the job exists, so a doomed request never becomes a
        # queued job that fails later for a reason already known here.
        check_budget(project, plan.estimated_cost_cents,
                     price_known=plan.price_is_known,
                     model=plan.caps.display_name)
    except BudgetExceeded as exc:
        raise DomainError(str(exc), code="budget_exceeded",
                          status_code=402) from exc
    except PriceUnknown as exc:
        raise DomainError(str(exc), code="price_unknown",
                          status_code=409) from exc

    job, created = await jobs.enqueue(
        session, project_id=project.id, kind="motion.submit",
        input_hash=plan.input_hash, target_type="shot", target_id=shot.id,
        payload={"model_key": plan.model_key,
                 "duration_s": plan.requested_duration_s,
                 "allow_experimental": body.allow_experimental})
    dispatch = jobs.needs_dispatch(job, created)
    await session.commit()
    if dispatch:
        await get_queue().enqueue("motion.submit", job.id, attempt=job.attempt)
    return JobAccepted(job_id=job.id, kind="motion.submit",
                       status=str(job.status), created=created)


@router.post("/projects/{project_id}/motion:generate_all")
async def generate_all_motion(project_id: uuid.UUID, body: GenerateMotion,
                              session: DbSession, user: CurrentUser) -> dict:
    """Animate every shot that has an approved still.

    The whole film's cost is checked once, up front. Checking per shot would
    let a fan-out spend most of a budget before the shot that exceeds it is
    reached -- and the clips already bought are not refundable.
    """
    project = await session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise NotFound("project not found")

    rows = (await session.execute(
        select(Shot).where(Shot.project_id == project.id)
        .order_by(Shot.sort_order))).scalars().all()

    key = body.model_key or resolve_model_key(project)
    plans, skipped = [], []
    for shot in rows:
        plan = await plan_motion(session, shot, project, model_key=key,
                                 allow_experimental=body.allow_experimental)
        if plan.ok:
            plans.append((shot, plan))
        else:
            skipped.append({"shot_id": str(shot.id),
                            "reason": plans and "" or plan.blocking[0].message})
    if not plans:
        raise DomainError(
            "no shot is ready to animate; approve a still first",
            code="nothing_to_animate")

    total = sum(p.estimated_cost_cents for _, p in plans)
    try:
        check_budget(project, total,
                     price_known=all(p.price_is_known for _, p in plans),
                     model=CATALOG[key].display_name)
    except BudgetExceeded as exc:
        raise DomainError(str(exc), code="budget_exceeded",
                          status_code=402) from exc
    except PriceUnknown as exc:
        raise DomainError(str(exc), code="price_unknown",
                          status_code=409) from exc

    queued = []
    for shot, plan in plans:
        job, created = await jobs.enqueue(
            session, project_id=project.id, kind="motion.submit",
            input_hash=plan.input_hash, target_type="shot", target_id=shot.id,
            payload={"model_key": plan.model_key,
                     "duration_s": plan.requested_duration_s,
                     "allow_experimental": body.allow_experimental})
        if jobs.needs_dispatch(job, created):
            queued.append((job.id, job.attempt))
    await session.commit()
    for jid, attempt in queued:
        await get_queue().enqueue("motion.submit", jid, attempt=attempt)
    return {"queued": len(queued), "shots": len(rows),
            "skipped": skipped, "model_key": key,
            "estimated_cost_cents": total}
