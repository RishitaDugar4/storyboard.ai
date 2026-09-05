"""M3: the pipeline as tracked background jobs.

Runs against the real database with the fake text provider and the inline
queue -- no Redis, no spend, but the same handlers, the same idempotency, and
the same materialisation that production uses.
"""
from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ["AI_TEXT_PROVIDER"] = "fake"
os.environ["JOB_QUEUE"] = "inline"

from app.auth import create_user                      # noqa: E402
from app.db.ids import uuid7                          # noqa: E402
from app.db.models import (Asset, AssetKind, AssetSource, Character,  # noqa: E402
                           Job, JobStatus, NarrationLine, Scene, Shot,
                           StoryboardDoc, User)
from app.db.session import (dispose_engine, get_engine,  # noqa: E402
                            get_sessionmaker)
from app.jobs import get_queue, reset_queue            # noqa: E402
from app.jobs.events import Event, get_bus            # noqa: E402
from app.jobs import service as jobs                   # noqa: E402
from app.main import create_app                        # noqa: E402

pytestmark = pytest.mark.asyncio

EMAIL, PASS = "jobs-test@local", "jobs-pass"
STORY = ("A keeper kept a light for forty years. One winter night the power "
         "failed and she turned the lens by hand until dawn, and eleven men "
         "came home who otherwise would not have.")


@pytest.fixture
async def client():
    reset_queue()
    async with get_sessionmaker()() as s:
        user = (await s.execute(select(User).where(

            User.email == EMAIL))).scalar_one_or_none()

        if user is None:

            user = await create_user(s, email=EMAIL,

                                     display_name="Jobs",

                                     passphrase=PASS)

        # The fixture owns this account's state: an account disabled

        # outside the suite must not silently break every test.

        user.is_active = True

        await s.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        await c.post("/api/v1/auth/session",
                     json={"email": EMAIL, "passphrase": PASS})
        yield c


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with get_sessionmaker()() as s:
        for t in ("job_events", "jobs", "ai_calls", "projects"):
            await s.execute(text(f"DELETE FROM {t}"))
        await s.commit()
    await dispose_engine()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_queue()


async def _project(client) -> str:
    r = await client.post("/api/v1/projects", json={"title": "Test film"})
    return r.json()["id"]


async def _settle(client, job_id: str) -> dict:
    await get_queue().drain()
    return (await client.get(f"/api/v1/jobs/{job_id}")).json()


# ---- story intake ---------------------------------------------------------
async def test_story_is_versioned(client):
    pid = await _project(client)
    a = await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    b = await client.put(f"/api/v1/projects/{pid}/story",
                         json={"raw_text": STORY + " And so it went."})
    assert a.json()["version"] == 1 and b.json()["version"] == 2
    assert (await client.get(f"/api/v1/projects/{pid}/story")).json()["version"] == 2


async def test_analyze_requires_a_story(client):
    pid = await _project(client)
    r = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    assert r.status_code == 409
    assert r.json()["code"] == "stage_precondition_failed"


async def test_storyboard_requires_an_analysis(client):
    pid = await _project(client)
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    r = await client.post(f"/api/v1/projects/{pid}/storyboard:generate", json={})
    assert r.status_code == 409


# ---- the pipeline ---------------------------------------------------------
async def test_analyze_runs_as_a_job_and_advances_the_stage(client):
    pid = await _project(client)
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})

    accepted = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    assert accepted.status_code == 202 and accepted.json()["created"] is True

    job = await _settle(client, accepted.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["progress"] == 100 and job["result"]["characters"] >= 1

    assert (await client.get(f"/api/v1/projects/{pid}/analysis")).status_code == 200
    assert (await client.get(f"/api/v1/projects/{pid}")).json()["stage"] == "analyzed"


async def test_full_pipeline_to_materialised_rows(client):
    pid = await _project(client)
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    a = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    assert (await _settle(client, a.json()["job_id"]))["status"] == "succeeded"

    g = await client.post(f"/api/v1/projects/{pid}/storyboard:generate",
                          json={"target_length_s": 90})
    job = await _settle(client, g.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["scenes"] >= 4

    listing = (await client.get(f"/api/v1/projects/{pid}/storyboards")).json()
    assert listing["total"] == 1
    sbid = listing["items"][0]["id"]

    applied = await client.post(
        f"/api/v1/projects/{pid}/storyboards/{sbid}:apply", json={})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["scenes"] >= 4 and body["shots"] >= body["scenes"]

    async with get_sessionmaker()() as s:
        chars = (await s.execute(select(Character).where(
            Character.project_id == uuid.UUID(pid)))).scalars().all()
        shots = (await s.execute(select(Shot).where(
            Shot.project_id == uuid.UUID(pid)))).scalars().all()
        lines = (await s.execute(select(NarrationLine).where(
            NarrationLine.project_id == uuid.UUID(pid)))).scalars().all()
    assert chars and shots and lines
    # The canon must be frozen into a single reusable string, not left as
    # structured fields for each prompt to re-render differently.
    assert chars[0].appearance_prompt and chars[0].name in chars[0].appearance_prompt
    # Ordering uses gaps so an insert between neighbours is one row update.
    assert sorted(s.sort_order for s in shots)[0] == 0
    # Narration must resolve to real shots, not dangle.
    assert any(l.shot_id is not None for l in lines)


async def test_apply_refuses_to_destroy_existing_work(client):
    pid = await _project(client)
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    a = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    await _settle(client, a.json()["job_id"])
    g = await client.post(f"/api/v1/projects/{pid}/storyboard:generate", json={})
    await _settle(client, g.json()["job_id"])
    sbid = (await client.get(f"/api/v1/projects/{pid}/storyboards")).json()["items"][0]["id"]
    await client.post(f"/api/v1/projects/{pid}/storyboards/{sbid}:apply", json={})

    # Simulate curation the user would lose: an approved still. A real asset
    # row, because selected_image_id is a foreign key -- fabricating an id
    # would only prove the test can write nonsense.
    async with get_sessionmaker()() as s:
        shot = (await s.execute(select(Shot).where(
            Shot.project_id == uuid.UUID(pid)).limit(1))).scalar_one()
        asset = Asset(id=uuid7(), project_id=uuid.UUID(pid),
                      kind=AssetKind.IMAGE, source=AssetSource.GENERATED,
                      storage_key="test/a.png", mime="image/png", bytes=1,
                      checksum="0" * 64)
        s.add(asset)
        await s.flush()
        shot.selected_image_id = asset.id
        await s.commit()

    refused = await client.post(
        f"/api/v1/projects/{pid}/storyboards/{sbid}:apply", json={})
    assert refused.status_code == 409
    assert refused.json()["code"] == "apply_would_destroy_work"
    assert "approved stills" in refused.json()["detail"]

    forced = await client.post(
        f"/api/v1/projects/{pid}/storyboards/{sbid}:apply", json={"force": True})
    assert forced.status_code == 200


# ---- idempotency ----------------------------------------------------------
async def test_identical_work_is_not_queued_twice(client):
    pid = await _project(client)
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    first = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    second = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["job_id"] == second.json()["job_id"]

    async with get_sessionmaker()() as s:
        n = len((await s.execute(select(Job).where(
            Job.project_id == uuid.UUID(pid)))).scalars().all())
    assert n == 1


async def test_a_different_story_is_different_work(client):
    pid = await _project(client)
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    first = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    await client.put(f"/api/v1/projects/{pid}/story",
                     json={"raw_text": STORY + " A different ending."})
    second = await client.post(f"/api/v1/projects/{pid}/story:analyze")
    assert first.json()["job_id"] != second.json()["job_id"]


async def test_claim_is_atomic(client):
    """At-least-once delivery must be harmless: the second claim finds nothing."""
    pid = await _project(client)
    async with get_sessionmaker()() as s:
        job, _ = await jobs.enqueue(s, project_id=uuid.UUID(pid),
                                    kind="story.analyze", input_hash="h")
        await s.commit()
        assert await jobs.claim(s, job.id) is not None
        assert await jobs.claim(s, job.id) is None


# ---- ownership ------------------------------------------------------------
async def test_another_users_project_is_invisible(client):
    pid = await _project(client)
    async with get_sessionmaker()() as s:
        other = (await s.execute(select(User).where(
            User.email == "other@local"))).scalar_one_or_none()
        if other is None:
            other = await create_user(s, email="other@local",
                                      display_name="Other",
                                      passphrase="other-pass")
        # This test owns the account's state, so disabling it outside the
        # suite cannot quietly break it.
        other.is_active = True
        await s.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as other:
        await other.post("/api/v1/auth/session",
                         json={"email": "other@local", "passphrase": "other-pass"})
        # 404, not 403: whether the project exists is not their business.
        assert (await other.get(f"/api/v1/projects/{pid}")).status_code == 404
        assert (await other.put(f"/api/v1/projects/{pid}/story",
                                json={"raw_text": STORY})).status_code == 404
        assert (await other.get("/api/v1/projects")).json()["total"] == 0


# ---- events ---------------------------------------------------------------
async def test_bus_delivers_to_subscribers_of_that_project_only():
    import asyncio
    bus = get_bus()
    async with bus.subscribe("p1") as mine:
        await bus.publish(Event("job", "p2", {"x": 1}))
        await bus.publish(Event("job", "p1", {"x": 2}))
        got = await asyncio.wait_for(mine.__anext__(), timeout=1)
    assert got.data == {"x": 2}


async def test_subscription_registers_before_iteration():
    """Publishing immediately after subscribing must not be lost."""
    import asyncio
    bus = get_bus()
    async with bus.subscribe("p9") as sub:
        await bus.publish(Event("job", "p9", {"n": 1}))
        got = await asyncio.wait_for(sub.__anext__(), timeout=1)
    assert got.data == {"n": 1}
    assert bus.subscriber_count == 0        # cleaned up on exit


async def test_entity_events_carry_an_id_and_a_reason_not_a_body():
    import asyncio
    bus = get_bus()
    pid = uuid.UUID(int=42)
    async with bus.subscribe(str(pid)) as sub:
        await jobs.notify_entity(pid, "storyboard", uuid.UUID(int=9), "ready")
        got = await asyncio.wait_for(sub.__anext__(), timeout=1)
    assert got.type == "entity"
    assert set(got.data) == {"type", "id", "reason"}   # no entity body


async def test_two_projects_with_the_same_story_do_not_collide(client):
    """The same text pasted into two projects is two units of work.

    Without the project in the idempotency key the second project silently
    receives the first one's job and is never analysed -- a failure that
    reports success and produces nothing.
    """
    a = await _project(client)
    b = await _project(client)
    for pid in (a, b):
        await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})

    first = await client.post(f"/api/v1/projects/{a}/story:analyze")
    second = await client.post(f"/api/v1/projects/{b}/story:analyze")
    assert first.json()["created"] is True
    assert second.json()["created"] is True
    assert first.json()["job_id"] != second.json()["job_id"]

    await get_queue().drain()
    for pid in (a, b):
        assert (await client.get(f"/api/v1/projects/{pid}/analysis")).status_code == 200


def test_idempotency_key_is_scoped_to_a_project():
    from app.jobs.service import idempotency_key
    p1, p2 = uuid.UUID(int=1), uuid.UUID(int=2)
    assert idempotency_key(p1, "story.analyze", None, "same") != \
           idempotency_key(p2, "story.analyze", None, "same")
    assert idempotency_key(p1, "story.analyze", None, "same") == \
           idempotency_key(p1, "story.analyze", None, "same")


# ---- retries actually reach the broker ------------------------------------
def test_the_broker_key_changes_with_the_attempt():
    """The bug this guards: arq refuses a _job_id it still holds a result for,
    and it holds them for keep_result (an hour). A key fixed for the life of a
    job therefore makes the first attempt the only one -- every requeue is
    accepted by us and silently dropped by the broker, and the row sits in
    `queued` until someone reads Redis."""
    from app.jobs.queue import ArqQueue

    class Recorder(ArqQueue):
        def __init__(self):
            super().__init__("redis://unused")
            self.ids: list[str] = []

        async def _pool(self):
            outer = self

            class FakeRedis:
                async def enqueue_job(self, _fn, _arg, *, _defer_by, _job_id):
                    outer.ids.append(_job_id)
            return FakeRedis()

    async def run():
        q = Recorder()
        jid = uuid7()
        for attempt in (0, 1, 2):
            await q.enqueue("narration.tts", jid, attempt=attempt)
        # Same attempt twice still de-duplicates: that is what the key is for.
        await q.enqueue("narration.tts", jid, attempt=2)
        return q.ids

    import asyncio
    ids = asyncio.run(run())
    assert len(set(ids)) == 3, f"attempts collapsed onto one key: {ids}"
    assert ids[2] == ids[3]


async def test_a_failed_job_can_be_retried_without_reusing_a_broker_key(client):
    """An explicit retry gets a fresh budget, not a rewritten history: the
    attempt counter has to keep rising or the requeue is indistinguishable
    from the delivery that already failed."""
    pid = await _project(client)
    async with get_sessionmaker()() as s:
        job, _ = await jobs.enqueue(s, project_id=uuid.UUID(pid),
                                    kind="story.analyze", input_hash="retry-me")
        await s.commit()
        await jobs.claim(s, job.id)                    # attempt -> 1
        job = await s.get(Job, job.id)
        await jobs.fail(s, job, "quota:rate_limited", "429")
        assert job.status is JobStatus.FAILED

        spent = job.attempt
        assert jobs.revive(job) is True
        assert job.status is JobStatus.QUEUED
        assert job.attempt == spent, "retry must not rewind the attempt counter"
        assert job.max_attempts == spent + jobs.RETRY_ATTEMPTS
        assert job.error_code is None


async def test_a_running_job_is_not_restarted_by_a_second_click(client):
    pid = await _project(client)
    async with get_sessionmaker()() as s:
        job, _ = await jobs.enqueue(s, project_id=uuid.UUID(pid),
                                    kind="story.analyze", input_hash="busy")
        await s.commit()
        await jobs.claim(s, job.id)
        job = await s.get(Job, job.id)
        assert jobs.revive(job) is False
        assert job.status is JobStatus.RUNNING
