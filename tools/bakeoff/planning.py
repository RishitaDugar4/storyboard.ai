"""Planning: reconcile authorial intent with what a model can actually do.

Pure, free, and side-effect-free. Both the operator-facing estimate and the
submit path call this; the submit path re-plans rather than trusting a caller.

Graduates to ``apps/api/app/ai/planning.py``. In the application this is what
backs ``POST /shots/{id}/motion:plan``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Sequence

from catalog import (
    CATALOG,
    AudioBehavior,
    ModelStatus,
    ModelTier,
    PriceConfidence,
    VideoModelCaps,
    get as get_caps,
)

# --------------------------------------------------------------------------- #
# App-level pacing constants. These are OURS, not any provider's.
# --------------------------------------------------------------------------- #
WORDS_PER_SECOND = 2.5      # narration pace; recalibrate against real TTS at M2
PAD_S = 0.9                 # 0.3s lead-in + 0.6s tail
TIGHT_SLACK_S = 0.75        # below this much headroom, warn before it bites
MIN_SHOT_S, MAX_SHOT_S = 2.5, 12.0


class FitStatus(StrEnum):
    FITS = "fits"
    TIGHT = "tight"
    OVERFLOW = "overflow"


@dataclass(frozen=True)
class NarrationFit:
    """Narration fit evaluated against a PROVIDER-RESOLVED duration.

    Deliberately not a schema validator on ``Shot.target_duration_s``: the shot
    carries authorial intent, and whether the words fit depends on which model
    is selected. A 6s intent is 5s on one model and 8s on another, and the same
    narration passes or fails accordingly. Evaluated at plan time, every time.
    """

    status: FitStatus
    word_count: int
    word_budget: int              # words that fit resolved_duration_s
    resolved_duration_s: float
    estimated_speech_s: float
    required_s: float             # speech + padding
    slack_s: float                # resolved - required; negative means overflow

    @property
    def slack_words(self) -> int:
        return self.word_budget - self.word_count

    def message(self) -> str:
        if self.status is FitStatus.FITS:
            return (f"{self.word_count}/{self.word_budget} words fit a "
                    f"{self.resolved_duration_s:g}s clip ({self.slack_s:.1f}s spare).")
        if self.status is FitStatus.TIGHT:
            return (f"{self.word_count}/{self.word_budget} words only just fit a "
                    f"{self.resolved_duration_s:g}s clip ({self.slack_s:.1f}s spare). "
                    f"Consider trimming {max(1, -self.slack_words + 2)} words.")
        return (f"{self.word_count} words need ~{self.required_s:.1f}s but the clip "
                f"resolves to {self.resolved_duration_s:g}s. Cut about "
                f"{abs(self.slack_words)} words, raise the target duration, or "
                f"accept a {abs(self.slack_s):.1f}s held frame.")


def word_budget(duration_s: float) -> int:
    return max(0, int((duration_s - PAD_S) * WORDS_PER_SECOND))


def evaluate_narration_fit(word_count: int, resolved_duration_s: float) -> NarrationFit:
    speech_s = word_count / WORDS_PER_SECOND if word_count else 0.0
    required_s = speech_s + PAD_S if word_count else 0.0
    slack_s = resolved_duration_s - required_s
    if slack_s < 0:
        status = FitStatus.OVERFLOW
    elif slack_s < TIGHT_SLACK_S:
        status = FitStatus.TIGHT
    else:
        status = FitStatus.FITS
    return NarrationFit(
        status=status,
        word_count=word_count,
        word_budget=word_budget(resolved_duration_s),
        resolved_duration_s=resolved_duration_s,
        estimated_speech_s=round(speech_s, 2),
        required_s=round(required_s, 2),
        slack_s=round(slack_s, 2),
    )


@dataclass(frozen=True)
class Note:
    code: str
    message: str


@dataclass
class GenerationPlan:
    """Everything needed to submit, price, authorize and reproduce one clip."""

    # routing
    model_key: str
    provider: str
    adapter: str
    model_id: str
    display_name: str
    tier: ModelTier
    catalog_version: str
    composer_version: int

    # inputs
    prompt: str
    negative_prompt: str | None
    first_frame_sha256: str
    reference_sha256: list[str]

    # requested vs resolved
    requested_duration_s: float
    resolved_duration_s: float
    requested_resolution: str
    resolved_resolution: str
    aspect_ratio: str
    seed: int | None

    # money
    estimated_cost_cents: int
    price_confidence: PriceConfidence
    price_source: str
    price_verified_at: date | None

    # advisories
    narration_fit: NarrationFit | None = None
    warnings: list[Note] = field(default_factory=list)
    blocking: list[Note] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def input_hash(self) -> str:
        """Stable identity of this generation's inputs. Drives caching, job
        idempotency, and staleness in the application."""
        payload = "|".join([
            self.model_key, self.model_id, self.prompt, self.negative_prompt or "",
            self.first_frame_sha256, ",".join(sorted(self.reference_sha256)),
            f"{self.resolved_duration_s:g}", self.resolved_resolution,
            self.aspect_ratio, str(self.seed), str(self.composer_version),
        ])
        return hashlib.sha256(payload.encode()).hexdigest()


class CostAuthorizationError(RuntimeError):
    """Raised when a generation's cost exceeds what the caller authorized."""


def authorize(plan: GenerationPlan, max_authorized_cost_cents: int | None) -> None:
    """Gate every paid submission on an explicit, caller-supplied ceiling.

    The estimate is *not* the control. Pricing may be ESTIMATED, stale, or plain
    wrong, and the provider is the only authority on what a call costs. The
    authorized maximum is the operator's actual risk decision, so it is
    mandatory and enforced here -- not inferred from the estimate.
    """
    if max_authorized_cost_cents is None:
        raise CostAuthorizationError(
            f"{plan.model_key}: paid submission requires an explicit "
            f"max_authorized_cost_cents (estimate is "
            f"{plan.estimated_cost_cents}c, confidence={plan.price_confidence})."
        )
    if max_authorized_cost_cents <= 0:
        raise CostAuthorizationError(
            f"{plan.model_key}: max_authorized_cost_cents must be positive, got "
            f"{max_authorized_cost_cents}."
        )
    if plan.estimated_cost_cents > max_authorized_cost_cents:
        raise CostAuthorizationError(
            f"{plan.model_key}: estimated {plan.estimated_cost_cents}c exceeds "
            f"authorized maximum {max_authorized_cost_cents}c for a "
            f"{plan.resolved_duration_s:g}s {plan.resolved_resolution} clip "
            f"(price confidence: {plan.price_confidence}, source: "
            f"{plan.price_source})."
        )


def plan_motion(
    *,
    model_key: str,
    prompt: str,
    negative_prompt_terms: Sequence[str],
    first_frame_sha256: str,
    reference_sha256: Sequence[str],
    target_duration_s: float,
    preferred_resolution: str,
    aspect_ratio: str,
    seed: int | None,
    composer_version: int,
    narration_word_count: int | None = None,
    allow_premium: bool = True,
    allow_experimental: bool = False,
) -> GenerationPlan:
    """Resolve intent against capabilities. Warns freely; blocks only on
    genuine impossibility."""
    caps = get_caps(model_key)
    warnings: list[Note] = []
    blocking: list[Note] = []

    if not caps.image_to_video:
        blocking.append(Note("no_image_to_video",
                             f"{caps.display_name} cannot animate an input image."))
    if caps.status is ModelStatus.DISABLED:
        blocking.append(Note("model_disabled",
                             f"{caps.display_name} is disabled in the catalogue."))
    if caps.status is ModelStatus.EXPERIMENTAL and not allow_experimental:
        blocking.append(Note(
            "model_experimental",
            f"{caps.display_name}'s request shape is unverified. Confirm it "
            f"against {caps.docs_url or 'the provider docs'} and re-run with "
            f"--allow-experimental."))
    if caps.tier is ModelTier.PREMIUM and not allow_premium:
        blocking.append(Note("premium_not_enabled",
                             f"{caps.display_name} is premium; premium spend is off."))
    if aspect_ratio not in caps.aspect_ratios:
        blocking.append(Note(
            "aspect_unsupported",
            f"{caps.display_name} supports {'/'.join(caps.aspect_ratios)}, "
            f"not {aspect_ratio}."))

    resolved_d = caps.durations.resolve(target_duration_s)
    if abs(resolved_d - target_duration_s) > 0.05:
        direction = "padded with a held frame" if resolved_d < target_duration_s \
            else "longer than requested"
        warnings.append(Note(
            "duration_adjusted",
            f"{caps.display_name} produces {caps.durations.describe()}; your "
            f"{target_duration_s:g}s target resolves to {resolved_d:g}s "
            f"({direction})."))

    resolved_r = caps.best_resolution(preferred_resolution)
    if resolved_r != preferred_resolution:
        warnings.append(Note(
            "resolution_adjusted",
            f"{caps.display_name} does not offer {preferred_resolution}; "
            f"using {resolved_r}."))

    refs = list(reference_sha256)[:caps.max_reference_images]
    if len(reference_sha256) > caps.max_reference_images:
        if caps.max_reference_images == 0:
            warnings.append(Note(
                "no_reference_support",
                f"{caps.display_name} has no reference-image input. Character "
                f"consistency rests entirely on the approved first frame."))
        else:
            warnings.append(Note(
                "references_truncated",
                f"{caps.display_name} accepts {caps.max_reference_images} "
                f"reference image(s); {len(reference_sha256) - caps.max_reference_images} "
                f"will be dropped and those characters may drift."))

    if caps.audio is AudioBehavior.ALWAYS_ON:
        warnings.append(Note(
            "audio_discarded",
            f"{caps.display_name} always generates audio; it is discarded at "
            f"render time and the prompt suppresses dialogue."))

    negative = ", ".join(negative_prompt_terms) if caps.supports_negative_prompt else None
    if negative_prompt_terms and negative is None:
        warnings.append(Note(
            "negative_prompt_folded",
            f"{caps.display_name} accepts no negative prompt; exclusions were "
            f"folded into the positive prompt."))

    if caps.pricing.confidence is not PriceConfidence.VERIFIED:
        stale = caps.pricing.staleness_days()
        warnings.append(Note(
            "price_unverified",
            f"Price for {caps.display_name} is {caps.pricing.confidence} "
            f"(source: {caps.pricing.source}"
            + (f", {stale}d old" if stale is not None else "")
            + "). The authorized cost cap governs, not this estimate."))

    fit = (evaluate_narration_fit(narration_word_count, resolved_d)
           if narration_word_count is not None else None)
    if fit and fit.status is FitStatus.OVERFLOW:
        warnings.append(Note("narration_overflow", fit.message()))
    elif fit and fit.status is FitStatus.TIGHT:
        warnings.append(Note("narration_tight", fit.message()))

    return GenerationPlan(
        model_key=caps.model_key, provider=caps.provider, adapter=caps.adapter,
        model_id=caps.model_id, display_name=caps.display_name, tier=caps.tier,
        catalog_version=__import__("catalog").CATALOG_VERSION,
        composer_version=composer_version,
        prompt=prompt, negative_prompt=negative,
        first_frame_sha256=first_frame_sha256, reference_sha256=refs,
        requested_duration_s=target_duration_s, resolved_duration_s=resolved_d,
        requested_resolution=preferred_resolution, resolved_resolution=resolved_r,
        aspect_ratio=aspect_ratio,
        seed=seed if caps.supports_seed else None,
        estimated_cost_cents=caps.pricing.cents(resolved_d, resolved_r),
        price_confidence=caps.pricing.confidence,
        price_source=caps.pricing.source,
        price_verified_at=caps.pricing.verified_at,
        narration_fit=fit, warnings=warnings, blocking=blocking,
    )


def cheapest_capable(
    *, target_duration_s: float, preferred_resolution: str, aspect_ratio: str,
    allow_premium: bool = False, allow_experimental: bool = False,
) -> str:
    """Economy-first default model selection.

    May rank on ESTIMATED pricing -- comparison is exactly what estimates are
    for. Authorization still happens separately at submit time.
    """
    candidates: list[tuple[int, VideoModelCaps]] = []
    for caps in CATALOG.values():
        if not caps.image_to_video or caps.status is ModelStatus.DISABLED:
            continue
        if caps.status is ModelStatus.EXPERIMENTAL and not allow_experimental:
            continue
        if caps.tier is ModelTier.PREMIUM and not allow_premium:
            continue
        if aspect_ratio not in caps.aspect_ratios:
            continue
        d = caps.durations.resolve(target_duration_s)
        r = caps.best_resolution(preferred_resolution)
        candidates.append((caps.pricing.cents(d, r), caps))
    if not candidates:
        raise LookupError("no catalogue model satisfies those requirements")
    return min(candidates, key=lambda t: (t[0], t[1].model_key))[1].model_key
