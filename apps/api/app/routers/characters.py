"""Characters: edit, lock, and see what unlocking would cost."""
from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import CurrentUser, DbSession
from ..db.models import Character, Project
from ..errors import DomainError, NotFound
from ..services.character_service import (CharacterLocked, lock_character,
                                          unlock_character, unlock_impact,
                                          update_character)

router = APIRouter(prefix="/api/v1", tags=["characters"])


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, max_length=40)
    appearance: dict | None = None
    voice: dict | None = None


def _read(c: Character, impact=None) -> dict:
    out = {
        "id": str(c.id), "slug": c.slug, "name": c.name, "role": c.role,
        "appearance": c.appearance, "appearance_prompt": c.appearance_prompt,
        "voice": c.voice, "seed": c.seed,
        "locked": c.locked_at is not None,
        "locked_at": c.locked_at.isoformat() if c.locked_at else None,
        "reference_asset_id": str(c.reference_asset_id) if c.reference_asset_id else None,
    }
    if impact is not None:
        out["unlock_impact"] = {
            "shots": impact.shots, "stills": impact.stills,
            "estimated_recost_cents": round(impact.estimated_recost_cents, 1)}
    return out


async def _owned_project(session, user, pid: uuid.UUID) -> Project:
    project = await session.get(Project, pid)
    if project is None or project.owner_id != user.id:
        raise NotFound("project not found")
    return project


async def _owned_character(session, user, cid: uuid.UUID) -> Character:
    c = await session.get(Character, cid)
    if c is None:
        raise NotFound("character not found")
    await _owned_project(session, user, c.project_id)
    return c


@router.get("/projects/{project_id}/characters")
async def list_characters(project_id: uuid.UUID, session: DbSession,
                          user: CurrentUser) -> dict:
    await _owned_project(session, user, project_id)
    rows = (await session.execute(
        select(Character).where(Character.project_id == project_id)
        .order_by(Character.sort_order))).scalars().all()
    items = [_read(c, await unlock_impact(session, c)) for c in rows]
    return {"items": items, "total": len(items)}


@router.patch("/characters/{character_id}")
async def patch_character(character_id: uuid.UUID, body: CharacterUpdate,
                          session: DbSession, user: CurrentUser) -> dict:
    c = await _owned_character(session, user, character_id)
    try:
        c = await update_character(session, c,
                                   body.model_dump(exclude_unset=True))
    except CharacterLocked as exc:
        raise DomainError(str(exc), code="character_locked",
                          status_code=409) from exc
    return _read(c)


@router.post("/characters/{character_id}:lock")
async def lock(character_id: uuid.UUID, session: DbSession,
               user: CurrentUser) -> dict:
    c = await _owned_character(session, user, character_id)
    return _read(await lock_character(session, c))


@router.post("/characters/{character_id}:unlock")
async def unlock(character_id: uuid.UUID, session: DbSession,
                 user: CurrentUser) -> dict:
    c = await _owned_character(session, user, character_id)
    impact = await unlock_character(session, c)
    return {**_read(c), "invalidated": {
        "shots": impact.shots, "stills": impact.stills,
        "estimated_recost_cents": round(impact.estimated_recost_cents, 1)}}
