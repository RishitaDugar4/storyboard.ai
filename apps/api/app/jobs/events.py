"""Progress fan-out for SSE.

Events carry invalidations, never entity bodies: the client is told *what*
changed and refetches through the normal path. That removes a whole class of
bug where the pushed copy and the fetched copy disagree.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
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


_bus = EventBus()


def get_bus() -> EventBus:
    return _bus
