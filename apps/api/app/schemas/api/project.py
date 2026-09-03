from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ...db.models.project import ProjectStage

ASPECT_RATIOS = ("16:9", "9:16")


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16)$")
    style_preset: str = Field(default="storybook_gouache", max_length=60)
    budget_cents: int = Field(default=6000, ge=0, le=1_000_000)


class ProjectUpdate(BaseModel):
    """All fields optional; only what is sent is changed."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    stage: ProjectStage | None = None
    style_preset: str | None = Field(default=None, max_length=60)
    style_bible: dict | None = None
    default_model_key: str | None = Field(default=None, max_length=64)
    allow_premium: bool | None = None
    budget_cents: int | None = Field(default=None, ge=0, le=1_000_000)


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    stage: ProjectStage
    aspect_ratio: str
    image_size: str
    style_preset: str
    style_bible: dict | None
    default_model_key: str | None
    allow_premium: bool
    budget_cents: int
    spent_cents: int
    share_token: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def remaining_cents(self) -> int:
        return max(0, self.budget_cents - self.spent_cents)


class ProjectList(BaseModel):
    items: list[ProjectRead]
    total: int
