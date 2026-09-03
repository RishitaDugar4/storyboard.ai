from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    passphrase: str = Field(min_length=1, max_length=200)


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
