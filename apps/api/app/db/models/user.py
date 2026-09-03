from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Timestamps, UUIDPk


class User(Base, UUIDPk, Timestamps):
    """Single-tenant by design: in practice this table holds one row.

    It exists so ownership is a real foreign key rather than an assumption,
    which keeps the deletion cascade honest.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
