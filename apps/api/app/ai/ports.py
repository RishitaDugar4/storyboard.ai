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
