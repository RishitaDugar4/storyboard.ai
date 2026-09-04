"""The event bus must reach a *different process*.

Handlers run in the arq worker; SSE subscribers live in the API. An in-process
bus publishes into a process nobody is listening to. That was invisible for a
long time because the inline queue used in most tests runs handlers inside the
API, where a purely local bus happens to work.
"""
import asyncio
import os
import subprocess
import sys

import pytest

from app.jobs.events import Event, EventBus, RedisEventBus, get_bus, reset_bus


def test_arq_mode_selects_a_cross_process_bus(monkeypatch):
    reset_bus()
    monkeypatch.setenv("JOB_QUEUE", "arq")
    assert isinstance(get_bus(), RedisEventBus)
    reset_bus()
    monkeypatch.setenv("JOB_QUEUE", "inline")
    assert isinstance(get_bus(), EventBus)
    reset_bus()


PUBLISH_FROM_A_WORKER = '''
import asyncio, os, sys
sys.path.insert(0, {root!r}); os.environ["JOB_QUEUE"] = "arq"
from app.jobs.events import get_bus, Event, close_bus
async def main():
    await get_bus().publish(
        Event(type="entity", project_id={pid!r}, data={{"kind": "still"}}))
    await close_bus()
asyncio.run(main())
'''


@pytest.mark.asyncio
async def test_a_worker_process_reaches_an_api_subscriber(monkeypatch, tmp_path):
    reset_bus()
    monkeypatch.setenv("JOB_QUEUE", "arq")
    bus = get_bus()
    pid = f"proj-{tmp_path.name}"
    try:
        async with bus.subscribe(pid) as sub:
            subprocess.run(
                [sys.executable, "-c",
                 PUBLISH_FROM_A_WORKER.format(root=os.getcwd(), pid=pid)],
                check=True, capture_output=True)
            event = await asyncio.wait_for(sub.__anext__(), timeout=10)
        assert event.type == "entity"
        assert event.data == {"kind": "still"}
    finally:
        reset_bus()


@pytest.mark.asyncio
async def test_heartbeat_cancellation_does_not_deafen_the_subscription(monkeypatch):
    """The SSE loop cancels its pending read every HEARTBEAT_S seconds."""
    reset_bus()
    monkeypatch.setenv("JOB_QUEUE", "arq")
    bus = get_bus()
    try:
        async with bus.subscribe("proj-heartbeat") as sub:
            for _ in range(3):
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(sub.__anext__(), timeout=0.2)
            await bus.publish(
                Event(type="job", project_id="proj-heartbeat", data={"n": 1}))
            event = await asyncio.wait_for(sub.__anext__(), timeout=10)
        assert event.data == {"n": 1}
    finally:
        reset_bus()
