"""Domain 5: still generation.

The approved still is the pivot of the whole product. It is a finished
deliverable on its own (Ken Burns renders it directly) and the first frame if
the shot is later animated. Nothing here is throwaway.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.prompts.compose import ComposedPrompt, compose_image_prompt
from ..db.models import (Asset, AssetKind, AssetSource, Character, Location,
                         Project, Scene, Shot)


@dataclass
class ShotPlan:
    """Everything needed to generate -- and to price -- one still."""

    shot: Shot
    prompt: ComposedPrompt
    size: str
    seed: int
    input_hash: str
    provider: str
    model: str
    characters: list[str]
    #: None means the adapter could not price this model. Treated as expensive,
    #: never as free.
    cost_per_image_cents: float | None = 0.0

    @property
    def estimated_cost_cents(self) -> float:
        return self.cost_per_image_cents or 0.0

    @property
    def price_is_known(self) -> bool:
        return self.cost_per_image_cents is not None


async def _context(session: AsyncSession, shot: Shot):
    scene = await session.get(Scene, shot.scene_id)
    location = (await session.get(Location, scene.location_id)
                if scene and scene.location_id else None)
    slugs = list(shot.subject_slugs or [])
    chars: list[Character] = []
    if slugs:
        chars = (await session.execute(
            select(Character).where(Character.project_id == shot.project_id,
                                    Character.slug.in_(slugs))
            .order_by(Character.sort_order))).scalars().all()
    return scene, location, chars


async def plan_still(session: AsyncSession, shot: Shot, project: Project,
                     *, provider: str, model: str,
                     cost_per_image_cents: float | None = 0.0) -> ShotPlan:
    scene, location, chars = await _context(session, shot)
    prompt = compose_image_prompt(
        style_bible=project.style_bible or {},
        shot_type=str(shot.shot_type),
        action=shot.action,
        composition_note=shot.composition_note,
        # Frozen canon, embedded verbatim. Paraphrasing here is exactly what
        # makes a character look like someone else in the next shot.
        character_prompts=[(c.name, c.appearance_prompt) for c in chars],
        location_fragment=location.prompt_fragment if location else "",
        time_of_day=scene.time_of_day if scene else "unspecified",
        prompt_override=shot.prompt_override,
    )
    size = project.image_size
    seed = int(shot.seed)
    return ShotPlan(
        shot=shot, prompt=prompt, size=size, seed=seed,
        input_hash=prompt.hash(seed=seed, size=size, provider=provider,
                               model=model),
        provider=provider, model=model,
        cost_per_image_cents=cost_per_image_cents,
        characters=[c.name for c in chars])


async def cached_asset(session: AsyncSession, project_id: uuid.UUID,
                       input_hash: str) -> Asset | None:
    """An identical prompt has already been paid for; reuse it.

    This is not an optimisation. Regenerating an identical still costs real
    money for a byte-identical result.
    """
    return (await session.execute(
        select(Asset).where(Asset.project_id == project_id,
                            Asset.kind == AssetKind.IMAGE,
                            Asset.input_hash == input_hash)
        .order_by(Asset.created_at.desc()).limit(1))).scalar_one_or_none()


async def recompute_image_hash(session: AsyncSession, shot: Shot,
                               project: Project, *, port=None) -> str:
    """Refresh the shot's record of what its prompt currently hashes to.

    `image_input_hash` means "the prompt this shot would produce now", while an
    asset's `input_hash` means "the prompt that produced me". Freshness is the
    comparison between them -- so this must run after any edit that changes the
    prompt, or a stale still keeps reporting itself as current.
    """
    if port is None:
        from ..ai.registry import get_image_port
        port = get_image_port()
    plan = await plan_still(
        session, shot, project,
        provider=getattr(port, "provider", "fake"),
        model=getattr(port, "model", "unknown"),
        cost_per_image_cents=getattr(port, "cost_per_image_cents", None))
    shot.image_input_hash = plan.input_hash
    return plan.input_hash


async def recompute_hashes_for_character(session: AsyncSession,
                                         project: Project,
                                         slug: str) -> int:
    """A character's canon is embedded in every prompt they appear in, so
    editing it invalidates those stills and no others."""
    shots = (await session.execute(
        select(Shot).where(Shot.project_id == project.id,
                           Shot.subject_slugs.contains([slug])))).scalars().all()
    for shot in shots:
        await recompute_image_hash(session, shot, project)
    return len(shots)


def still_is_fresh(shot: Shot, asset: Asset | None) -> bool:
    """Freshness is derived, never stored.

    A hand-uploaded file is permanently fresh: it was not produced by a prompt,
    so no prompt change can invalidate it, and it must never be silently
    regenerated over.
    """
    if asset is None:
        return False
    if asset.source is AssetSource.MANUAL:
        return True
    return bool(shot.image_input_hash) and asset.input_hash == shot.image_input_hash


class BudgetExceeded(RuntimeError):
    def __init__(self, spent: float, budget: int, needed: float) -> None:
        super().__init__(
            f"this would spend {needed:.0f}c on top of {spent:.0f}c already "
            f"used, exceeding the {budget}c budget for this project. Raise the "
            f"budget deliberately, or generate fewer candidates.")
        self.spent, self.budget, self.needed = spent, budget, needed


class PriceUnknown(RuntimeError):
    def __init__(self, model: str) -> None:
        super().__init__(
            f"no price is known for {model}, so this spend cannot be checked "
            f"against the budget. Add it to the adapter's pricing table before "
            f"generating with it.")


def check_budget(project: Project, needed_cents: float,
                 *, price_known: bool = True, model: str = "") -> None:
    """Checked before submitting, never after.

    A budget enforced after the call has already been paid is not a budget --
    and an unpriced model is refused rather than waved through as free.
    """
    if not price_known:
        raise PriceUnknown(model)
    if float(project.spent_cents) + needed_cents > float(project.budget_cents):
        raise BudgetExceeded(float(project.spent_cents),
                             int(project.budget_cents), needed_cents)
