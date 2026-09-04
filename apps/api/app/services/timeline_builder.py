"""Build a render Timeline from the working rows.

This is the seam between everything upstream and the renderer. It is also
where the duration algorithm lives (ARCHITECTURE 10.3): screen time is derived
from *measured* narration, and the visual is padded to fit rather than the
audio being compressed to fit the visual.

The renderer itself takes the Timeline and file paths and nothing else, so the
correctness of the film rests almost entirely on this function.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.pacing import PAD_S
from ..db.models import (Asset, AssetKind, Character, NarrationLine, Project,
                         Scene, Shot)
from ..render.timeline import (AudioCue, AudioMix, Card, CameraMove, Clip,
                               KenBurns, Profile, Source, SourceKind, Timeline)
from ..storage import get_storage

#: The image lands a beat before the voice starts, and holds a beat after.
LEAD_IN_MS = 300
TAIL_MS = int(PAD_S * 1000) - LEAD_IN_MS


@dataclass
class BuildProblem:
    code: str
    message: str
    shot_id: str | None = None


@dataclass
class BuildResult:
    timeline: Timeline | None
    blocking: list[BuildProblem] = field(default_factory=list)
    advisory: list[BuildProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.timeline is not None and not self.blocking


async def build_timeline(session: AsyncSession, project: Project, *,
                         profile: Profile = Profile.PREVIEW,
                         subtitles: bool = True) -> BuildResult:
    storage = get_storage()
    blocking: list[BuildProblem] = []
    advisory: list[BuildProblem] = []

    rows = (await session.execute(
        select(Shot, Scene).join(Scene, Scene.id == Shot.scene_id)
        .where(Shot.project_id == project.id)
        .order_by(Scene.sort_order, Shot.sort_order))).all()
    if not rows:
        return BuildResult(None, [BuildProblem(
            "no_shots", "apply a storyboard before rendering")])

    lines_by_shot: dict[uuid.UUID, list[NarrationLine]] = {}
    scene_lines: dict[uuid.UUID, list[NarrationLine]] = {}
    for line in (await session.execute(
            select(NarrationLine).where(NarrationLine.project_id == project.id)
            .order_by(NarrationLine.sort_order))).scalars():
        if line.shot_id:
            lines_by_shot.setdefault(line.shot_id, []).append(line)
        else:
            scene_lines.setdefault(line.scene_id, []).append(line)

    asset_ids = {s.selected_image_id for s, _ in rows if s.selected_image_id}
    asset_ids |= {l.audio_asset_id
                  for ls in (*lines_by_shot.values(), *scene_lines.values())
                  for l in ls if l.audio_asset_id}
    assets: dict[uuid.UUID, Asset] = {}
    if asset_ids:
        assets = {a.id: a for a in (await session.execute(
            select(Asset).where(Asset.id.in_(asset_ids)))).scalars()}

    width, height = ((1280, 720) if profile is Profile.PREVIEW
                     else tuple(int(x) for x in project.image_size.split("x")))

    title_card = Card(text=project.title or "Untitled", duration_ms=2500)
    cursor = title_card.duration_ms
    clips: list[Clip] = []

    for shot, scene in rows:
        # Scene-level narration with no shot is charged to the scene's first
        # shot, so no line is silently dropped from the film.
        lines = list(lines_by_shot.get(shot.id, []))
        if shot.sort_order == 0:
            lines = scene_lines.get(scene.id, []) + lines

        still = assets.get(shot.selected_image_id) if shot.selected_image_id else None
        if still is None:
            blocking.append(BuildProblem(
                "no_still", f"shot in '{scene.title}' has no approved still",
                str(shot.id)))
            continue
        path = storage.local_path(still.storage_key)
        if path is None:
            blocking.append(BuildProblem(
                "still_missing", f"the file for '{scene.title}' is gone from storage",
                str(shot.id)))
            continue

        cues: list[AudioCue] = []
        offset = LEAD_IN_MS
        narration_ms = 0
        for line in lines:
            audio = assets.get(line.audio_asset_id) if line.audio_asset_id else None
            if audio is None or not line.duration_ms:
                advisory.append(BuildProblem(
                    "no_audio", f"a line in '{scene.title}' has no narration yet",
                    str(shot.id)))
                continue
            apath = storage.local_path(audio.storage_key)
            if apath is None:
                blocking.append(BuildProblem(
                    "audio_missing", f"narration file for '{scene.title}' is gone",
                    str(shot.id)))
                continue
            cues.append(AudioCue(line_id=str(line.id), path=apath,
                                 offset_ms=offset, duration_ms=line.duration_ms,
                                 text=line.text, speaker=line.speaker_slug))
            offset += line.duration_ms
            narration_ms += line.duration_ms

        # ARCHITECTURE 10.3: a still stretches for free, so intent is honoured
        # exactly unless the narration needs more room.
        required_ms = (narration_ms + LEAD_IN_MS + TAIL_MS) if narration_ms else 0
        duration_ms = max(int(float(shot.target_duration_s) * 1000), required_ms)

        clips.append(Clip(
            shot_id=str(shot.id),
            scene_index=scene.sort_order // 1000,
            shot_index=shot.sort_order // 1000,
            source=Source(kind=SourceKind.STILL, path=path),
            kenburns=KenBurns(move=CameraMove(str(shot.camera_move))),
            start_ms=cursor, duration_ms=duration_ms, tail_freeze_ms=0,
            audio=cues, label=scene.title))
        cursor += duration_ms

    if blocking:
        return BuildResult(None, blocking, advisory)

    music = None
    if project.music_track_key:
        music = storage.local_path(project.music_track_key)

    timeline = Timeline(
        profile=profile, title=project.title, width=width, height=height,
        fps=24, audio=AudioMix(music_path=music, music_db=-24.0),
        title_card=title_card,
        end_card=Card(text="Happy Birthday", duration_ms=3000),
        clips=clips, subtitles=subtitles)
    return BuildResult(timeline, blocking, advisory)
