from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    passphrase: str = Field(min_length=1, max_length=200)


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    last_login_at: datetime | None = None
