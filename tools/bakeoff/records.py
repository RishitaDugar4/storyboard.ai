"""The generation record: complete reproducibility metadata for one clip.

Field names deliberately mirror the future ``motion_generations`` table so that
``results.jsonl`` can be replayed into the application database with a mechanical
mapping. ``to_motion_generations_row()`` is that mapping, and it is the contract
this harness exists to validate.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

RECORD_SCHEMA_VERSION = "1.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@dataclass
class GenerationRecord:
    # ---- harness identity (not persisted by the app) ----------------------
    record_schema_version: str
    run_id: str
    image_id: str
    case_id: str
    repeat_index: int

    # ---- WHO ran it -------------------------------------------------------
    adapter: str
    provider: str
    model_key: str
    model_id: str
    model_version: str | None          # provider-reported, when available
    provider_job_id: str | None
    provider_endpoint: str | None
    catalog_version: str
    composer_version: int

    # ---- EXACT inputs -----------------------------------------------------
    prompt: str
    negative_prompt: str | None
    first_frame_path: str
    first_frame_sha256: str
    reference_sha256: list[str]
    seed: int | None

    # ---- requested vs resolved -------------------------------------------
    requested_duration_s: float
    resolved_duration_s: float
    requested_resolution: str
    resolved_resolution: str
    aspect_ratio: str
    capability_warnings: list[dict[str, str]]

    # ---- narration fit (evaluated against RESOLVED duration) -------------
    narration_text: str
    narration_word_count: int
    narration_fit_status: str | None
    narration_word_budget: int | None
    narration_slack_s: float | None

    # ---- money ------------------------------------------------------------
    estimated_cost_cents: int
    actual_cost_cents: int | None
    cost_source: str                  # provider | estimated | unknown
    price_confidence: str
    price_source: str
    price_verified_at: str | None
    max_authorized_cost_cents: int | None

    # ---- lifecycle --------------------------------------------------------
    status: str                       # ready | failed | expired | cancelled | skipped
    error_code: str | None
    error_detail: str | None
    created_at: str
    submitted_at: str | None
    completed_at: str | None
    downloaded_at: str | None
    expires_at: str | None
    latency_ms: int | None
    poll_count: int

    # ---- result -----------------------------------------------------------
    input_hash: str
    output_path: str | None
    output_sha256: str | None
    output_bytes: int | None
    measured_duration_ms: int | None
    measured_fps: float | None
    measured_width: int | None
    measured_height: int | None
    measured_has_audio: bool | None
    probe_ok: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=False)

    # ------------------------------------------------------------------ #
    def to_motion_generations_row(self, *, project_id: str, shot_id: str) -> dict[str, Any]:
        """Map onto the application's ``motion_generations`` columns.

        Every column the application persists is present here. The harness
        fields above (run_id/image_id/case_id/repeat_index) are the only
        extras, and they have no counterpart by design.
        """
        return {
            "project_id": project_id,
            "shot_id": shot_id,
            "status": self.status,
            # who
            "provider": self.provider,
            "model_key": self.model_key,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "provider_job_id": self.provider_job_id,
            "provider_endpoint": self.provider_endpoint,
            "adapter": self.adapter,
            "catalog_version": self.catalog_version,
            "composer_version": self.composer_version,
            # what was asked
            "requested_duration_s": self.requested_duration_s,
            "resolved_duration_s": self.resolved_duration_s,
            "requested_resolution": self.requested_resolution,
            "resolved_resolution": self.resolved_resolution,
            "aspect_ratio": self.aspect_ratio,
            "motion_prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "seed": self.seed,
            "capability_warnings": self.capability_warnings,
            "first_frame_sha256": self.first_frame_sha256,
            "reference_sha256": self.reference_sha256,
            "input_hash": self.input_hash,
            # money
            "estimated_cost_cents": self.estimated_cost_cents,
            "actual_cost_cents": self.actual_cost_cents,
            "cost_source": self.cost_source,
            "price_confidence": self.price_confidence,
            "max_authorized_cost_cents": self.max_authorized_cost_cents,
            # result
            "measured_duration_ms": self.measured_duration_ms,
            "latency_ms": self.latency_ms,
            "poll_count": self.poll_count,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "submitted_at": self.submitted_at,
            "expires_at": self.expires_at,
            "downloaded_at": self.downloaded_at,
            "created_at": self.created_at,
        }
