"""Video model catalogue: provider capabilities and pricing as typed data.

This module is the single place where provider-specific facts live. Nothing
else in the harness -- and nothing else in the future application outside
``ai/adapters/`` -- may name a provider or hard-code one of its constraints.

Graduates to ``apps/api/app/ai/catalog.py`` unchanged.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from typing import Any, Iterable, Literal, Mapping

# Bump when any entry changes. Recorded on every generation for reproducibility.
CATALOG_VERSION = "2026-08-26.1"


class ModelTier(StrEnum):
    ECONOMY = "economy"
    STANDARD = "standard"
    PREMIUM = "premium"


class AudioBehavior(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    ALWAYS_ON = "always_on"


class ModelStatus(StrEnum):
    #: Request/response shape verified against live provider docs.
    ACTIVE = "active"
    #: Shape written from partial docs; must be confirmed before paid use.
    EXPERIMENTAL = "experimental"
    #: Retained for reference; the harness refuses to run it.
    DISABLED = "disabled"


class PriceConfidence(StrEnum):
    #: Taken from the provider's own pricing page on ``verified_at``.
    VERIFIED = "verified"
    #: Third-party or inferred. Usable for comparison, never for authorization.
    ESTIMATED = "estimated"
    #: No usable figure. Comparison only; paid use requires an explicit cap.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DurationSupport:
    """What clip lengths a model can actually produce."""

    kind: Literal["discrete", "range"]
    values: tuple[float, ...] = ()
    min_s: float = 0.0
    max_s: float = 0.0
    step_s: float = 1.0

    def resolve(self, target_s: float) -> float:
        """Smallest legal duration >= target; the longest available if none fits."""
        if self.kind == "discrete":
            legal = sorted(self.values)
            if not legal:
                raise ValueError("discrete DurationSupport with no values")
            return next((v for v in legal if v >= target_s - 1e-6), legal[-1])
        clamped = min(max(target_s, self.min_s), self.max_s)
        steps = math.ceil((clamped - self.min_s) / self.step_s - 1e-6)
        return round(min(self.min_s + steps * self.step_s, self.max_s), 3)

    def describe(self) -> str:
        if self.kind == "discrete":
            return "/".join(f"{v:g}s" for v in sorted(self.values))
        return f"{self.min_s:g}-{self.max_s:g}s step {self.step_s:g}s"


@dataclass(frozen=True)
class Pricing:
    """Price plus the provenance needed to decide how far to trust it.

    ``cheapest_capable()`` may rank on ESTIMATED pricing. Paid submission must
    never trust it -- authorization is enforced separately against a caller
    supplied ``max_authorized_cost_cents`` (see ``planning.authorize``).
    """

    kind: Literal["per_second", "per_clip"]
    usd: Mapping[str, float]          # keyed by resolution label
    source: str                       # URL or "provider pricing page"
    verified_at: date | None
    confidence: PriceConfidence
    notes: str = ""

    def cents(self, duration_s: float, resolution: str) -> int:
        unit = self.usd.get(resolution)
        if unit is None:                       # fall back to the priciest listed
            unit = max(self.usd.values()) if self.usd else 0.0
        billed = duration_s if self.kind == "per_second" else 1.0
        return int(round(unit * billed * 100))

    @property
    def is_authoritative(self) -> bool:
        return self.confidence is PriceConfidence.VERIFIED

    def describe(self, duration_s: float, resolution: str) -> str:
        unit = self.usd.get(resolution, max(self.usd.values()) if self.usd else 0.0)
        per = f"${unit:.3f}/s" if self.kind == "per_second" else f"${unit:.3f}/clip"
        return f"{per} -> ${self.cents(duration_s, resolution) / 100:.2f}"

    def staleness_days(self, today: date | None = None) -> int | None:
        if self.verified_at is None:
            return None
        return ((today or date.today()) - self.verified_at).days


#: Legacy payload shape, used only by EXPERIMENTAL entries whose real schema
#: has not been read yet. ACTIVE entries must declare their own field list.
GENERIC_REQUEST_FIELDS = frozenset({
    "prompt", "image_url", "duration", "resolution", "aspect_ratio",
    "negative_prompt", "seed",
})


@dataclass(frozen=True)
class VideoModelCaps:
    """Everything the application needs to know about one image-to-video model."""

    model_key: str          # our stable key; never changes once published
    provider: str           # "fal" | "veo" | "runway" | "luma" | ...
    model_id: str           # provider-native id actually sent on the wire
    display_name: str
    tier: ModelTier
    adapter: str            # which VideoPort implementation serves it
    status: ModelStatus

    image_to_video: bool
    durations: DurationSupport
    resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    max_reference_images: int
    supports_negative_prompt: bool
    supports_seed: bool
    audio: AudioBehavior

    pricing: Pricing
    typical_latency_s: int          # seed value; replaced by measured p50
    max_wait_s: int
    retention_hours: int | None     # None = provider keeps media indefinitely

    #: Exact provider input fields this endpoint accepts. The adapter filters
    #: its payload through this allowlist, so sending a field the model does
    #: not define is impossible. Verified against the provider's OpenAPI schema
    #: for ACTIVE entries; EXPERIMENTAL entries fall back to a generic set.
    request_fields: frozenset[str] = frozenset(GENERIC_REQUEST_FIELDS)
    #: Fixed provider params merged into every request (e.g. disabling a
    #: provider-side prompt rewriter).
    extra_params: Mapping[str, Any] = field(default_factory=dict)
    #: False when the endpoint has no resolution / aspect input at all -- the
    #: output follows the model or the input image. Planning must not pretend
    #: to control what the API does not expose.
    resolution_selectable: bool = True
    aspect_selectable: bool = True

    docs_url: str = ""
    pinned_version: str | None = None   # provider-reported version, if any
    notes: str = ""

    @staticmethod
    def _height(label: str) -> int:
        m = re.search(r"(\d+)", label)
        return int(m.group(1)) if m else 0

    def best_resolution(self, preferred: str) -> str:
        """Exact match wins; else the largest option not exceeding `preferred`;
        else the smallest available. Returns the provider's literal token
        (fal uses '768P' where we say '768p') so adapters can send it verbatim."""
        if not self.resolutions:
            return preferred
        for r in self.resolutions:
            if r.lower() == preferred.lower():
                return r
        want = self._height(preferred)
        under = [r for r in self.resolutions if self._height(r) <= want]
        return max(under, key=self._height) if under else min(
            self.resolutions, key=self._height)

    def capability_chips(self) -> list[str]:
        chips = [
            self.durations.describe(),
            "/".join(self.resolutions),
            f"{self.max_reference_images} refs" if self.max_reference_images
            else "no refs",
        ]
        if not self.supports_negative_prompt:
            chips.append("no negative prompt")
        if not self.resolution_selectable:
            chips.append("fixed resolution")
        if self.audio is AudioBehavior.ALWAYS_ON:
            chips.append("audio forced on")
        if self.retention_hours:
            chips.append(f"expires {self.retention_hours}h")
        return chips


def index_by_key(entries: Iterable[VideoModelCaps]) -> dict[str, VideoModelCaps]:
    out: dict[str, VideoModelCaps] = {}
    for e in entries:
        if e.model_key in out:
            raise ValueError(f"duplicate model_key: {e.model_key}")
        out[e.model_key] = e
    return out


# --------------------------------------------------------------------------- #
# The catalogue.
#
# Pricing carries its own provenance. Where `confidence` is ESTIMATED the figure
# is good enough to rank models against each other and to show the operator an
# indicative number, but a paid run still requires an explicit authorized cap.
#
# `status` records how well the *request shape* is understood:
#   ACTIVE        - submit/poll/fetch verified against live provider docs
#   EXPERIMENTAL  - written from partial docs; --allow-experimental required
#
# Adding a model is a new entry here. No migration, no endpoint, no UI change.
# --------------------------------------------------------------------------- #

_FAL_DOCS = "https://fal.ai/docs/model-endpoints/queue"
_VEO_DOCS = "https://ai.google.dev/gemini-api/docs/veo"
_VERIFIED_ON = date(2026, 8, 25)
#: Entries whose model id, input schema and price were read directly from the
#: provider's OpenAPI document and model page over raw HTTP on this date.
_SCHEMA_VERIFIED = date(2026, 8, 26)
_KLING_PAGE = "https://fal.ai/models/fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
_HAILUO_PRO_PAGE = "https://fal.ai/models/fal-ai/minimax/hailuo-02/pro/image-to-video"
_HAILUO_STD_PAGE = "https://fal.ai/models/fal-ai/minimax/hailuo-02/standard/image-to-video"

CATALOG: dict[str, VideoModelCaps] = index_by_key([
    # ---------------- economy workhorse -------------------------------------
    VideoModelCaps(
        model_key="kling-2.5-turbo-i2v",
        provider="fal",
        model_id="fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        display_name="Kling 2.5 Turbo Pro",
        tier=ModelTier.ECONOMY,
        adapter="fal",
        status=ModelStatus.ACTIVE,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 10.0)),
        resolutions=("1080p",),
        aspect_ratios=("16:9", "9:16", "1:1"),
        resolution_selectable=False,
        aspect_selectable=False,
        max_reference_images=0,
        supports_negative_prompt=True,
        supports_seed=False,
        audio=AudioBehavior.NONE,
        request_fields=frozenset({"prompt", "image_url", "duration",
                                  "negative_prompt", "cfg_scale"}),
        extra_params={"cfg_scale": 0.5},
        pricing=Pricing(
            kind="per_second",
            usd={"1080p": 0.07},
            source=_KLING_PAGE,
            verified_at=_SCHEMA_VERIFIED,
            confidence=PriceConfidence.VERIFIED,
            notes="Model page: $0.35 for 5s, +$0.07 per additional second. "
                  "Base is exactly 5 x $0.07, so flat per-second is "
                  "algebraically identical for every legal duration.",
        ),
        # Measured: 78.6s across two runs (docs/adr/001-bakeoff-results.md).
        typical_latency_s=79,
        max_wait_s=1200,
        retention_hours=None,
        docs_url=_KLING_PAGE,
        notes="Expected MVP workhorse. Schema verified: no seed, no resolution "
              "and no aspect_ratio inputs -- output follows the input image, so "
              "the approved first frame is the ONLY consistency anchor. "
              "Output resolution is unverified; the first run's ffprobe settles "
              "it. cfg_scale (0-1) is the prompt-adherence lever.",
    ),
    # ---------------- mid tier ----------------------------------------------
    VideoModelCaps(
        model_key="hailuo-02-pro-i2v",
        provider="fal",
        model_id="fal-ai/minimax/hailuo-02/pro/image-to-video",
        display_name="Hailuo 02 Pro",
        tier=ModelTier.STANDARD,
        adapter="fal",
        status=ModelStatus.ACTIVE,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(6.0,)),
        resolutions=("1080p",),
        aspect_ratios=("16:9", "9:16"),
        resolution_selectable=False,
        aspect_selectable=False,
        max_reference_images=0,
        supports_negative_prompt=False,
        supports_seed=False,
        audio=AudioBehavior.NONE,
        request_fields=frozenset({"prompt", "image_url", "prompt_optimizer"}),
        # Provider-side prompt rewriting defaults to ON and would silently
        # replace the prompt we composed, breaking both determinism and the
        # reproducibility of input_hash. Always off.
        extra_params={"prompt_optimizer": False},
        pricing=Pricing(
            kind="per_second",
            usd={"1080p": 0.08},
            source=_HAILUO_PRO_PAGE,
            verified_at=_SCHEMA_VERIFIED,
            confidence=PriceConfidence.VERIFIED,
            notes="Model page: $0.08/sec; a 6s video costs $0.48.",
        ),
        typical_latency_s=180,
        max_wait_s=1500,
        retention_hours=None,
        docs_url=_HAILUO_PRO_PAGE,
        notes="Schema verified: NO duration, resolution, seed or negative "
              "prompt inputs. Fixed 6s at 1080p -- the earlier 10s option was "
              "fiction and would have desynced the timeline.",
    ),
    # ---------------- economy alternative: same family, half the price ------
    VideoModelCaps(
        model_key="hailuo-02-standard-i2v",
        provider="fal",
        model_id="fal-ai/minimax/hailuo-02/standard/image-to-video",
        display_name="Hailuo 02 Standard",
        tier=ModelTier.ECONOMY,
        adapter="fal",
        status=ModelStatus.ACTIVE,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(6.0, 10.0)),
        resolutions=("768P", "512P"),          # provider's literal tokens
        aspect_ratios=("16:9", "9:16"),
        resolution_selectable=True,
        aspect_selectable=False,
        max_reference_images=0,
        supports_negative_prompt=False,
        supports_seed=False,
        audio=AudioBehavior.NONE,
        request_fields=frozenset({"prompt", "image_url", "duration",
                                  "resolution", "prompt_optimizer"}),
        extra_params={"prompt_optimizer": False},
        pricing=Pricing(
            kind="per_second",
            usd={"768P": 0.045, "512P": 0.017},
            source=_HAILUO_STD_PAGE,
            verified_at=_SCHEMA_VERIFIED,
            confidence=PriceConfidence.VERIFIED,
            notes="Model page: 768P $0.045/sec (6s = $0.27), 512P $0.017/sec.",
        ),
        # Measured: 90.7s on one run (docs/adr/001-bakeoff-results.md).
        typical_latency_s=91,
        max_wait_s=1500,
        retention_hours=None,
        docs_url=_HAILUO_STD_PAGE,
        notes="Cheapest verified entry and MORE configurable than Pro: real "
              "duration (6/10s) and resolution (768P/512P) inputs. 768P is "
              "below 1080p delivery, so judge upscaled quality in the "
              "contact sheet before adopting it.",
    ),
    # ---------------- premium benchmark -------------------------------------
    VideoModelCaps(
        model_key="veo-3.1-standard-i2v",
        provider="veo",
        model_id="veo-3.1-generate-preview",
        display_name="Veo 3.1",
        tier=ModelTier.PREMIUM,
        adapter="veo",
        status=ModelStatus.ACTIVE,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(4.0, 6.0, 8.0)),
        resolutions=("720p", "1080p"),
        aspect_ratios=("16:9", "9:16"),
        max_reference_images=3,
        supports_negative_prompt=False,
        supports_seed=True,
        audio=AudioBehavior.ALWAYS_ON,
        pricing=Pricing(
            kind="per_second",
            usd={"720p": 0.40, "1080p": 0.40},
            source=_VEO_DOCS + " (Gemini API pricing page)",
            verified_at=_VERIFIED_ON,
            confidence=PriceConfidence.VERIFIED,
            notes="Audio included in the rate. Billed only on successful "
                  "generation.",
        ),
        typical_latency_s=180,
        max_wait_s=1800,
        retention_hours=48,
        docs_url=_VEO_DOCS,
        notes="PREMIUM benchmark. Only model here with reference images (<=3). "
              "Run it on a reduced sample via sample_limit.",
    ),

    # ================= not enabled by default ================================
    VideoModelCaps(
        model_key="veo-3.1-fast-i2v",
        provider="veo",
        model_id="veo-3.1-fast-generate-preview",
        display_name="Veo 3.1 Fast",
        tier=ModelTier.STANDARD,
        adapter="veo",
        status=ModelStatus.ACTIVE,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(4.0, 6.0, 8.0)),
        resolutions=("720p", "1080p"),
        aspect_ratios=("16:9", "9:16"),
        max_reference_images=3,
        supports_negative_prompt=False,
        supports_seed=True,
        audio=AudioBehavior.ALWAYS_ON,
        pricing=Pricing(
            kind="per_second",
            usd={"720p": 0.10, "1080p": 0.12},
            source=_VEO_DOCS + " (Gemini API pricing page)",
            verified_at=_VERIFIED_ON,
            confidence=PriceConfidence.VERIFIED,
        ),
        typical_latency_s=150,
        max_wait_s=1500,
        retention_hours=48,
        docs_url=_VEO_DOCS,
        notes="Reference images at a quarter of Standard's price. Strong "
              "candidate to replace the premium slot after the bake-off.",
    ),
    VideoModelCaps(
        model_key="wan-2.5-i2v",
        provider="fal",
        model_id="fal-ai/wan-25-preview/image-to-video",
        display_name="Wan 2.5",
        tier=ModelTier.ECONOMY,
        adapter="fal",
        status=ModelStatus.EXPERIMENTAL,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 10.0)),
        resolutions=("480p", "720p"),
        aspect_ratios=("16:9", "9:16"),
        max_reference_images=0,
        supports_negative_prompt=True,
        supports_seed=True,
        audio=AudioBehavior.NONE,
        pricing=Pricing(
            kind="per_second",
            usd={"480p": 0.05, "720p": 0.05},
            source="aggregated third-party pricing surveys",
            verified_at=_VERIFIED_ON,
            confidence=PriceConfidence.ESTIMATED,
        ),
        typical_latency_s=90,
        max_wait_s=900,
        retention_hours=None,
        docs_url=_FAL_DOCS,
        notes="Cheapest listed option. Verify model_id on the fal model page.",
    ),
    VideoModelCaps(
        model_key="runway-gen4-turbo-i2v",
        provider="runway",
        model_id="gen4_turbo",
        display_name="Runway Gen-4 Turbo",
        tier=ModelTier.STANDARD,
        adapter="task_api",
        status=ModelStatus.EXPERIMENTAL,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 10.0)),
        resolutions=("720p", "1080p"),
        aspect_ratios=("16:9", "9:16"),
        max_reference_images=0,
        supports_negative_prompt=False,
        supports_seed=True,
        audio=AudioBehavior.NONE,
        pricing=Pricing(
            kind="per_second",
            usd={"720p": 0.12, "1080p": 0.15},
            source="aggregated third-party pricing surveys",
            verified_at=_VERIFIED_ON,
            confidence=PriceConfidence.ESTIMATED,
        ),
        typical_latency_s=100,
        max_wait_s=900,
        retention_hours=None,
        docs_url="https://docs.dev.runwayml.com/",
        notes="POST /v1/image_to_video, GET /v1/tasks/{id} confirmed at shape "
              "level only. Confirm body field names before paid use.",
    ),
    VideoModelCaps(
        model_key="luma-ray2-i2v",
        provider="luma",
        model_id="ray-2",
        display_name="Luma Ray 2",
        tier=ModelTier.STANDARD,
        adapter="task_api",
        status=ModelStatus.EXPERIMENTAL,
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 9.0)),
        resolutions=("720p", "1080p"),
        aspect_ratios=("16:9", "9:16"),
        max_reference_images=0,
        supports_negative_prompt=False,
        supports_seed=False,
        audio=AudioBehavior.NONE,
        pricing=Pricing(
            kind="per_second",
            usd={"720p": 0.10, "1080p": 0.14},
            source="aggregated third-party pricing surveys",
            verified_at=_VERIFIED_ON,
            confidence=PriceConfidence.ESTIMATED,
        ),
        typical_latency_s=120,
        max_wait_s=1200,
        retention_hours=None,
        docs_url="https://docs.lumalabs.ai/",
        notes="Keyframe-based request body could not be verified from public "
              "docs. Confirm before enabling.",
    ),
])


def get(model_key: str) -> VideoModelCaps:
    try:
        return CATALOG[model_key]
    except KeyError:
        raise KeyError(
            f"unknown model_key {model_key!r}. Known keys: "
            + ", ".join(sorted(CATALOG))
        ) from None


def with_measured_latency(model_key: str, p50_s: int) -> VideoModelCaps:
    """Return a copy with measured latency folded in (post bake-off update)."""
    return replace(get(model_key), typical_latency_s=p50_s)
