"""Story, storyboard, and the working rows materialised from it.

The storyboard arrives as one immutable JSON document (provenance: exactly what
the model produced, re-runnable) and is then materialised into normalised rows
(working state: what you edit, attach assets to, and regenerate per item).
Keeping both is the point -- editing a large JSONB blob in place is where this
design usually goes wrong.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (CheckConstraint, DateTime, Enum, ForeignKey, Index,
                        Integer, Numeric, SmallInteger, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, Timestamps, UUIDPk


def _enum(py_enum, name):
    return Enum(py_enum, name=name, native_enum=True,
                values_callable=lambda e: [m.value for m in e])


class ShotType(enum.StrEnum):
    ESTABLISHING = "establishing"; WIDE = "wide"; MEDIUM = "medium"
    CLOSE_UP = "close_up"; INSERT = "insert"; OVER_SHOULDER = "over_shoulder"
    POV = "pov"


class CameraMove(enum.StrEnum):
    STATIC = "static"; PUSH_IN = "push_in"; PULL_OUT = "pull_out"
    PAN_LEFT = "pan_left"; PAN_RIGHT = "pan_right"; TILT_UP = "tilt_up"
    TILT_DOWN = "tilt_down"; ORBIT = "orbit"; HANDHELD = "handheld"


class MotionMode(enum.StrEnum):
    KENBURNS = "kenburns"      # free: the approved still with a camera move
    GENERATED = "generated"    # a paid clip
    MANUAL = "manual"          # something you uploaded


# --------------------------------------------------------------------------- #
class StoryInput(Base, UUIDPk, Timestamps):
    """Versioned: revising the story must never destroy the storyboard you
    already curated from an earlier draft."""

    __tablename__ = "story_inputs"
    __table_args__ = (UniqueConstraint("project_id", "version",
                                       name="uq_story_inputs_project_version"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)


class StoryAnalysisDoc(Base, UUIDPk, Timestamps):
    __tablename__ = "story_analyses"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    story_input_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("story_inputs.id", ondelete="CASCADE"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_cents: Mapped[float] = mapped_column(Numeric(10, 4), default=0,
                                              nullable=False)
    repaired: Mapped[bool] = mapped_column(default=False, nullable=False)


class StoryboardDoc(Base, UUIDPk, Timestamps):
    """Immutable provenance. Materialised into rows by :func:`apply`."""

    __tablename__ = "storyboards"
    __table_args__ = (UniqueConstraint("project_id", "version",
                                       name="uq_storyboards_project_version"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    story_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("story_analyses.id", ondelete="SET NULL"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_cents: Mapped[float] = mapped_column(Numeric(10, 4), default=0,
                                              nullable=False)
    repaired: Mapped[bool] = mapped_column(default=False, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
class Character(Base, UUIDPk, Timestamps):
    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("project_id", "slug",
                                       name="uq_characters_project_slug"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    appearance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: Rendered once at lock time and then FROZEN. Every image prompt embeds
    #: this string byte-for-byte; paraphrasing between shots is the single
    #: biggest cause of character drift.
    appearance_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    voice: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Location(Base, UUIDPk, Timestamps):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("project_id", "slug",
                                       name="uq_locations_project_slug"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_fragment: Mapped[str] = mapped_column(Text, nullable=False)


class Scene(Base, UUIDPk, Timestamps):
    __tablename__ = "scenes"
    __table_args__ = (Index("ix_scenes_project_order", "project_id", "sort_order"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    #: Gaps of 1000 so a single-row update can insert between neighbours
    #: without renumbering the whole film.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    time_of_day: Mapped[str] = mapped_column(String(16), default="unspecified",
                                             nullable=False)
    mood: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    present_slugs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    shots: Mapped[list["Shot"]] = relationship(
        back_populates="scene", cascade="all, delete-orphan", lazy="selectin",
        order_by="Shot.sort_order")
    narration: Mapped[list["NarrationLine"]] = relationship(
        back_populates="scene", cascade="all, delete-orphan", lazy="selectin",
        order_by="NarrationLine.sort_order")


class Shot(Base, UUIDPk, Timestamps):
    __tablename__ = "shots"
    __table_args__ = (
        Index("ix_shots_scene_order", "scene_id", "sort_order"),
        Index("ix_shots_project_mode", "project_id", "motion_mode"),
        CheckConstraint("target_duration_s BETWEEN 2.5 AND 12.0",
                        name="target_duration_in_range"),
    )

    scene_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False)
    #: Denormalised so project-wide queries (freshness, cost, preflight) do not
    #: need a join through scenes.
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_type: Mapped[ShotType] = mapped_column(_enum(ShotType, "shot_type"),
                                                nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    composition_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    camera_move: Mapped[CameraMove] = mapped_column(
        _enum(CameraMove, "camera_move"), default=CameraMove.PUSH_IN,
        nullable=False)
    subject_motion: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ambient_sound: Mapped[str] = mapped_column(String(120), default="",
                                               nullable=False)
    motion_priority: Mapped[str] = mapped_column(String(8), default="low",
                                                 nullable=False)
    #: Authorial intent. NOT a provider grid -- which durations a model can
    #: actually produce is resolved later against the capability catalogue.
    target_duration_s: Mapped[float] = mapped_column(
        Numeric(4, 1), default=6.0, nullable=False)
    motion_mode: Mapped[MotionMode] = mapped_column(
        _enum(MotionMode, "motion_mode"), default=MotionMode.KENBURNS,
        nullable=False)
    preferred_model_key: Mapped[str | None] = mapped_column(String(64),
                                                            nullable=True)
    subject_slugs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    prompt_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    motion_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_image_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    selected_clip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    #: Freshness is derived, never stored as a flag: an asset is stale iff the
    #: hash that produced it differs from the current one.
    image_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    motion_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    scene: Mapped["Scene"] = relationship(back_populates="shots")


class NarrationLine(Base, UUIDPk, Timestamps):
    __tablename__ = "narration_lines"
    __table_args__ = (Index("ix_narration_scene_order", "scene_id", "sort_order"),)

    scene_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    shot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_slug: Mapped[str] = mapped_column(String(40), default="narrator",
                                              nullable=False)
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    delivery: Mapped[str] = mapped_column(String(16), default="neutral",
                                          nullable=False)
    audio_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    #: Measured from the rendered audio, never estimated. The timeline is built
    #: on this number.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    scene: Mapped["Scene"] = relationship(back_populates="narration")
