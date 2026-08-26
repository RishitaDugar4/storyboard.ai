"""VideoPort: one interface for every image-to-video backend.

Implementations are thin and contain no policy. Duration snapping, reference
truncation, pricing, tier rules and authorization all happen in ``planning``
against the catalogue, before an adapter is ever called.

Graduates to ``apps/api/app/ai/adapters/``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class ErrorKind(StrEnum):
    #: 429/5xx/timeouts. Retry with backoff.
    TRANSIENT = "transient"
    #: Provider concurrency or daily cap. Retry after a LONG defer and do not
    #: consume an attempt -- otherwise a queue delay becomes a hard failure.
    QUOTA = "quota"
    #: Content policy / person-generation refusal. Never retry as-is.
    REFUSAL = "refusal"
    #: Bad request -- almost always a catalogue bug. Fix the data, not the call.
    INVALID = "invalid"
    #: Credentials missing or rejected.
    AUTH = "auth"
    #: Provider retention window elapsed before download.
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ProviderError(RuntimeError):
    def __init__(self, kind: ErrorKind, code: str, detail: str = "") -> None:
        super().__init__(f"{kind}:{code}: {detail}")
        self.kind = kind
        self.code = code
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.kind in (ErrorKind.TRANSIENT, ErrorKind.QUOTA)

    @property
    def consumes_attempt(self) -> bool:
        return self.kind is not ErrorKind.QUOTA


@dataclass(frozen=True)
class VideoRequest:
    """Fully resolved. Every field here already passed through planning."""

    model_key: str
    model_id: str
    first_frame_path: Path
    prompt: str
    negative_prompt: str | None
    reference_paths: list[Path]      # already truncated to the model's limit
    duration_s: float                # already snapped to a legal value
    resolution: str                  # already resolved
    aspect_ratio: str
    seed: int | None


@dataclass
class Submission:
    provider_job_id: str
    endpoint: str
    submitted_at: datetime
    expires_at: datetime | None = None    # providers that delete media
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OperationState:
    done: bool
    error: ProviderError | None = None
    video_uri: str | None = None
    reported_cost_cents: int | None = None
    model_version: str | None = None
    progress_hint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    path: Path
    bytes_written: int
    sha256: str
    content_type: str | None = None


@runtime_checkable
class VideoPort(Protocol):
    name: str

    def serves(self, adapter_name: str) -> bool: ...
    async def submit(self, req: VideoRequest) -> Submission: ...
    async def poll(self, sub: Submission) -> OperationState: ...
    async def fetch(self, state: OperationState, dest: Path) -> FetchResult: ...
    async def aclose(self) -> None: ...
