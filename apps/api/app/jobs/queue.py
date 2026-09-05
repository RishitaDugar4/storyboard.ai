"""Queue port.

The queue carries only a job id -- every fact about the work lives in Postgres
(see db/models/jobs.py). That keeps the broker replaceable and, more usefully,
makes the whole job system runnable without one: `InlineQueue` executes on the
spot, so tests and local development need no Redis at all.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable, Protocol

log = logging.getLogger("hbz.jobs")

#: A handler receives a job id and does the work. Everything it needs it loads
#: from the database itself.
Handler = Callable[[uuid.UUID], Awaitable[None]]


class JobQueue(Protocol):
    async def enqueue(self, kind: str, job_id: uuid.UUID,
                      defer_s: float = 0.0, attempt: int = 0) -> None: ...
    async def close(self) -> None: ...


class InlineQueue:
    """Runs handlers immediately, in this process.

    Not a mock: it is the honest single-process implementation, and it is what
    makes an end-to-end test of the pipeline possible without infrastructure.
    Deferred work is scheduled with asyncio rather than dropped.
    """

    def __init__(self, handlers: dict[str, Handler]) -> None:
        self._handlers = handlers
        self._tasks: set[asyncio.Task] = set()

    async def enqueue(self, kind: str, job_id: uuid.UUID,
                      defer_s: float = 0.0, attempt: int = 0) -> None:
        del attempt                     # no broker, so nothing to key on
        handler = self._handlers.get(kind)
        if handler is None:
            raise KeyError(f"no handler registered for job kind {kind!r}")

        async def run() -> None:
            if defer_s:
                await asyncio.sleep(defer_s)
            try:
                await handler(job_id)
            except Exception:                       # noqa: BLE001
                # The handler is responsible for recording its own failure on
                # the job row; this only stops one bad job killing the process.
                log.exception("inline job %s (%s) raised", job_id, kind)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for everything queued so far. Tests use this; production does
        not have a moment where 'everything' is a meaningful set."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()


class ArqQueue:
    """Redis-backed queue for the deployed stack.

    Job kinds map to arq function names one-to-one, and the payload is only the
    job id -- so a message lost in Redis costs a re-enqueue, never a result.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._redis = None
        self._lock = asyncio.Lock()

    async def _pool(self):
        """Created on first use, not at construction.

        The queue is resolved from inside a request handler, where the event
        loop is already running -- building the pool eagerly there means
        run_until_complete on a live loop, which raises.
        """
        if self._redis is None:
            async with self._lock:
                if self._redis is None:
                    from arq import create_pool
                    from arq.connections import RedisSettings
                    self._redis = await create_pool(
                        RedisSettings.from_dsn(self._dsn))
        return self._redis

    async def enqueue(self, kind: str, job_id: uuid.UUID,
                      defer_s: float = 0.0, attempt: int = 0) -> None:
        redis = await self._pool()
        await redis.enqueue_job(
            kind.replace(".", "_"), str(job_id),
            _defer_by=defer_s or None,
            # arq de-duplicates on this; the database UNIQUE on
            # idempotency_key is the real guard, this just avoids the round
            # trip when the same job is enqueued twice in quick succession.
            #
            # The attempt is part of the key, and must be. arq refuses a
            # _job_id it still holds a result for, and it holds results for
            # keep_result (an hour). A key fixed for the life of the job
            # therefore makes the FIRST attempt the only one: every requeue --
            # a retryable failure, the stranded-job sweep, the Retry button --
            # is accepted by us and silently dropped by the broker, and the row
            # sits in `queued` until someone notices. Per attempt, the dedup
            # still does its job within an attempt and retries get through.
            _job_id=f"{kind}:{job_id}:{attempt}",
        )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
