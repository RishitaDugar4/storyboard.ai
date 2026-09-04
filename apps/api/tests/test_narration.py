"""M5: narration, the fit check, and the first watchable film.

The end-to-end test here is the milestone's whole point: story in, MP4 out,
through the real job system, with zero spend.
"""
from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ["AI_TEXT_PROVIDER"] = "fake"
os.environ["AI_IMAGE_PROVIDER"] = "fake"
os.environ["AI_SPEECH_PROVIDER"] = "fake"
os.environ["JOB_QUEUE"] = "inline"

from app.ai.audio import pcm_duration_ms, pcm_to_wav, silence_wav  # noqa: E402
from app.auth import create_user                        # noqa: E402
from app.db.models import NarrationLine, Shot, User      # noqa: E402
from app.db.session import (dispose_engine, get_engine,   # noqa: E402
                            get_sessionmaker)
from app.jobs import get_queue, reset_queue              # noqa: E402
from app.main import create_app                          # noqa: E402
from app.render.ffmpeg import capabilities, probe        # noqa: E402
from app.services.narration_service import (FitStatus, MAX_TAIL_FREEZE_MS,  # noqa: E402
                                             evaluate_fit)

pytestmark = pytest.mark.asyncio
EMAIL, PASS = "narr@local", "narr-pass"
STORY = ("A keeper kept a light for forty years. One winter night the power "
         "failed and she turned the lens by hand until dawn.")
needs_ffmpeg = pytest.mark.skipif(not capabilities().ffmpeg, reason="ffmpeg absent")


# ---- pcm helpers ----------------------------------------------------------
def test_duration_is_arithmetic_not_a_guess():
    # 24kHz, 16-bit mono: one second is 48000 bytes, exactly.
    assert pcm_duration_ms(48_000) == 1000
    assert pcm_duration_ms(159_886) == 3331          # the real sample we measured


def test_wav_wrapper_is_well_formed():
    w = pcm_to_wav(b"\x00\x00" * 24_000)
    assert w[:4] == b"RIFF" and w[8:12] == b"WAVE"
    assert len(w) == 44 + 48_000


def test_silence_has_the_requested_length():
    assert pcm_duration_ms(len(silence_wav(500)) - 44) == 500


# ---- fit ------------------------------------------------------------------
def _fit(duration_s: float, lines: list[tuple[str, int | None]]):
    shot = Shot(); shot.target_duration_s = duration_s
    out = []
    for t, ms in lines:
        l = NarrationLine(); l.text = t; l.duration_ms = ms; out.append(l)
    return evaluate_fit(shot, out)


def test_fit_uses_measured_audio_when_it_exists():
    f = _fit(6.0, [("one two three four", 3000)])
    assert f.status is FitStatus.FITS and f.narration_ms == 3000


def test_fit_is_unknown_before_anything_is_recorded():
    assert _fit(6.0, [("one two three", None)]).status is FitStatus.UNKNOWN


def test_overflow_reports_the_held_frame_it_would_need():
    f = _fit(4.0, [("a b c d e f g h i j", 6000)])
    assert f.status is FitStatus.OVERFLOW
    assert f.tail_freeze_ms == 6000 + 900 - 4000
    assert f.blocks_render is (f.tail_freeze_ms > MAX_TAIL_FREEZE_MS)


def test_a_small_overrun_is_tolerated_not_blocked():
    f = _fit(5.0, [("a b c d e f g h", 4500)])
    assert f.tail_freeze_ms <= MAX_TAIL_FREEZE_MS and not f.blocks_render


# ---- end to end -----------------------------------------------------------
@pytest.fixture
async def client(tmp_path):
    os.environ["STORAGE_DIR"] = str(tmp_path)
    from app.config import get_settings
    from app.storage import reset_storage
    get_settings.cache_clear(); reset_storage(); reset_queue()
    async with get_sessionmaker()() as s:
        if not (await s.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none():
            await create_user(s, email=EMAIL, display_name="N", passphrase=PASS)
            await s.commit()
    async with AsyncClient(transport=ASGITransport(app=create_app()),
                           base_url="http://test", timeout=180) as c:
        await c.post("/api/v1/auth/session",
                     json={"email": EMAIL, "passphrase": PASS})
        yield c


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with get_sessionmaker()() as s:
        for t in ("job_events", "jobs", "ai_calls", "renders", "assets",
                  "projects"):
            await s.execute(text(f"DELETE FROM {t}"))
        await s.commit()
    await dispose_engine(); get_engine.cache_clear(); get_sessionmaker.cache_clear()
    reset_queue()


async def _ready_project(client) -> str:
    """A project with a storyboard applied and every shot given a still."""
    pid = (await client.post("/api/v1/projects", json={"title": "Film"})).json()["id"]
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    await client.post(f"/api/v1/projects/{pid}/story:analyze")
    await get_queue().drain()
    await client.post(f"/api/v1/projects/{pid}/storyboard:generate", json={})
    await get_queue().drain()
    sb = (await client.get(f"/api/v1/projects/{pid}/storyboards")).json()["items"][0]["id"]
    await client.post(f"/api/v1/projects/{pid}/storyboards/{sb}:apply", json={})
    for shot in (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]:
        await client.post(f"/api/v1/shots/{shot['id']}/image:generate", json={"n": 1})
    await get_queue().drain()
    return pid


async def test_voices_are_listed(client):
    body = (await client.get("/api/v1/voices")).json()
    assert body["items"] and isinstance(body["items"][0], str)


async def test_narration_generation_records_measured_duration(client):
    pid = await _ready_project(client)
    body = (await client.post(f"/api/v1/projects/{pid}/narration:generate_all")).json()
    assert body["queued"] >= 1
    await get_queue().drain()

    items = (await client.get(f"/api/v1/projects/{pid}/narration")).json()["items"]
    lines = [l for it in items for l in it["lines"]]
    assert lines, "no narration lines"
    assert all(l["duration_ms"] for l in lines), "a line has no measured duration"
    assert all(l["fresh"] for l in lines)
    assert all(l["audio_url"] for l in lines)
    # Fit stops being a guess once the audio exists.
    assert all(it["fit"]["status"] != "unknown" for it in items)


async def test_preflight_blocks_a_render_with_no_stills(client):
    pid = (await client.post("/api/v1/projects", json={"title": "Empty"})).json()["id"]
    r = await client.post(f"/api/v1/projects/{pid}/preflight",
                          json={"profile": "preview"})
    assert r.json()["ok"] is False
    assert r.json()["blocking"]


async def test_render_is_refused_before_a_job_exists(client):
    pid = (await client.post("/api/v1/projects", json={"title": "Empty"})).json()["id"]
    r = await client.post(f"/api/v1/projects/{pid}/renders",
                          json={"profile": "preview"})
    # Better a clear refusal now than a six-minute render ending in black.
    assert r.status_code == 409 and r.json()["code"] == "preflight_failed"


@needs_ffmpeg
async def test_the_whole_pipeline_produces_a_playable_film(client):
    pid = await _ready_project(client)
    await client.post(f"/api/v1/projects/{pid}/narration:generate_all")
    await get_queue().drain()

    pre = (await client.post(f"/api/v1/projects/{pid}/preflight",
                             json={"profile": "preview"})).json()
    assert pre["ok"] is True, pre["blocking"]

    accepted = await client.post(f"/api/v1/projects/{pid}/renders",
                                 json={"profile": "preview"})
    assert accepted.status_code == 202
    await get_queue().drain()

    job = (await client.get(f"/api/v1/jobs/{accepted.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job

    renders = (await client.get(f"/api/v1/projects/{pid}/renders")).json()["items"]
    assert renders and renders[0]["status"] == "succeeded"
    assert renders[0]["video_url"] and renders[0]["duration_ms"] > 0

    # The file must be a real, probeable video -- not merely a row that says so.
    from app.db.models import Asset, Render
    from app.storage import get_storage
    async with get_sessionmaker()() as s:
        row = (await s.execute(select(Render).where(
            Render.project_id == uuid.UUID(pid)))).scalars().first()
        asset = await s.get(Asset, row.video_asset_id)
    path = get_storage().local_path(asset.storage_key)
    info = probe(path)
    assert info.ok and info.has_video and info.has_audio
    assert abs(info.duration_ms - row.duration_ms) < 200
    assert (await client.get(f"/api/v1/projects/{pid}")).json()["stage"] == "previewed"
