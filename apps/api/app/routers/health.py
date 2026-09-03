"""Liveness and readiness.

/healthz answers "is the process up" and must never touch a dependency;
/readyz answers "can it serve traffic" and therefore must.
"""
from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from ..auth import DbSession
from ..config import get_settings
from ..render.ffmpeg import capabilities

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "env": get_settings().env}


@router.get("/readyz")
async def readyz(session: DbSession, response: Response) -> dict:
    checks: dict[str, object] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"

    caps = capabilities()
    checks["ffmpeg"] = "ok" if caps.ffmpeg else "missing"
    checks["ffprobe"] = "ok" if caps.ffprobe else "missing"

    ready = checks["database"] == "ok" and caps.ffmpeg
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "checks": checks}
