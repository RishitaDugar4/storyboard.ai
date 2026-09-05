"""Client-side request pacing.

A provider's rate limit is a fact about our account, not about one adapter
instance: every worker process shares the same key, so the counter has to live
somewhere they can all see it. Redis when it is configured, a process-local
window when it is not -- and the fallback is exactly right in the case that
produces it, a single process with no broker.

Paced, not policed. A caller that arrives over the limit waits for the next
slot rather than failing: a 429 costs a job attempt and a round trip through
the retry machinery, while a wait costs only the wait.

The window is a sliding log rather than a fixed bucket, because a fixed bucket
lets a burst at :59 and another at :01 put twice the limit inside one real
minute -- which is the shape of the failure this exists to prevent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque

log = logging.getLogger("hbz.ai.ratelimit")

#: Timestamps come from Redis TIME, not from each caller's clock, so processes
#: on skewed clocks still agree on what "the last minute" means.
_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
if redis.call('ZCARD', KEYS[1]) < limit then
    redis.call('ZADD', KEYS[1], now, ARGV[3])
    redis.call('PEXPIRE', KEYS[1], window)
    return 0
end

local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return math.max(1, math.ceil(tonumber(oldest[2]) + window - now))
"""


class RateLimiter:
    """Allow `limit` acquisitions per `window_s`, waiting for a slot.

    One instance per (provider, model) pair: quotas are per model, so a shared
    counter would throttle the text model on the speech model's traffic.
    """

    def __init__(self, key: str, limit: int, *, window_s: float = 60.0,
                 max_wait_s: float = 300.0, redis_url: str | None = None) -> None:
        self.key, self.limit = key, limit
        self.window_ms = int(window_s * 1000)
        self.max_wait_s = max_wait_s
        self._redis_url = redis_url if redis_url is not None else os.getenv("REDIS_URL")
        self._redis = None
        self._script = None
        self._local: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._warned = False

    async def acquire(self) -> float:
        """Block until a slot is free. Returns the seconds spent waiting.

        Raises TimeoutError if a slot would take longer than `max_wait_s` --
        an unbounded wait inside a worker slot is indistinguishable from a
        hang, and the reaper would eventually kill the job anyway.
        """
        started = time.monotonic()
        while True:
            wait_ms = await self._try()
            if wait_ms <= 0:
                return time.monotonic() - started
            waited = time.monotonic() - started
            if waited + wait_ms / 1000 > self.max_wait_s:
                raise TimeoutError(
                    f"{self.key}: no slot within {self.max_wait_s:.0f}s at "
                    f"{self.limit}/{self.window_ms / 1000:.0f}s")
            log.info("%s: at %d/%s, waiting %.1fs for a slot",
                     self.key, self.limit, self.window_ms // 1000, wait_ms / 1000)
            await asyncio.sleep(wait_ms / 1000)

    async def _try(self) -> float:
        """Milliseconds to wait, or 0 if a slot was taken."""
        client = await self._client()
        if client is None:
            return await self._try_local()
        try:
            return float(await self._script(
                keys=[f"ratelimit:{self.key}"],
                args=[self.window_ms, self.limit, uuid.uuid4().hex]))
        except Exception as exc:                        # noqa: BLE001
            # Losing Redis must not stop synthesis; degrade to per-process
            # pacing, which still holds the line for a single worker.
            if not self._warned:
                log.warning("%s: redis pacing unavailable (%s); "
                            "falling back to per-process", self.key, exc)
                self._warned = True
            self._redis = None
            return await self._try_local()

    async def _try_local(self) -> float:
        async with self._lock:
            now = time.monotonic() * 1000
            cutoff = now - self.window_ms
            while self._local and self._local[0] <= cutoff:
                self._local.popleft()
            if len(self._local) < self.limit:
                self._local.append(now)
                return 0.0
            return max(1.0, self._local[0] + self.window_ms - now)

    async def _client(self):
        if not self._redis_url:
            return None
        if self._redis is None:
            try:
                from redis.asyncio import Redis
                self._redis = Redis.from_url(self._redis_url)
                self._script = self._redis.register_script(_ACQUIRE_LUA)
            except Exception as exc:                    # noqa: BLE001
                if not self._warned:
                    log.warning("%s: cannot reach redis (%s); pacing per-process",
                                self.key, exc)
                    self._warned = True
                self._redis_url = None
                return None
        return self._redis


class RateLimitedSpeech:
    """SpeechPort decorator that paces `synthesize`.

    The wait happens inside the worker slot, which is deliberate: arq's
    concurrency is the burst, and holding a slot for the tail of a fan-out is
    cheaper than the 429 it replaces. With the default 8/min and a worker at
    8 concurrent jobs, the longest any line waits is about one window.
    """

    def __init__(self, inner, limiter: RateLimiter) -> None:
        self._inner, self._limiter = inner, limiter
        self.model = inner.model
        self.provider = inner.provider

    def voices(self) -> list[str]:
        return self._inner.voices()

    async def synthesize(self, *, text: str, voice: str,
                         style: str | None = None):
        from .ports import AIError, AIErrorKind
        try:
            waited = await self._limiter.acquire()
        except TimeoutError as exc:
            # Report it as the thing it is -- our own pacing, not the
            # provider's -- so the detail does not send anyone to Google's
            # quota console for a limit we imposed here.
            raise AIError(AIErrorKind.QUOTA, "local_rate_limit", str(exc)) from exc
        if waited > 0.5:
            log.info("speech: paced %.1fs before synthesizing", waited)
        return await self._inner.synthesize(text=text, voice=voice, style=style)
