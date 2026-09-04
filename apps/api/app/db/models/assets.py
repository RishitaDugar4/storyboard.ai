"""Generated and uploaded media.

Assets are content-addressed by the hash of the inputs that produced them, so
freshness is *derived* rather than tracked with flags: a still is stale exactly
when the prompt that would produce it no longer matches the one that did. That
single rule replaces a whole class of bookkeeping, and at image and clip prices
it is also what stops the app paying twice for identical work.
"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import (Boolean, Enum, ForeignKey, Index, Integer, Numeric,
                        String, Text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Timestamps, UUIDPk


class AssetKind(enum.StrEnum):
    IMAGE = "image"
    CLIP = "clip"
    AUDIO = "audio"
    VIDEO = "video"
    SUBTITLE = "subtitle"
    POSTER = "poster"


class AssetSource(enum.StrEnum):
    GENERATED = "generated"
    #: Uploaded by hand. Permanently fresh: the user's own file cannot be
    #: "stale" relative to a prompt, and must never be silently regenerated.
    MANUAL = "manual"
    DERIVED = "derived"


class Asset(Base, UUIDPk, Timestamps):
    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_project_kind", "project_id", "kind", "created_at"),
        Index("ix_assets_input_hash", "input_hash"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[AssetKind] = mapped_column(
        Enum(AssetKind, name="asset_kind", native_enum=True,
             values_callable=lambda e: [m.value for m in e]), nullable=False)
    source: Mapped[AssetSource] = mapped_column(
        Enum(AssetSource, name="asset_source", native_enum=True,
             values_callable=lambda e: [m.value for m in e]),
        default=AssetSource.GENERATED, nullable=False)

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    mime: Mapped[str] = mapped_column(String(80), nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    has_audio: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Matches the producing entity's current hash while the asset is fresh.
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Exactly what was sent: prompt, seed, size. Debugging gold when an image
    #: comes back wrong months later.
    params: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    cost_cents: Mapped[float] = mapped_column(Numeric(10, 4), default=0,
                                              nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RenderProfile(enum.StrEnum):
    #: Ken Burns over approved stills. Costs nothing beyond the stills and is
    #: the product's default output, not a rehearsal for a better one.
    PREVIEW = "preview"
    FINAL = "final"


class Render(Base, UUIDPk, Timestamps):
    __tablename__ = "renders"
    __table_args__ = (Index("ix_renders_project_created", "project_id",
                            "created_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    profile: Mapped[RenderProfile] = mapped_column(
        Enum(RenderProfile, name="render_profile", native_enum=True,
             values_callable=lambda e: [m.value for m in e]), nullable=False)
    #: The exact Timeline that produced this file. A render is reproducible
    #: from it alone, and it explains months later why a cut looks the way it
    #: does.
    timeline: Mapped[dict] = mapped_column(JSONB, nullable=False)
    timeline_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued",
                                        nullable=False)
    video_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    poster_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    subtitle_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
