from __future__ import annotations

import enum
import uuid

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, Timestamps, UUIDPk


class ProjectStage(enum.StrEnum):
    DRAFT = "draft"
    ANALYZED = "analyzed"
    STORYBOARDED = "storyboarded"
    CHARACTERS_LOCKED = "characters_locked"
    STILLS = "stills"
    NARRATION = "narration"
    PREVIEWED = "previewed"
    MOTION = "motion"
    RENDERED = "rendered"


class Project(Base, UUIDPk, Timestamps):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_owner_updated", "owner_id", "updated_at"),
        CheckConstraint("budget_cents >= 0", name="budget_nonnegative"),
        CheckConstraint("spent_cents >= 0", name="spent_nonnegative"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    stage: Mapped[ProjectStage] = mapped_column(
        Enum(ProjectStage, name="project_stage", native_enum=True,
             values_callable=lambda e: [m.value for m in e]),
        default=ProjectStage.DRAFT, nullable=False)

    aspect_ratio: Mapped[str] = mapped_column(String(12), default="16:9",
                                              nullable=False)
    image_size: Mapped[str] = mapped_column(String(20), default="1920x1080",
                                            nullable=False)
    style_preset: Mapped[str] = mapped_column(String(60),
                                              default="storybook_gouache",
                                              nullable=False)
    style_bible: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: Default narrator voice; a character with its own voice overrides it.
    narrator_voice_id: Mapped[str | None] = mapped_column(String(40),
                                                          nullable=True)
    #: Blob key of the licensed music bed, mixed well under the narration.
    music_track_key: Mapped[str | None] = mapped_column(String(500),
                                                        nullable=True)

    #: Motion defaults. Premium spend is opt-in per project (ARCHITECTURE D5).
    default_model_key: Mapped[str | None] = mapped_column(String(64),
                                                          nullable=True)
    allow_premium: Mapped[bool] = mapped_column(default=False, nullable=False)

    budget_cents: Mapped[int] = mapped_column(Integer, default=6000,
                                              nullable=False)
    spent_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    share_token: Mapped[str | None] = mapped_column(String(64), unique=True,
                                                    nullable=True)

    owner: Mapped["User"] = relationship(lazy="raise")  # noqa: F821
