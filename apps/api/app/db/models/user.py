from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Timestamps, UUIDPk


class User(Base, UUIDPk, Timestamps):
    """An account. Small and closed: accounts are created from the CLI, not by
    signup, because this app has exactly the users it was built for.

    Each user owns their own projects; ownership is enforced by foreign key and
    checked on every read, so two people sharing the instance never see each
    other's work.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    passphrase_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
