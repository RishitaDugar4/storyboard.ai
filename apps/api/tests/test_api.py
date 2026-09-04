"""M0 integration tests: the real FastAPI app against the real database.

No mocks -- this milestone exists to prove wiring, and a mocked session would
prove nothing about migrations, enums or cascades.
"""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("AI_TEXT_PROVIDER", "fake")

TEST_EMAIL, TEST_PASS = "pytest-user@local", "pytest-pass"

from app.auth import create_user               # noqa: E402
from app.config import get_settings            # noqa: E402
from app.db.models import User                 # noqa: E402
from app.db.session import (dispose_engine, get_engine,   # noqa: E402
                            get_sessionmaker)
from app.main import create_app                # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def account():
    """Every test gets the same account, created once and reused."""
    async with get_sessionmaker()() as s:
        existing = (await s.execute(
            select(User).where(User.email == TEST_EMAIL))).scalar_one_or_none()
        if existing is None:
            await create_user(s, email=TEST_EMAIL, display_name="Pytest",
                              passphrase=TEST_PASS)
            await s.commit()
    yield


@pytest.fixture(autouse=True)
async def clean_database():
    """Truncate between tests, then drop the engine.

    The engine is cached for the process lifetime (correct in production: one
    pool per worker), but pytest-asyncio gives each test its own event loop, and
    a pooled asyncpg connection bound to a closed loop fails on reuse. Disposing
    per test keeps the production caching intact rather than weakening it for
    the tests' benefit.
    """
    yield
    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM jobs"))
        await s.execute(text("DELETE FROM projects"))
        await s.commit()
    await dispose_engine()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def _login(client) -> None:
    r = await client.post("/api/v1/auth/session",
                          json={"email": TEST_EMAIL, "passphrase": TEST_PASS})
    assert r.status_code == 204, r.text


# ---- ops ------------------------------------------------------------------
async def test_healthz_does_not_touch_dependencies(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_readyz_reports_database_and_ffmpeg(client):
    r = await client.get("/readyz")
    body = r.json()
    assert body["checks"]["database"] == "ok"
    assert set(body["checks"]) == {"database", "ffmpeg", "ffprobe"}


# ---- auth -----------------------------------------------------------------
async def test_me_requires_a_session(client):
    r = await client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_wrong_passphrase_is_rejected(client):
    r = await client.post("/api/v1/auth/session",
                          json={"email": TEST_EMAIL, "passphrase": "nope"})
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"


async def test_unknown_account_gives_the_same_error(client):
    """The message must not distinguish 'no such account' from 'wrong
    passphrase', or it enumerates who has an account here."""
    a = await client.post("/api/v1/auth/session",
                          json={"email": "nobody@local", "passphrase": "x"})
    b = await client.post("/api/v1/auth/session",
                          json={"email": TEST_EMAIL, "passphrase": "x"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


async def test_disabled_account_cannot_log_in(client):
    async with get_sessionmaker()() as s:
        u = (await s.execute(
            select(User).where(User.email == TEST_EMAIL))).scalar_one()
        u.is_active = False
        await s.commit()
    try:
        r = await client.post("/api/v1/auth/session",
                              json={"email": TEST_EMAIL, "passphrase": TEST_PASS})
        assert r.status_code == 401
    finally:
        async with get_sessionmaker()() as s:
            u = (await s.execute(
                select(User).where(User.email == TEST_EMAIL))).scalar_one()
            u.is_active = True
            await s.commit()


async def test_login_then_me(client):
    await _login(client)
    r = await client.get("/api/v1/me")
    assert r.status_code == 200
    assert r.json()["email"] == TEST_EMAIL


async def test_logout_clears_the_session(client):
    await _login(client)
    await client.delete("/api/v1/auth/session")
    client.cookies.clear()
    assert (await client.get("/api/v1/me")).status_code == 401


async def test_tampered_cookie_rejected(client):
    await _login(client)
    name = get_settings().session_cookie_name
    client.cookies.set(name, client.cookies[name][:-3] + "xxx")
    assert (await client.get("/api/v1/me")).status_code == 401


# ---- projects -------------------------------------------------------------
async def test_projects_require_auth(client):
    assert (await client.get("/api/v1/projects")).status_code == 401


async def test_project_crud_round_trip(client):
    await _login(client)

    created = await client.post("/api/v1/projects",
                                json={"title": "The Lighthouse Keeper"})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    assert created.json()["stage"] == "draft"
    assert created.json()["image_size"] == "1920x1080"
    assert created.json()["allow_premium"] is False

    listed = await client.get("/api/v1/projects")
    assert listed.json()["total"] == 1

    patched = await client.patch(f"/api/v1/projects/{pid}",
                                 json={"stage": "storyboarded",
                                       "allow_premium": True})
    assert patched.json()["stage"] == "storyboarded"
    assert patched.json()["allow_premium"] is True
    assert patched.json()["title"] == "The Lighthouse Keeper"   # untouched

    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    assert (await client.get(f"/api/v1/projects/{pid}")).status_code == 404


async def test_portrait_aspect_flips_image_size(client):
    await _login(client)
    r = await client.post("/api/v1/projects",
                          json={"title": "vertical", "aspect_ratio": "9:16"})
    assert r.json()["image_size"] == "1080x1920"


async def test_unsupported_aspect_ratio_rejected(client):
    await _login(client)
    r = await client.post("/api/v1/projects",
                          json={"title": "square", "aspect_ratio": "1:1"})
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"


async def test_missing_project_is_404_not_500(client):
    await _login(client)
    r = await client.get("/api/v1/projects/00000000-0000-7000-8000-000000000000")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


async def test_share_link_is_stable_once_created(client):
    await _login(client)
    pid = (await client.post("/api/v1/projects",
                             json={"title": "share me"})).json()["id"]
    first = (await client.post(f"/api/v1/projects/{pid}/share")).json()["share_token"]
    second = (await client.post(f"/api/v1/projects/{pid}/share")).json()["share_token"]
    assert first and first == second


async def test_budget_must_be_non_negative(client):
    await _login(client)
    r = await client.post("/api/v1/projects",
                          json={"title": "x", "budget_cents": -1})
    assert r.status_code == 422
