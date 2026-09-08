"""Domain 6: motion.

The approved still is the first frame; this decides what happens after it.

Two responsibilities, kept apart on purpose:

* **Planning one shot** -- resolve authorial intent against one model's real
  capabilities, price it, and refuse only what is genuinely impossible.
* **Planning the film's durations** -- decide what each shot should *ask* for
  so that the sum of what the model can actually deliver lands near the
  requested runtime. Every provider snaps a duration UP to its grid, so
  planning each shot in isolation systematically overshoots: fourteen shots
  asking for 6s on a 5s/10s grid buys 140s of video for a 90s film.

Nothing here names a provider. Everything it knows comes from the catalogue.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.catalog import (CATALOG, AudioBehavior, ModelStatus, ModelTier,
                          PriceConfidence, VideoModelCaps)
from ..ai.catalog import get as get_caps
from ..ai.pacing import PAD_S, required_seconds
from ..ai.prompts.compose import ComposedMotion, compose_motion_prompt
from ..db.models import (Asset, AssetKind, AssetSource, NarrationLine, Project,
                         Scene, Shot)


@dataclass(frozen=True)
class Note:
    code: str
    message: str


@dataclass
class MotionPlan:
    """Everything needed to submit, price, cache and reproduce one clip."""

    shot_id: uuid.UUID
    caps: VideoModelCaps
    prompt: ComposedMotion

    requested_duration_s: float
    resolved_duration_s: float
    resolved_resolution: str
    aspect_ratio: str
    seed: int | None

    first_frame_checksum: str
    input_hash: str
    estimated_cost_cents: int

    warnings: list[Note] = field(default_factory=list)
    blocking: list[Note] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def model_key(self) -> str:
        return self.caps.model_key

    @property
    def price_is_known(self) -> bool:
        """An ESTIMATED price is good enough to compare models and to show a
        human; it is not good enough to authorize spend against a budget."""
        return self.caps.pricing.confidence is PriceConfidence.VERIFIED

    def describe(self) -> str:
        return (f"{self.caps.display_name} · {self.resolved_duration_s:g}s · "
                f"{self.resolved_resolution} · "
                f"{self.estimated_cost_cents / 100:.2f} USD")


# --------------------------------------------------------------------------- #
# Model selection
# --------------------------------------------------------------------------- #
def selectable_models(*, aspect_ratio: str = "16:9", allow_premium: bool = False,
                      allow_experimental: bool = False) -> list[VideoModelCaps]:
    """Every catalogue entry that could serve this project, cheapest first."""
    out = []
    for caps in CATALOG.values():
        if not caps.image_to_video or caps.status is ModelStatus.DISABLED:
            continue
        if caps.status is ModelStatus.EXPERIMENTAL and not allow_experimental:
            continue
        if caps.tier is ModelTier.PREMIUM and not allow_premium:
            continue
        # A model with no aspect input follows its first frame, so it can serve
        # any project -- refusing it would hide a working, cheaper option.
        if caps.aspect_selectable and aspect_ratio not in caps.aspect_ratios:
            continue
        out.append(caps)
    return sorted(out, key=lambda c: (
        c.pricing.cents(c.durations.resolve(6.0), c.resolutions[0]), c.model_key))


#: The measured default. ARCHITECTURE D5 says economy-first, and
#: `cheapest_capable` still implements that -- but the bake-off actually ran
#: these models, and the cheapest entry is not the right default:
#: Hailuo Standard returned an off-standard 1364x768 frame and ran 15% slower,
#: while Kling delivered 1920x1080 and hit its requested duration within 42ms
#: across two runs (docs/adr/001-bakeoff-results.md, decision 1). Eight cents
#: a clip is the right price for that difference.
DEFAULT_MODEL_KEY = "kling-2.5-turbo-i2v"


def cheapest_capable(*, aspect_ratio: str = "16:9", allow_premium: bool = False,
                     allow_experimental: bool = False) -> str:
    """Economy-first default (ARCHITECTURE D5)."""
    options = selectable_models(aspect_ratio=aspect_ratio,
                                allow_premium=allow_premium,
                                allow_experimental=allow_experimental)
    if not options:
        raise LookupError(
            f"no catalogue model can animate a {aspect_ratio} project"
            + ("" if allow_premium else " without premium spend enabled"))
    return options[0].model_key


def resolve_model_key(project: Project, override: str | None = None,
                      shot: Shot | None = None) -> str:
    """Shot preference beats project default beats the economy default."""
    for candidate in (override,
                      shot.preferred_model_key if shot else None,
                      project.default_model_key):
        if candidate:
            return candidate
    caps = CATALOG.get(DEFAULT_MODEL_KEY)
    if caps and (not caps.aspect_selectable
                 or project.aspect_ratio in caps.aspect_ratios):
        return DEFAULT_MODEL_KEY
    # The measured default cannot serve this project's shape; fall back to the
    # architecture's economy-first rule rather than to nothing.
    return cheapest_capable(aspect_ratio=project.aspect_ratio,
                            allow_premium=bool(project.allow_premium))


# --------------------------------------------------------------------------- #
# Film-level duration planning
# --------------------------------------------------------------------------- #
@dataclass
class DurationAllocation:
    shot_id: uuid.UUID
    #: What the storyboard asked for.
    intent_s: float
    #: The floor imposed by narration: audio is never compressed to fit a
    #: picture, so a shot must be at least as long as the words it carries.
    floor_s: float
    #: What we will actually ask the model for.
    planned_s: float
    #: What the model will actually produce for that ask.
    resolved_s: float


@dataclass
class FilmDurationPlan:
    allocations: list[DurationAllocation]
    caps: VideoModelCaps
    target_total_s: float

    @property
    def planned_total_s(self) -> float:
        return sum(a.resolved_s for a in self.allocations)

    @property
    def drift_s(self) -> float:
        return self.planned_total_s - self.target_total_s

    @property
    def floor_total_s(self) -> float:
        """The shortest film this model can make of this storyboard."""
        return sum(self.caps.durations.resolve(a.floor_s)
                   for a in self.allocations)

    def summary(self) -> str:
        return (f"{len(self.allocations)} shots · "
                f"{self.planned_total_s:.0f}s of generated video "
                f"(target {self.target_total_s:.0f}s, "
                f"{self.drift_s:+.0f}s)")


def plan_film_durations(shots: list[tuple[uuid.UUID, float, int]],
                        caps: VideoModelCaps,
                        target_total_s: float) -> FilmDurationPlan:
    """Allocate clip lengths across a whole film against one model's grid.

    `shots` is (shot_id, intent_s, narration_word_count).

    Every provider rounds a requested duration UP to the nearest length it can
    produce, so planning shots independently overshoots the runtime by the
    average rounding error times the shot count. This starts every shot at the
    shortest length that still holds its narration, then spends the remaining
    runtime one grid step at a time on whichever shot the storyboard wanted
    longest -- so the overshoot is bounded by a single step rather than
    accumulating across the film.

    A film whose floor already exceeds the target is reported, not silently
    truncated: the fix is fewer shots or less narration, and only a human
    should choose which.
    """
    grid = sorted(caps.durations.values) if caps.durations.kind == "discrete" else []
    allocations: list[DurationAllocation] = []
    for shot_id, intent_s, words in shots:
        floor_s = max(required_seconds(words), 0.0) if words else 0.0
        start = caps.durations.resolve(max(floor_s, 0.1))
        allocations.append(DurationAllocation(
            shot_id=shot_id, intent_s=intent_s, floor_s=floor_s,
            planned_s=start, resolved_s=start))

    if not grid:
        # A continuous range needs no allocation game: ask for exactly the
        # intent and let the model clamp it.
        for a in allocations:
            a.planned_s = max(a.intent_s, a.floor_s)
            a.resolved_s = caps.durations.resolve(a.planned_s)
        return FilmDurationPlan(allocations, caps, target_total_s)

    def total() -> float:
        return sum(a.resolved_s for a in allocations)

    # Spend the remaining runtime where the storyboard most wanted length.
    # Bounded by the number of upgrades available, so it always terminates.
    upgradable = True
    while upgradable and total() < target_total_s:
        upgradable = False
        best, best_deficit = None, 0.0
        for a in allocations:
            higher = [v for v in grid if v > a.resolved_s]
            if not higher:
                continue
            step = higher[0]
            if total() - a.resolved_s + step > target_total_s:
                continue          # this upgrade would overshoot the film
            deficit = a.intent_s - a.resolved_s
            if deficit > best_deficit:
                best, best_deficit = a, deficit
        if best is not None:
            best.resolved_s = next(v for v in grid if v > best.resolved_s)
            best.planned_s = best.resolved_s
            upgradable = True

    return FilmDurationPlan(allocations, caps, target_total_s)


# --------------------------------------------------------------------------- #
# Planning one shot
# --------------------------------------------------------------------------- #
async def _narration_words(session: AsyncSession, shot: Shot) -> int:
    lines = (await session.execute(
        select(NarrationLine).where(NarrationLine.shot_id == shot.id))
    ).scalars().all()
    return sum(len(l.text.split()) for l in lines)


async def plan_motion(session: AsyncSession, shot: Shot, project: Project, *,
                      model_key: str | None = None,
                      duration_s: float | None = None,
                      allow_experimental: bool = False) -> MotionPlan:
    """Resolve one shot's intent against one model's real capabilities.

    Warns freely; blocks only on genuine impossibility. A model with no
    reference-image input is a legitimate, cheap choice -- it just means the
    approved still carries the whole consistency burden, and the plan says so
    rather than hiding the model or failing at submit time.
    """
    key = model_key or resolve_model_key(project, shot=shot)
    caps = get_caps(key)
    warnings: list[Note] = []
    blocking: list[Note] = []

    still = (await session.get(Asset, shot.selected_image_id)
             if shot.selected_image_id else None)
    if still is None:
        blocking.append(Note(
            "no_approved_still",
            "This shot has no approved still. The keyframe is the first frame "
            "of the clip, so it must exist before motion can be generated."))
    if not caps.image_to_video:
        blocking.append(Note("no_image_to_video",
                             f"{caps.display_name} cannot animate an input image."))
    if caps.status is ModelStatus.DISABLED:
        blocking.append(Note("model_disabled",
                             f"{caps.display_name} is disabled in the catalogue."))
    if caps.status is ModelStatus.EXPERIMENTAL and not allow_experimental:
        blocking.append(Note(
            "model_experimental",
            f"{caps.display_name}'s request shape has never been verified "
            f"against the live API. Confirm it against "
            f"{caps.docs_url or 'the provider docs'} before spending on it."))
    if caps.tier is ModelTier.PREMIUM and not project.allow_premium:
        blocking.append(Note(
            "premium_not_enabled",
            f"{caps.display_name} is a premium model and premium spend is off "
            f"for this project."))
    if caps.aspect_selectable and project.aspect_ratio not in caps.aspect_ratios:
        blocking.append(Note(
            "aspect_unsupported",
            f"{caps.display_name} supports {'/'.join(caps.aspect_ratios)}, "
            f"not {project.aspect_ratio}."))
    elif not caps.aspect_selectable:
        warnings.append(Note(
            "aspect_follows_source",
            f"{caps.display_name} has no aspect-ratio input; the clip's shape "
            f"follows the approved still, which must already be "
            f"{project.aspect_ratio}."))

    intent_s = float(duration_s if duration_s is not None
                     else shot.target_duration_s)
    resolved_d = caps.durations.resolve(intent_s)
    if abs(resolved_d - intent_s) > 0.05:
        how = ("padded with a held frame" if resolved_d < intent_s
               else "longer than asked for")
        warnings.append(Note(
            "duration_adjusted",
            f"{caps.display_name} produces {caps.durations.describe()}; the "
            f"{intent_s:g}s intent resolves to {resolved_d:g}s ({how})."))

    words = await _narration_words(session, shot)
    if words and required_seconds(words) > resolved_d + 0.05:
        warnings.append(Note(
            "narration_overflows_clip",
            f"{words} words need {required_seconds(words):.1f}s but the clip "
            f"will be {resolved_d:g}s. The renderer will hold the last frame "
            f"for {required_seconds(words) - resolved_d:.1f}s -- visible as a "
            f"freeze. Shorten the line or pick a model with a longer grid."))

    preferred_res = f"{project.image_size.split('x')[-1]}p"
    resolved_r = caps.best_resolution(preferred_res)
    if not caps.resolution_selectable and resolved_r.lower() != preferred_res.lower():
        warnings.append(Note(
            "resolution_fixed",
            f"{caps.display_name} has no resolution input; it outputs "
            f"{resolved_r} whatever is asked. The measured clip is the "
            f"authority, and the renderer scales it to delivery."))
    elif resolved_r.lower() != preferred_res.lower():
        warnings.append(Note(
            "resolution_adjusted",
            f"{caps.display_name} does not offer {preferred_res}; using "
            f"{resolved_r}, upscaled at render time."))

    if caps.max_reference_images == 0 and (shot.subject_slugs or []):
        warnings.append(Note(
            "no_reference_support",
            f"{caps.display_name} accepts no reference images. Character "
            f"consistency rests entirely on the approved still."))
    if caps.audio is AudioBehavior.ALWAYS_ON:
        warnings.append(Note(
            "audio_discarded",
            f"{caps.display_name} always generates audio; it is discarded at "
            f"render time so it cannot fight the narrator."))
    if caps.pricing.confidence is not PriceConfidence.VERIFIED:
        stale = caps.pricing.staleness_days()
        warnings.append(Note(
            "price_unverified",
            f"The price for {caps.display_name} is {caps.pricing.confidence} "
            f"(source: {caps.pricing.source}"
            + (f", {stale}d old" if stale is not None else "")
            + "). It cannot be checked against the budget."))

    scene = await session.get(Scene, shot.scene_id)
    prompt = compose_motion_prompt(
        subject_motion=shot.subject_motion or "",
        environment_motion=getattr(shot, "environment_motion", "") or "",
        camera_move=str(shot.camera_move),
        motion_pacing=getattr(shot, "motion_pacing", "slow") or "slow",
        motion_language=(project.style_bible or {}).get("motion_language", ""),
        supports_negative_prompt=caps.supports_negative_prompt,
        motion_override=shot.motion_override)
    if not (shot.subject_motion or "").strip() and not (
            getattr(shot, "environment_motion", "") or "").strip():
        warnings.append(Note(
            "no_motion_direction",
            f"This shot's storyboard says nothing about what should move, so "
            f"the clip will be whatever {caps.display_name} invents. Add "
            f"subject or environment motion to direct it."))

    seed = int(shot.seed) if caps.supports_seed else None
    checksum = still.checksum if still else ""
    return MotionPlan(
        shot_id=shot.id, caps=caps, prompt=prompt,
        requested_duration_s=intent_s, resolved_duration_s=resolved_d,
        resolved_resolution=resolved_r, aspect_ratio=project.aspect_ratio,
        seed=seed, first_frame_checksum=checksum,
        input_hash=prompt.hash(model_key=caps.model_key,
                               duration_s=resolved_d, resolution=resolved_r,
                               seed=seed, first_frame_checksum=checksum),
        estimated_cost_cents=caps.pricing.cents(resolved_d, resolved_r),
        warnings=warnings, blocking=blocking)


# --------------------------------------------------------------------------- #
# Caching and freshness -- the same rules as stills, at ten times the price
# --------------------------------------------------------------------------- #
async def cached_clip(session: AsyncSession, project_id: uuid.UUID,
                      input_hash: str) -> Asset | None:
    """An identical generation has already been paid for; reuse it.

    Not an optimisation. A 5s Kling clip is 35c, roughly fourteen times a
    still, and regenerating one from identical inputs buys nothing.
    """
    return (await session.execute(
        select(Asset).where(Asset.project_id == project_id,
                            Asset.kind == AssetKind.CLIP,
                            Asset.input_hash == input_hash)
        .order_by(Asset.created_at.desc()).limit(1))).scalar_one_or_none()


async def recompute_motion_hash(session: AsyncSession, shot: Shot,
                                project: Project) -> str | None:
    """Refresh what this shot's motion inputs currently hash to.

    Must run after anything that changes the clip's inputs -- the motion plan,
    the approved still, the chosen model -- or a stale clip keeps reporting
    itself as current and ships in the film.
    """
    plan = await plan_motion(session, shot, project, allow_experimental=True)
    shot.motion_input_hash = plan.input_hash
    return plan.input_hash


def clip_is_fresh(shot: Shot, asset: Asset | None) -> bool:
    """Derived, never stored -- the same rule as stills.

    An uploaded clip is permanently fresh: it was not produced by a prompt, so
    no prompt change can invalidate it, and it must never be regenerated over.
    """
    if asset is None:
        return False
    if asset.source is AssetSource.MANUAL:
        return True
    return bool(shot.motion_input_hash) and asset.input_hash == shot.motion_input_hash
