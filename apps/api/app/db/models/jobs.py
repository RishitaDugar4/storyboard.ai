"""Background work, tracked in Postgres.

The queue only carries a job id; everything that matters -- status, attempts,
idempotency, progress, results -- lives in the database. That means a lost
Redis message costs a re-enqueue rather than a lost generation, and it makes
the job system testable without any broker at all.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, DateTime, Enum, ForeignKey, Index, Integer,
                        SmallInteger, String, Text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Timestamps, UUIDPk


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    #: Handed to a provider and waiting on it. Distinct from RUNNING because it
    #: occupies no worker slot -- the poller re-enqueues itself.
    AWAITING_PROVIDER = "awaiting_provider"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED,
            JobStatus.SKIPPED}
ACTIVE = {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.AWAITING_PROVIDER}


class Job(Base, UUIDPk, Timestamps):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_project_queued", "project_id", "queued_at"),
        Index("ix_jobs_active", "status",
              postgresql_where="status IN ('queued','running','awaiting_provider')"),
        Index("ix_jobs_parent", "parent_job_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=True,
             values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.QUEUED, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    #: sha256(kind | target | input_hash). UNIQUE, so enqueuing the same work
    #: twice is a no-op rather than a second charge.
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True,
                                                 nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    message: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class JobEvent(Base, Timestamps):
    """Append-only progress log. bigserial rather than a uuid because these are
    written far more often than they are addressed."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job_at", "job_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True,
                                    autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[str] = mapped_column(String(12), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AICall(Base, UUIDPk, Timestamps):
    """Every provider call, for cost accounting and post-hoc debugging."""

    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_calls_project_created", "project_id",
                            "created_at"),)

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    capability: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    units: Mapped[float | None] = mapped_column(nullable=True)
    cost_cents: Mapped[float] = mapped_column(default=0.0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ok: Mapped[bool] = mapped_column(default=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
