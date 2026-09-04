"""Background job system.

The queue is chosen at startup: inline when no broker is configured (local
development and tests), arq when Redis is present. Handlers are identical
either way.
"""
from __future__ import annotations

import os
from functools import lru_cache

from .handlers import HANDLERS
from .queue import ArqQueue, InlineQueue, JobQueue


@lru_cache(maxsize=1)
def get_queue() -> JobQueue:
    if os.getenv("JOB_QUEUE", "inline").lower() == "arq":
        return ArqQueue(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    return InlineQueue(HANDLERS)


def reset_queue() -> None:
    get_queue.cache_clear()


__all__ = ["get_queue", "reset_queue", "HANDLERS", "JobQueue", "InlineQueue"]
