"""Application settings.

Everything the app needs to run comes from the environment, so the same image
runs locally and on the VPS with no code change.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "apps/api/.env"),
        env_file_encoding="utf-8", extra="ignore",
    )

    env: Literal["local", "prod"] = "local"
    debug: bool = True

    # --- database ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://localhost/hbday_zee_dev"
    db_echo: bool = False
    db_pool_size: int = 5

    # --- redis (unused until M3, declared so compose and config agree) -----
    redis_url: str = "redis://localhost:6379/0"

    # --- auth -------------------------------------------------------------
    #: Single-tenant gate, not a product surface (ARCHITECTURE D-auth).
    #: MUST be overridden in prod; startup refuses the default there.
    app_passphrase: str = "change-me"
    session_secret: str = "dev-only-insecure-secret"
    session_cookie_name: str = "hbz_session"
    session_max_age_s: int = 60 * 60 * 24 * 30
    session_cookie_secure: bool = False
    owner_email: str = "owner@localhost"
    owner_name: str = "Owner"

    # --- storage ----------------------------------------------------------
    storage_dir: Path = REPO_ROOT / "storage"

    # --- http -------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            # A sync URL silently breaks the async engine much later; fix it here.
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations synchronously."""
        return self.database_url.replace("+asyncpg", "")

    def assert_production_safe(self) -> None:
        if self.env != "prod":
            return
        problems = []
        if self.app_passphrase == "change-me":
            problems.append("APP_PASSPHRASE is still the default")
        if self.session_secret == "dev-only-insecure-secret":
            problems.append("SESSION_SECRET is still the default")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE should be true behind TLS")
        if problems:
            raise RuntimeError("refusing to start in prod: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
