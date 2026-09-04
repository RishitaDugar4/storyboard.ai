"""Still generation as a background job."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from ...ai.ports import AIError
from ...ai.registry import get_image_port
from ...db.ids import uuid7
from ...db.models import (AICall, Asset, AssetKind, AssetSource, Project,
                          ProjectStage, Shot)
from ...db.session import get_sessionmaker
from ...services.still_service import (BudgetExceeded, PriceUnknown,
                                        cached_asset, check_budget, plan_still)
from ...storage import asset_key, get_storage
from .. import service as jobs


async def generate_still_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        async with jobs.running(session, job_id) as job:
            if job is None:
                return

            shot = await session.get(Shot, job.target_id)
            project = await session.get(Project, job.project_id)
            if shot is None or project is None:
                await jobs.fail(session, job, "not_found",
                                "the shot or project no longer exists")
                return

            port = get_image_port()
            n = int(job.payload.get("n", 2))
            plan = await plan_still(
                session, shot, project,
                provider=getattr(port, "provider", "fake"),
                model=getattr(port, "model", "unknown"),
                cost_per_image_cents=getattr(port, "cost_per_image_cents", None))

            # Record the hash first: freshness is measured against the prompt
            # we are about to use, whether or not generation succeeds.
            shot.image_input_hash = plan.input_hash

            if existing := await cached_asset(session, project.id, plan.input_hash):
                await jobs.progress(session, job, 100, "reused an identical still")
                if shot.selected_image_id is None:
                    shot.selected_image_id = existing.id
                await jobs.succeed(session, job, {
                    "asset_ids": [str(existing.id)], "cached": True,
                    "cost_cents": 0.0})
                await jobs.notify_entity(project.id, "shot", shot.id, "still_ready")
                return

            try:
                check_budget(project, plan.estimated_cost_cents * n,
                             price_known=plan.price_is_known,
                             model=plan.model)
            except (BudgetExceeded, PriceUnknown) as exc:
                code = ("budget_exceeded" if isinstance(exc, BudgetExceeded)
                        else "price_unknown")
                await jobs.fail(session, job, code, str(exc))
                return

            await jobs.progress(session, job, 20,
                                f"generating {n} candidate(s)")
            try:
                images, usage = await port.generate(
                    positive=plan.prompt.positive, negative=plan.prompt.negative,
                    size=plan.size, seed=plan.seed, n=n)
            except AIError as exc:
                await jobs.fail(session, job, f"{exc.kind}:{exc.code}",
                                exc.detail, retryable=exc.retryable)
                return

            await jobs.progress(session, job, 75, "storing candidates")
            storage = get_storage()
            asset_ids: list[str] = []
            for img in images:
                aid = uuid7()
                key = asset_key(project.id, "image", aid, "png")
                blob = await storage.put(key, img.data)
                session.add(Asset(
                    id=aid, project_id=project.id, kind=AssetKind.IMAGE,
                    source=AssetSource.GENERATED, storage_key=key,
                    mime=img.mime, bytes=blob.bytes, checksum=blob.checksum,
                    width=img.width, height=img.height,
                    provider=plan.provider, model=plan.model,
                    input_hash=plan.input_hash,
                    params={"positive": plan.prompt.positive,
                            "negative": plan.prompt.negative,
                            "seed": plan.seed, "size": plan.size},
                    cost_cents=usage.cost_cents / max(1, len(images))))
                asset_ids.append(str(aid))

            session.add(AICall(
                id=uuid7(), project_id=project.id, job_id=job.id,
                capability="image", provider=plan.provider, model=plan.model,
                units=len(images), cost_cents=usage.cost_cents,
                latency_ms=usage.latency_ms, ok=True))
            project.spent_cents = int(project.spent_cents + round(usage.cost_cents))

            # First candidate is auto-selected so a shot is never left without
            # a picture; choosing between candidates stays the user's call.
            if shot.selected_image_id is None and asset_ids:
                shot.selected_image_id = uuid.UUID(asset_ids[0])
            if project.stage in (ProjectStage.STORYBOARDED,
                                 ProjectStage.CHARACTERS_LOCKED):
                project.stage = ProjectStage.STILLS

            await jobs.succeed(session, job, {
                "asset_ids": asset_ids, "cached": False,
                "cost_cents": round(usage.cost_cents, 2)})
            await jobs.notify_entity(project.id, "shot", shot.id, "still_ready")
