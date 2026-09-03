"""Async engine and request-scoped sessions."""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)

from ..config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    s = get_settings()
    return create_async_engine(
        s.database_url, echo=s.db_echo, pool_size=s.db_pool_size,
        pool_pre_ping=True, future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False,
                              autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Commits on success, rolls back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
