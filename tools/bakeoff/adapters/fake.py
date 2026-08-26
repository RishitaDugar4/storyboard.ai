"""FakeVideoAdapter: serves every catalogue entry, spends nothing.

Honours each model's declared capabilities exactly as the real adapter would --
including the resolved duration -- so the planning, polling, download, probing
and reporting paths are all genuinely exercised. This is what makes the harness
(and later the application) developable and CI-testable without spend.
"""
from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from pathlib import Path

from adapters.base import (
    ErrorKind, FetchResult, OperationState, ProviderError, Submission,
    VideoRequest,
)
from catalog import get as get_caps
from probe import sha256_file, synthesize_clip
from records import utcnow


class FakeVideoAdapter:
    name = "fake"

    def __init__(self, *, speed: float = 10.0, failure_rate: float = 0.0,
                 seed: int = 0) -> None:
        #: Simulated latency is the model's typical_latency_s divided by this.
        self.speed = max(1.0, speed)
        self.failure_rate = failure_rate
        self._rng = random.Random(seed)
        self._jobs: dict[str, dict] = {}

    def serves(self, adapter_name: str) -> bool:
        return True                      # stands in for any adapter

    async def submit(self, req: VideoRequest) -> Submission:
        caps = get_caps(req.model_key)
        job_id = f"fake-{req.model_key}-{self._rng.randrange(16**8):08x}"
        now = utcnow()
        self._jobs[job_id] = {
            "req": req,
            "ready_at": now + timedelta(seconds=caps.typical_latency_s / self.speed),
            "fail": self._rng.random() < self.failure_rate,
        }
        return Submission(
            provider_job_id=job_id,
            endpoint=f"fake://{req.model_id}",
            submitted_at=now,
            expires_at=(now + timedelta(hours=caps.retention_hours)
                        if caps.retention_hours else None),
            raw={"simulated": True},
        )

    async def poll(self, sub: Submission) -> OperationState:
        job = self._jobs.get(sub.provider_job_id)
        if job is None:
            return OperationState(done=True, error=ProviderError(
                ErrorKind.INVALID, "unknown_job", sub.provider_job_id))
        remaining = (job["ready_at"] - utcnow()).total_seconds()
        if remaining > 0:
            return OperationState(done=False,
                                  progress_hint=f"~{remaining:.0f}s remaining")
        if job["fail"]:
            return OperationState(done=True, error=ProviderError(
                ErrorKind.REFUSAL, "simulated_refusal",
                "FakeVideoAdapter simulated a content-policy refusal"))
        caps = get_caps(job["req"].model_key)
        return OperationState(
            done=True,
            video_uri=f"fake://{sub.provider_job_id}",
            # Most providers report nothing; mirror that so the harness proves
            # it can survive a missing actual cost.
            reported_cost_cents=None,
            model_version=caps.pinned_version or f"{caps.model_id}@fake",
            raw={"simulated": True},
        )

    async def fetch(self, state: OperationState, dest: Path) -> FetchResult:
        job_id = (state.video_uri or "").removeprefix("fake://")
        job = self._jobs.get(job_id)
        if job is None:
            raise ProviderError(ErrorKind.INVALID, "unknown_job", job_id)
        req: VideoRequest = job["req"]
        caps = get_caps(req.model_key)
        ok, note = await synthesize_clip(
            first_frame=req.first_frame_path, dest=dest,
            duration_s=req.duration_s, resolution=req.resolution,
            label=f"FAKE {caps.display_name} {req.duration_s:g}s {req.resolution}",
        )
        if not ok and not dest.exists():
            raise ProviderError(ErrorKind.UNKNOWN, "synthesis_failed", note)
        return FetchResult(path=dest, bytes_written=dest.stat().st_size,
                           sha256=sha256_file(dest),
                           content_type="video/mp4" if ok else "application/octet-stream")

    async def aclose(self) -> None:
        return None
