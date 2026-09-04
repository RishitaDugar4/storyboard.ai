"""Domain 3: character management.

Locking is the mechanism that makes a character look like themselves across a
film. On lock the structured appearance is rendered into one canon string and
frozen; every downstream prompt embeds that exact string. Unlocking is the most
expensive edit in the app, so it reports what it would invalidate before doing
anything.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Asset, Character, Shot
from ..jobs import service as jobs
from .materialize import render_appearance_prompt


class CharacterLocked(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name} is locked. Unlock first -- but note that changing a locked "
            "character invalidates every still they appear in.")


@dataclass
class UnlockImpact:
    shots: int
    stills: int
    estimated_recost_cents: float

    @property
    def is_expensive(self) -> bool:
        return self.shots > 0


async def _shots_featuring(session: AsyncSession, character: Character):
    """Shots whose subject_slugs contain this character.

    Done in SQL with a JSONB containment test rather than by loading every
    shot: this runs on every character read in the UI.
    """
    return (await session.execute(
        select(Shot).where(
            Shot.project_id == character.project_id,
            Shot.subject_slugs.contains([character.slug])))).scalars().all()


async def unlock_impact(session: AsyncSession, character: Character,
                        *, cost_per_still_cents: float = 2.5) -> UnlockImpact:
    shots = await _shots_featuring(session, character)
    with_stills = [s for s in shots if s.selected_image_id is not None]
    return UnlockImpact(
        shots=len(shots), stills=len(with_stills),
        estimated_recost_cents=len(with_stills) * cost_per_still_cents)


async def lock_character(session: AsyncSession, character: Character) -> Character:
    """Freeze the canon.

    Rendered once, here, and never again: if each prompt re-rendered it from
    the structured fields, a later edit to one field would silently change
    every future shot while leaving earlier ones alone.
    """
    character.appearance_prompt = render_appearance_prompt(character.appearance)
    character.locked_at = jobs.now()
    await session.flush()
    return character


async def unlock_character(session: AsyncSession,
                           character: Character) -> UnlockImpact:
    impact = await unlock_impact(session, character)
    character.locked_at = None
    await session.flush()
    return impact


async def update_character(session: AsyncSession, character: Character,
                           changes: dict) -> Character:
    if character.locked_at is not None:
        raise CharacterLocked(character.name)
    for field in ("name", "role"):
        if field in changes and changes[field] is not None:
            setattr(character, field, changes[field])
    if changes.get("appearance"):
        character.appearance = {**character.appearance, **changes["appearance"]}
        # Kept in sync while unlocked so the UI can preview the canon; frozen
        # only when the character is locked.
        character.appearance_prompt = render_appearance_prompt(character.appearance)
    if changes.get("voice"):
        character.voice = {**(character.voice or {}), **changes["voice"]}
    await session.flush()
    return character
