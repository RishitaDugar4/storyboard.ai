"""M4: prompt composition, freshness, budget, locking, and still generation."""
from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ["AI_TEXT_PROVIDER"] = "fake"
os.environ["AI_IMAGE_PROVIDER"] = "fake"
os.environ["JOB_QUEUE"] = "inline"

from app.ai.prompts.compose import (BASE_NEGATIVE, COMPOSER_VERSION,  # noqa: E402
                                    compose_image_prompt)
from app.auth import create_user                        # noqa: E402
from app.db.models import (Asset, AssetKind, AssetSource, Character,  # noqa: E402
                           Project, Shot, User)
from app.db.session import (dispose_engine, get_engine,   # noqa: E402
                            get_sessionmaker)
from app.jobs import get_queue, reset_queue              # noqa: E402
from app.main import create_app                          # noqa: E402
from app.services.still_service import (BudgetExceeded, PriceUnknown,  # noqa: E402
                                         check_budget, still_is_fresh)

pytestmark = pytest.mark.asyncio
EMAIL, PASS = "stills@local", "stills-pass"
STORY = ("A keeper kept a light for forty years. One night the power failed "
         "and she turned the lens by hand until dawn.")

STYLE = {"art_style": "gouache", "palette": ["slate", "amber"],
         "lighting": "one warm source", "line_and_texture": "dry brush",
         "negative": ["photorealism"]}


# ---- prompt composition ---------------------------------------------------
def test_character_canon_is_embedded_verbatim():
    canon = "Mara Halloran, early seventies, white cropped hair, oilskin coat"
    p = compose_image_prompt(style_bible=STYLE, shot_type="close_up",
                             action="She lowers the lamp",
                             character_prompts=[("Mara", canon)])
    # Paraphrasing here is precisely what makes a character look like someone
    # else in the next shot.
    assert canon in p.positive


def test_fragments_record_where_each_phrase_came_from():
    p = compose_image_prompt(style_bible=STYLE, shot_type="wide",
                             action="The sea", location_fragment="open water",
                             character_prompts=[("Mara", "a keeper")])
    origins = [o for o, _ in p.fragments]
    assert "style" in origins and "action" in origins
    assert "character:Mara" in origins and "location" in origins


def test_base_negatives_are_always_present():
    p = compose_image_prompt(style_bible={}, shot_type="wide", action="x")
    for term in BASE_NEGATIVE:
        assert term in p.negative


def test_override_replaces_composition_but_keeps_negatives():
    p = compose_image_prompt(style_bible=STYLE, shot_type="wide", action="x",
                             prompt_override="a completely different image")
    assert p.positive.startswith("a completely different image")
    assert "gouache" not in p.positive
    assert "watermark" in p.negative


def test_hash_is_stable_and_sensitive():
    a = compose_image_prompt(style_bible=STYLE, shot_type="wide", action="x")
    b = compose_image_prompt(style_bible=STYLE, shot_type="wide", action="y")
    args = dict(seed=1, size="512x512", provider="fal", model="m")
    assert a.hash(**args) == a.hash(**args)
    assert a.hash(**args) != b.hash(**args)
    assert a.hash(**args) != a.hash(**{**args, "seed": 2})
    assert a.hash(**args) != a.hash(**{**args, "model": "other"})


# ---- freshness ------------------------------------------------------------
def _shot(**kw):
    s = Shot(); s.image_input_hash = kw.get("hash"); return s


def _asset(source, input_hash):
    a = Asset(); a.source = source; a.input_hash = input_hash; return a


def test_still_with_no_asset_is_not_fresh():
    assert still_is_fresh(_shot(hash="a"), None) is False


def test_matching_hash_is_fresh():
    assert still_is_fresh(_shot(hash="a"),
                          _asset(AssetSource.GENERATED, "a")) is True


def test_changed_prompt_makes_the_still_stale():
    assert still_is_fresh(_shot(hash="b"),
                          _asset(AssetSource.GENERATED, "a")) is False


def test_uploaded_stills_are_permanently_fresh():
    """A file the user chose was not produced by a prompt, so no prompt change
    can invalidate it -- and it must never be silently regenerated."""
    assert still_is_fresh(_shot(hash="anything"),
                          _asset(AssetSource.MANUAL, None)) is True


# ---- budget ---------------------------------------------------------------
def test_budget_is_checked_before_spending():
    p = Project(); p.spent_cents, p.budget_cents = 90, 100
    check_budget(p, 10)                       # exactly at the limit is allowed
    with pytest.raises(BudgetExceeded) as exc:
        check_budget(p, 11)
    assert "exceeding" in str(exc.value)


def test_an_unpriced_model_is_refused_not_treated_as_free():
    """Defaulting an unknown model to zero would quietly disable the budget --
    the one control between a fan-out and a surprise invoice."""
    p = Project(); p.spent_cents, p.budget_cents = 0, 10_000
    with pytest.raises(PriceUnknown):
        check_budget(p, 0, price_known=False, model="mystery")


# ---- end to end -----------------------------------------------------------
@pytest.fixture
async def client(tmp_path):
    os.environ["STORAGE_DIR"] = str(tmp_path)
    from app.config import get_settings
    from app.storage import reset_storage
    get_settings.cache_clear(); reset_storage(); reset_queue()
    async with get_sessionmaker()() as s:
        if not (await s.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none():
            await create_user(s, email=EMAIL, display_name="S", passphrase=PASS)
            await s.commit()
    async with AsyncClient(transport=ASGITransport(app=create_app()),
                           base_url="http://test") as c:
        await c.post("/api/v1/auth/session",
                     json={"email": EMAIL, "passphrase": PASS})
        yield c


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with get_sessionmaker()() as s:
        for t in ("job_events", "jobs", "ai_calls", "assets", "projects"):
            await s.execute(text(f"DELETE FROM {t}"))
        await s.commit()
    await dispose_engine(); get_engine.cache_clear(); get_sessionmaker.cache_clear()
    reset_queue()


async def _project_with_shots(client) -> tuple[str, list[dict]]:
    pid = (await client.post("/api/v1/projects", json={"title": "stills"})).json()["id"]
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    a = (await client.post(f"/api/v1/projects/{pid}/story:analyze")).json()
    await get_queue().drain()
    g = (await client.post(f"/api/v1/projects/{pid}/storyboard:generate", json={})).json()
    await get_queue().drain()
    sb = (await client.get(f"/api/v1/projects/{pid}/storyboards")).json()["items"][0]["id"]
    await client.post(f"/api/v1/projects/{pid}/storyboards/{sb}:apply", json={})
    shots = (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]
    return pid, shots


async def test_prompt_endpoint_exposes_every_fragment(client):
    pid, shots = await _project_with_shots(client)
    body = (await client.get(f"/api/v1/shots/{shots[0]['id']}/prompt")).json()
    assert body["positive"] and body["fragments"]
    assert {f["origin"] for f in body["fragments"]} & {"style", "action"}
    assert body["estimated_cost_cents"] >= 0


async def test_generate_select_and_freshness(client):
    pid, shots = await _project_with_shots(client)
    sid = shots[0]["id"]

    accepted = await client.post(f"/api/v1/shots/{sid}/image:generate",
                                 json={"n": 2})
    assert accepted.status_code == 202
    await get_queue().drain()

    job = (await client.get(f"/api/v1/jobs/{accepted.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    assert len(job["result"]["asset_ids"]) == 2

    listed = (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]
    me = next(s for s in listed if s["id"] == sid)
    assert me["still"] is not None
    assert me["still_fresh"] is True          # generated from the current prompt

    images = (await client.get(f"/api/v1/shots/{sid}/images")).json()["items"]
    assert len(images) == 2
    other = next(i for i in images if not i["selected"])
    sel = await client.post(f"/api/v1/shots/{sid}/image:select",
                            json={"asset_id": other["id"]})
    assert sel.status_code == 200 and sel.json()["asset"]["selected"] is True


async def test_identical_prompt_reuses_the_paid_asset(client):
    """Regenerating an unchanged shot must not pay twice for the same bytes."""
    pid, shots = await _project_with_shots(client)
    sid = shots[0]["id"]
    first = await client.post(f"/api/v1/shots/{sid}/image:generate", json={"n": 1})
    await get_queue().drain()
    spent_after_first = (await client.get(f"/api/v1/projects/{pid}")).json()["spent_cents"]

    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM jobs"))
        await s.commit()

    again = await client.post(f"/api/v1/shots/{sid}/image:generate", json={"n": 1})
    await get_queue().drain()
    job = (await client.get(f"/api/v1/jobs/{again.json()['job_id']}")).json()
    assert job["result"]["cached"] is True
    assert (await client.get(f"/api/v1/projects/{pid}")).json()["spent_cents"] == spent_after_first


async def test_generation_is_refused_over_budget(client, monkeypatch):
    pid, shots = await _project_with_shots(client)
    # The fake provider is genuinely free, so price one in to exercise the gate.
    from app.ai import registry
    from app.ai.adapters.fakes import FakeImageAdapter
    registry.get_image_port.cache_clear()
    monkeypatch.setattr(registry, "get_image_port",
                        lambda: FakeImageAdapter(cost_per_image_cents=50.0))
    import app.routers.stills as stills_router
    monkeypatch.setattr(stills_router, "get_image_port", registry.get_image_port)

    await client.patch(f"/api/v1/projects/{pid}", json={"budget_cents": 10})
    r = await client.post(f"/api/v1/shots/{shots[0]['id']}/image:generate",
                          json={"n": 2})
    # 402 before the job exists: a doomed request should never become a queued
    # job that fails later for a reason we already knew.
    assert r.status_code == 402 and r.json()["code"] == "budget_exceeded"


async def test_upload_overrides_and_is_permanently_fresh(client):
    pid, shots = await _project_with_shots(client)
    sid = shots[0]["id"]
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    r = await client.post(f"/api/v1/shots/{sid}/image:upload",
                          files={"file": ("mine.png", png, "image/png")})
    assert r.status_code == 200 and r.json()["asset"]["source"] == "manual"
    me = next(s for s in (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]
              if s["id"] == sid)
    assert me["still_fresh"] is True


async def test_upload_rejects_non_images(client):
    _, shots = await _project_with_shots(client)
    r = await client.post(f"/api/v1/shots/{shots[0]['id']}/image:upload",
                          files={"file": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 400


# ---- character locking ----------------------------------------------------
async def test_lock_freezes_the_canon_and_blocks_edits(client):
    pid, _ = await _project_with_shots(client)
    chars = (await client.get(f"/api/v1/projects/{pid}/characters")).json()["items"]
    cid = chars[0]["id"]

    locked = (await client.post(f"/api/v1/characters/{cid}:lock")).json()
    assert locked["locked"] is True and locked["appearance_prompt"]

    blocked = await client.patch(f"/api/v1/characters/{cid}",
                                 json={"name": "Someone Else"})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "character_locked"


async def test_unlock_reports_what_it_would_invalidate(client):
    pid, shots = await _project_with_shots(client)
    chars = (await client.get(f"/api/v1/projects/{pid}/characters")).json()["items"]
    cid = chars[0]["id"]
    await client.post(f"/api/v1/characters/{cid}:lock")

    body = (await client.post(f"/api/v1/characters/{cid}:unlock")).json()
    assert body["locked"] is False
    # The most expensive edit in the app must say so before it is made.
    assert "invalidated" in body
    assert set(body["invalidated"]) == {"shots", "stills", "estimated_recost_cents"}


async def test_shots_are_ordered_by_scene_then_shot(client):
    """shot.sort_order is per-scene, so ordering by it alone shuffles the film."""
    pid, _ = await _project_with_shots(client)
    items = (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]
    indices = [s["scene_index"] for s in items]
    assert indices == sorted(indices), indices
    # And stable across calls, which is what the UI and the renderer rely on.
    again = (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]
    assert [s["id"] for s in again] == [s["id"] for s in items]
