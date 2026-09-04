from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StoryWrite(BaseModel):
    raw_text: str = Field(min_length=1)


class StoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    raw_text: str
    word_count: int
    created_at: datetime


class StoryboardGenerateRequest(BaseModel):
    target_length_s: int = Field(default=90, ge=20, le=600)
    notes: str = Field(default="", max_length=800)


class StoryboardApplyRequest(BaseModel):
    #: Materialising over existing work destroys approved stills, recorded
    #: narration and paid clips, so it must be asked for explicitly.
    force: bool = False


class JobAccepted(BaseModel):
    job_id: uuid.UUID
    kind: str
    status: str
    #: False means identical work was already queued or finished, and this is
    #: that job rather than a second one.
    created: bool
