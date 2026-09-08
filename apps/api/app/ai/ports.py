"""AI capability ports.

Adapters return domain types; a provider SDK object never leaves the adapter.
Cross-cutting concerns (retry, cost accounting, tracing) are decorators that
implement the same Protocol, so they are written once and apply to every
provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AIErrorKind(StrEnum):
    TRANSIENT = "transient"      # 429/5xx/timeout -- retry with backoff
    QUOTA = "quota"              # provider cap -- long defer, no attempt spent
    REFUSAL = "refusal"          # safety decline -- never retry as-is
    INVALID = "invalid"          # schema unfixable after repair -- surface raw
    AUTH = "auth"
    BUDGET = "budget"            # our own cap, checked before the call
    #: A provider deleted the media before we downloaded it (Veo keeps clips
    #: 48h). Not retryable as a download -- the generation must be re-run, and
    #: paid for again, which is a decision for a human rather than a retry.
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class AIError(RuntimeError):
    def __init__(self, kind: AIErrorKind, code: str, detail: str = "",
                 raw: str | None = None) -> None:
        super().__init__(f"{kind}:{code}: {detail}")
        self.kind, self.code, self.detail, self.raw = kind, code, detail, raw

    @property
    def retryable(self) -> bool:
        return self.kind in (AIErrorKind.TRANSIENT, AIErrorKind.QUOTA)


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    cost_cents: float = 0.0


@dataclass
class StructuredResult(Generic[T]):
    value: T
    usage: Usage
    #: True when the first response failed our cross-field validators and a
    #: second call fixed it. Worth surfacing: a schema that repairs often is a
    #: prompt problem, not a model problem.
    repaired: bool = False
    repair_errors: list[str] = field(default_factory=list)
    raw_text: str | None = None


@dataclass
class ImageResult:
    data: bytes
    mime: str
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    revised_prompt: str | None = None


class ImagePort(Protocol):
    """Still generation.

    `n` candidates in one call where the provider supports it: human selection
    from two options is the real character-consistency mechanism in the MVP,
    and asking for both at once is cheaper than two round trips.
    """

    model: str

    async def generate(
        self, *, positive: str, negative: str, size: str,
        seed: int | None = None, n: int = 1,
    ) -> tuple[list[ImageResult], Usage]: ...


@dataclass
class SpeechResult:
    data: bytes
    mime: str
    #: Exact, not estimated. Every downstream duration -- the shot's screen
    #: time, the subtitle cue, the audio offset -- is built on this number.
    duration_ms: int
    sample_rate: int
    voice: str


class SpeechPort(Protocol):
    model: str
    provider: str

    async def synthesize(self, *, text: str, voice: str,
                         style: str | None = None) -> tuple[SpeechResult, Usage]: ...

    def voices(self) -> list[str]: ...


class TextPort(Protocol):
    async def generate_structured(
        self, *, schema: type[T], system: str, user: str,
        max_tokens: int = 16000, effort: str = "high",
        cache_prefix: str | None = None,
    ) -> StructuredResult[T]: ...


# --------------------------------------------------------------------------- #
# Motion. The fourth capability.
#
# Unlike text, image and speech, video generation is asynchronous by nature:
# submit, poll, fetch. That shape is deliberately NOT hidden behind a
# synchronous facade -- a 90-second provider wait held open inside one job
# would occupy a worker slot for the whole time and die with it, so the job
# layer needs the three phases separately (ARCHITECTURE 8.4).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VideoRequest:
    """Fully resolved. Every field here has already passed through planning.

    Adapters contain no policy: duration snapping, reference truncation,
    pricing and authorization all happened before this object was built.
    """

    model_key: str
    model_id: str
    #: The APPROVED still, as bytes. Always image-to-video -- the keyframe is
    #: the consistency anchor, and for models with no reference-image input it
    #: is the ONLY one.
    first_frame: bytes
    first_frame_mime: str
    prompt: str
    negative_prompt: str | None      # None when the model has no such input
    reference_images: list[bytes]    # already truncated to the model's limit
    duration_s: float                # already snapped to a legal value
    resolution: str                  # already resolved
    aspect_ratio: str
    seed: int | None


@dataclass
class Submission:
    provider_job_id: str
    endpoint: str
    #: Providers that delete media (Veo: +48h). None means kept indefinitely.
    expires_at: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class OperationState:
    done: bool
    error: AIError | None = None
    video_uri: str | None = None
    #: Populated only where the provider actually reports a charge. fal does
    #: not, so cost stays estimated and the budget gate governs.
    reported_cost_cents: int | None = None
    model_version: str | None = None
    progress_hint: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class VideoResult:
    data: bytes
    mime: str
    #: ffprobe truth, filled in by the caller after download. The request's
    #: duration is a promise; this is what actually arrived, and the bake-off
    #: measured providers missing it by up to 125ms.
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool = False


class VideoPort(Protocol):
    """One interface for every image-to-video backend.

    Implementations are thin. Everything the app reasons about lives in the
    catalogue, not in these methods -- which is what makes adding a model a
    data change rather than a code change.
    """

    name: str

    def serves(self, adapter_name: str) -> bool: ...
    async def submit(self, req: VideoRequest) -> Submission: ...
    async def poll(self, sub: Submission) -> OperationState: ...
    async def fetch(self, state: OperationState) -> VideoResult: ...
    async def aclose(self) -> None: ...
