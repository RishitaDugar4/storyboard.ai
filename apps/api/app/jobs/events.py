"""Progress fan-out for SSE.

Events carry invalidations, never entity bodies: the client is told *what*
changed and refetches through the normal path. That removes a whole class of
bug where the pushed copy and the fetched copy disagree.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict

log = logging.getLogger("hbz.events")
from dataclasses import asdict, dataclass, field


@dataclass
class Event:
    type: str                       # "job" | "entity" | "cost" | "ping"
    project_id: str
    data: dict = field(default_factory=dict)

    def sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data)}\n\n"


class Subscription:
    """A registered listener.

    Registration happens in ``__init__`` rather than on first iteration: an
    async generator does not run its body until you consume it, so anything
    published between subscribing and the first ``__anext__`` would be silently
    dropped. For SSE that window is small; for anything that subscribes and
    then triggers work, it loses the event it was waiting for.
    """

    def __init__(self, bus: "EventBus", project_id: str, maxsize: int) -> None:
        self._bus = bus
        self._project_id = project_id
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        bus._register(project_id, self.queue)

    def close(self) -> None:
        self._bus._unregister(self._project_id, self.queue)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Event:
        return await self.queue.get()

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()


class EventBus:
    """In-process pub/sub, one queue per subscriber.

    Correct for a single API process, which is what this deployment is. A Redis
    implementation slots in behind the same methods when there is more than one.
    """

    def __init__(self, max_queue: int = 256) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._max = max_queue

    def _register(self, project_id: str, q: asyncio.Queue) -> None:
        self._subs[project_id].add(q)

    def _unregister(self, project_id: str, q: asyncio.Queue) -> None:
        self._subs[project_id].discard(q)
        if not self._subs[project_id]:
            self._subs.pop(project_id, None)

    def subscribe(self, project_id: str) -> Subscription:
        return Subscription(self, project_id, self._max)

    async def publish(self, event: Event) -> None:
        for q in list(self._subs.get(event.project_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A slow reader must not stall a worker. It resyncs on
                # reconnect, which the client does anyway.
                pass

    @property
    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._subs.values())


class RedisEventBus:
    """Cross-process fan-out.

    The in-process bus above is only correct when publisher and subscriber
    share a process. They do not: handlers run in the arq worker while SSE
    subscribers live in the API. Without this, every live update was published
    into a process nobody was listening to -- invisible in tests, because the
    inline queue runs handlers inside the API.
    """

    CHANNEL = "hbz:events:{project_id}"

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._redis = None
        self._lock = asyncio.Lock()
        self._local = EventBus()          # same-process listeners still work

    async def _client(self):
        if self._redis is None:
            async with self._lock:
                if self._redis is None:
                    import redis.asyncio as aioredis
                    self._redis = aioredis.from_url(self._dsn,
                                                    decode_responses=True)
        return self._redis

    async def publish(self, event: Event) -> None:
        try:
            client = await self._client()
            await client.publish(
                self.CHANNEL.format(project_id=event.project_id),
                json.dumps({"type": event.type, "data": event.data}))
        except Exception:
            # A dropped notification must never fail the job that produced it;
            # the client polls and resyncs on reconnect.
            log.warning("could not publish %s for %s", event.type,
                        event.project_id, exc_info=True)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    def subscribe(self, project_id: str) -> "RedisSubscription":
        return RedisSubscription(self, project_id)

    @property
    def subscriber_count(self) -> int:
        return self._local.subscriber_count


class RedisSubscription:
    """Reads redis on its own task, hands events over through a queue.

    The SSE loop cancels its pending __anext__ on every heartbeat. If that
    cancellation landed inside the redis socket read, the pubsub connection
    would be left mid-protocol and the subscription would silently go deaf.
    Awaiting a queue instead makes cancellation land somewhere harmless.
    """

    def __init__(self, bus: "RedisEventBus", project_id: str) -> None:
        self._bus = bus
        self._project_id = project_id
        self._pubsub = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)

    async def __aenter__(self) -> "RedisSubscription":
        client = await self._bus._client()
        self._pubsub = client.pubsub()
        await self._pubsub.subscribe(
            RedisEventBus.CHANNEL.format(project_id=self._project_id))
        self._task = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass

    async def _read(self) -> None:
        assert self._pubsub is not None
        try:
            while True:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=30.0)
                if message is None or message.get("type") != "message":
                    continue
                payload = json.loads(message["data"])
                event = Event(type=payload["type"],
                              project_id=self._project_id,
                              data=payload.get("data", {}))
                try:
                    self._queue.put_nowait(event)
                except asyncio.QueueFull:
                    # A client this far behind will resync on its next refetch;
                    # blocking here would stall every other subscriber.
                    log.warning("dropping event for slow subscriber %s",
                                self._project_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("event reader for %s stopped", self._project_id,
                        exc_info=True)

    def __aiter__(self) -> "RedisSubscription":
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()


_bus: EventBus | RedisEventBus | None = None


def get_bus():
    """Redis-backed when a broker is configured, in-process otherwise.

    The inline queue runs handlers in this process, so the local bus is correct
    there and needs no broker.
    """
    global _bus
    if _bus is None:
        if os.getenv("JOB_QUEUE", "inline").lower() == "arq":
            _bus = RedisEventBus(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        else:
            _bus = EventBus()
    return _bus


def reset_bus() -> None:
    global _bus
    _bus = None


async def close_bus() -> None:
    """Release the broker connection on process shutdown."""
    global _bus
    bus, _bus = _bus, None
    if isinstance(bus, RedisEventBus):
        await bus.close()
