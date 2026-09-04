"""Characters: edit, lock, and see what unlocking would cost."""
from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import CurrentUser, DbSession
from sqlalchemy import func

from ..db.models import Character, NarrationLine, Project
from ..errors import DomainError, NotFound
from ..services.character_service import (CharacterLocked, lock_character,
                                          unlock_character, unlock_impact,
                                          update_character)
from ..services.still_service import recompute_hashes_for_character

router = APIRouter(prefix="/api/v1", tags=["characters"])


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, max_length=40)
    appearance: dict | None = None
    #: {"voice_name": "Puck"} — narration lines whose speaker is this
    #: character are then spoken in that voice instead of the narrator's.
    voice: dict | None = None


def _read(c: Character, impact=None, spoken_lines: int = 0) -> dict:
    out = {
        "id": str(c.id), "slug": c.slug, "name": c.name, "role": c.role,
        "appearance": c.appearance, "appearance_prompt": c.appearance_prompt,
        "voice": c.voice, "seed": c.seed,
        "locked": c.locked_at is not None,
        "locked_at": c.locked_at.isoformat() if c.locked_at else None,
        "reference_asset_id": str(c.reference_asset_id) if c.reference_asset_id else None,
        "voice_name": (c.voice or {}).get("voice_name"),
        "spoken_lines": spoken_lines,
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
    # How many narration lines this character actually speaks, so the voice
    # picker can say whether choosing one changes anything.
    counts = dict((await session.execute(
        select(NarrationLine.speaker_slug, func.count())
        .where(NarrationLine.project_id == project_id)
        .group_by(NarrationLine.speaker_slug))).all())
    items = [_read(c, await unlock_impact(session, c), counts.get(c.slug, 0))
             for c in rows]
    return {"items": items, "total": len(items)}


@router.patch("/characters/{character_id}")
async def patch_character(character_id: uuid.UUID, body: CharacterUpdate,
                          session: DbSession, user: CurrentUser) -> dict:
    c = await _owned_character(session, user, character_id)
    try:
        c = await update_character(session, c,
                                   body.model_dump(exclude_unset=True))
        if body.voice:
            # Changing a voice makes that character's recorded lines stale, so
            # they are re-spoken rather than left in the old voice.
            await _invalidate_spoken_lines(session, c)
        if body.appearance or body.name:
            # The canon is embedded verbatim in every prompt featuring them,
            # so editing it makes exactly those stills stale.
            project = await session.get(Project, c.project_id)
            await recompute_hashes_for_character(session, project, c.slug)
    except CharacterLocked as exc:
        raise DomainError(str(exc), code="character_locked",
                          status_code=409) from exc
    return _read(c, await unlock_impact(session, c))


async def _invalidate_spoken_lines(session, c: Character) -> int:
    lines = (await session.execute(
        select(NarrationLine).where(
            NarrationLine.project_id == c.project_id,
            NarrationLine.speaker_slug == c.slug))).scalars().all()
    for line in lines:
        line.input_hash = None          # nothing can match, so audio is stale
    return len(lines)


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
