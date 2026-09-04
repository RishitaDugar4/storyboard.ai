"""Turn an immutable Storyboard document into editable rows.

This is the one destructive step in the pipeline. Re-applying over rows that
already carry approved stills, recorded narration, and paid clips would destroy
work that money cannot exactly re-create, so it refuses unless the caller is
explicit (see `ApplyRefused`).
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.ids import uuid7
from ..db.models import (Character, Location, NarrationLine, Scene, Shot,
                         StoryboardDoc)
from ..schemas.ai import Storyboard

#: Gaps so a later insert between two neighbours is a single-row update.
ORDER_STEP = 1000


class ApplyRefused(RuntimeError):
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        bits = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        super().__init__(
            f"this project already has {bits}. Re-applying a storyboard "
            "deletes all of it. Pass force=True only if you mean to start over; "
            "to change one scene, regenerate that scene instead.")


@dataclass
class ApplyResult:
    characters: int
    locations: int
    scenes: int
    shots: int
    narration_lines: int


def render_appearance_prompt(c: dict) -> str:
    """Flatten the structured canon into the exact string every image prompt
    will embed. Rendered once and frozen at lock time: paraphrasing between
    shots is the biggest cause of character drift."""
    bits = [
        c["name"], f"a {c['age_impression']} {c['role']}".strip(),
        c["build"], f"{c['hair']} hair", f"{c['eyes']} eyes", f"{c['skin']} skin",
        *(c.get("distinguishing_features") or []),
        f"wearing {c['default_wardrobe']}",
    ]
    return ", ".join(b for b in bits if b)


async def _existing_work(session: AsyncSession,
                         project_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, model, cond in (
        ("approved stills", Shot, Shot.selected_image_id.isnot(None)),
        ("generated clips", Shot, Shot.selected_clip_id.isnot(None)),
        ("recorded narration lines", NarrationLine,
         NarrationLine.audio_asset_id.isnot(None)),
        ("locked characters", Character, Character.locked_at.isnot(None)),
    ):
        counts[label] = (await session.execute(
            select(func.count()).select_from(model)
            .where(model.project_id == project_id, cond))).scalar_one()
    return counts


async def apply_storyboard(session: AsyncSession, doc: StoryboardDoc, *,
                           force: bool = False) -> ApplyResult:
    project_id = doc.project_id
    counts = await _existing_work(session, project_id)
    if any(counts.values()) and not force:
        raise ApplyRefused(counts)

    sb = Storyboard.model_validate(doc.document)

    await session.execute(delete(Scene).where(Scene.project_id == project_id))
    await session.execute(delete(Character).where(Character.project_id == project_id))
    await session.execute(delete(Location).where(Location.project_id == project_id))
    await session.flush()

    rng = random.Random(str(project_id))            # stable across re-applies

    for i, c in enumerate(sb.characters):
        data = c.model_dump()
        session.add(Character(
            id=uuid7(), project_id=project_id, slug=c.slug, name=c.name,
            role=c.role, appearance=data,
            appearance_prompt=render_appearance_prompt(data),
            voice=data.get("voice") or {}, seed=rng.randrange(1, 2**31),
            sort_order=i * ORDER_STEP))

    loc_ids: dict[str, uuid.UUID] = {}
    for l in sb.locations:
        lid = uuid7()
        loc_ids[l.slug] = lid
        session.add(Location(id=lid, project_id=project_id, slug=l.slug,
                             name=l.name, description=l.description,
                             prompt_fragment=l.prompt_fragment))
    await session.flush()

    # Scenes and shots first, flushed, THEN narration: narration_lines carry a
    # foreign key to shots created in this same unit of work, and relying on
    # SQLAlchemy's insert ordering to resolve that is a bet, not a guarantee.
    shot_ids_by_scene: list[tuple[uuid.UUID, dict[int, uuid.UUID]]] = []
    shots = lines = 0

    for si, sc in enumerate(sb.scenes):
        scene_id = uuid7()
        session.add(Scene(
            id=scene_id, project_id=project_id,
            location_id=loc_ids.get(sc.location_slug) if sc.location_slug else None,
            sort_order=si * ORDER_STEP, title=sc.title, summary=sc.summary,
            time_of_day=sc.time_of_day, mood=sc.mood,
            present_slugs=list(sc.present_slugs)))

        shot_ids: dict[int, uuid.UUID] = {}
        for hi, sh in enumerate(sc.shots):
            shot_id = uuid7()
            shot_ids[sh.local_index] = shot_id
            session.add(Shot(
                id=shot_id, scene_id=scene_id, project_id=project_id,
                sort_order=hi * ORDER_STEP, shot_type=sh.shot_type,
                action=sh.action, composition_note=sh.composition_note,
                camera_move=sh.camera_move, subject_motion=sh.subject_motion,
                ambient_sound=sh.ambient_sound,
                motion_priority=sh.motion_priority,
                target_duration_s=sh.target_duration_s,
                subject_slugs=list(sh.subject_slugs),
                seed=rng.randrange(1, 2**31)))
            shots += 1
        shot_ids_by_scene.append((scene_id, shot_ids))

    await session.flush()

    for (scene_id, shot_ids), sc in zip(shot_ids_by_scene, sb.scenes):
        for ni, n in enumerate(sc.narration):
            session.add(NarrationLine(
                id=uuid7(), scene_id=scene_id, project_id=project_id,
                shot_id=shot_ids.get(n.shot_local_index)
                if n.shot_local_index is not None else None,
                sort_order=ni * ORDER_STEP, speaker_slug=n.speaker,
                text=n.text, delivery=n.delivery))
            lines += 1

    await session.flush()
    return ApplyResult(len(sb.characters), len(sb.locations), len(sb.scenes),
                       shots, lines)
