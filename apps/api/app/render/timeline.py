"""The Timeline: the render contract.

A Timeline is fully self-contained -- it names local file paths and durations
and nothing else. The renderer never touches the database, the network, or the
AI layer, which is what makes it testable from fixtures, runnable from the CLI,
and unchanged when the source of a shot switches between a still and a
generated clip.

See docs/ARCHITECTURE.md section 10.2.
"""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

TIMELINE_SCHEMA_VERSION = "3.0"

#: Bump when any filter-graph construction changes. Included in every clip
#: cache key, so a renderer change correctly invalidates cached intermediates.
RENDERER_VERSION = 1


class SourceKind(StrEnum):
    STILL = "still"      # image + Ken Burns move  (free; the preview default)
    CLIP = "clip"        # generated or uploaded video


class CameraMove(StrEnum):
    STATIC = "static"
    PUSH_IN = "push_in"
    PULL_OUT = "pull_out"
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    TILT_UP = "tilt_up"
    TILT_DOWN = "tilt_down"
    ORBIT = "orbit"          # approximated as a slow push for Ken Burns
    HANDHELD = "handheld"    # approximated as a very slow push


class Profile(StrEnum):
    PREVIEW = "preview"
    FINAL = "final"


class KenBurns(BaseModel):
    """Motion applied to a still source. Ignored when kind == clip."""

    move: CameraMove = CameraMove.PUSH_IN
    start_scale: float = Field(default=1.0, ge=1.0, le=2.0)
    end_scale: float = Field(default=1.12, ge=1.0, le=2.0)


class Source(BaseModel):
    kind: SourceKind
    path: Path
    #: ffprobe truth for clips. Never the requested duration -- a provider that
    #: promises 6s can return 5.875s, and that drift compounds across shots.
    native_duration_ms: int | None = None
    has_audio: bool = False
    #: Provenance, so a render is self-describing months later.
    provider: str | None = None
    model_key: str | None = None

    @model_validator(mode="after")
    def _clip_needs_measured_duration(self):
        if self.kind is SourceKind.CLIP and not self.native_duration_ms:
            raise ValueError(
                f"{self.path.name}: a clip source must carry native_duration_ms "
                "measured with ffprobe")
        return self


class AudioCue(BaseModel):
    """One narration line placed at an ABSOLUTE offset within its clip.

    Absolute placement (rather than sequential concatenation) means one wrong
    duration cannot cascade into every later line.
    """

    line_id: str
    path: Path
    offset_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    text: str = ""
    speaker: str = "narrator"


class Clip(BaseModel):
    shot_id: str
    scene_index: int = 0
    shot_index: int = 0
    source: Source
    kenburns: KenBurns | None = None
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    #: Hold the last frame when narration outruns the visual.
    tail_freeze_ms: int = Field(default=0, ge=0)
    audio: list[AudioCue] = Field(default_factory=list)
    label: str = ""              # debug only; never rendered

    @model_validator(mode="after")
    def _coherent(self):
        if self.source.kind is SourceKind.STILL and self.kenburns is None:
            self.kenburns = KenBurns()
        if self.source.kind is SourceKind.CLIP and self.kenburns is not None:
            raise ValueError(f"{self.shot_id}: kenburns is only valid on a still")
        for cue in self.audio:
            if cue.offset_ms + cue.duration_ms > self.duration_ms + 1:
                raise ValueError(
                    f"{self.shot_id}: narration '{cue.line_id}' ends at "
                    f"{cue.offset_ms + cue.duration_ms}ms but the clip is only "
                    f"{self.duration_ms}ms. The duration algorithm should have "
                    "padded the visual (see ARCHITECTURE 10.3).")
        return self

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms

    def cache_key(self, profile: Profile, width: int, height: int, fps: int) -> str:
        """Identity of the normalised intermediate for this clip.

        Includes the source file's content hash, so re-rendering after changing
        two shots rebuilds only those two.
        """
        h = hashlib.sha256()
        try:
            with self.source.path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            h.update(str(self.source.path).encode())
        payload = json.dumps({
            "src": h.hexdigest(),
            "kind": str(self.source.kind),
            "kb": self.kenburns.model_dump(mode="json") if self.kenburns else None,
            "dur": self.duration_ms,
            "freeze": self.tail_freeze_ms,
            "profile": str(profile),
            "geom": [width, height, fps],
            "v": RENDERER_VERSION,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class Card(BaseModel):
    """A title or end card. Rendered to a PNG and then treated as a still,
    so it needs no special stage in the pipeline."""

    text: str
    subtitle: str = ""
    duration_ms: int = Field(default=2500, gt=0)


class AudioMix(BaseModel):
    sample_rate: int = 48000
    music_path: Path | None = None
    music_db: float = -22.0
    music_fade_in_s: float = 2.0
    music_fade_out_s: float = 3.0
    #: Generated-clip audio is discarded by default: several providers force it
    #: on and will invent dialogue that collides with the narrator.
    keep_source_audio: bool = False
    source_audio_db: float = -20.0
    loudness_target_lufs: float = -16.0


class Timeline(BaseModel):
    schema_version: Literal["3.0"] = TIMELINE_SCHEMA_VERSION
    profile: Profile = Profile.FINAL
    title: str = ""
    width: int = 1920
    height: int = 1080
    fps: int = 24
    audio: AudioMix = Field(default_factory=AudioMix)
    title_card: Card | None = None
    end_card: Card | None = None
    clips: list[Clip] = Field(min_length=1)
    subtitles: bool = True

    @model_validator(mode="after")
    def _contiguous(self):
        """Clips must tile the timeline without gaps or overlaps.

        The builder computes start_ms cumulatively; if these ever disagree the
        audio would drift against the picture, so it is checked rather than
        assumed.
        """
        cursor = self.title_card.duration_ms if self.title_card else 0
        for c in sorted(self.clips, key=lambda x: x.start_ms):
            if c.start_ms != cursor:
                raise ValueError(
                    f"clip {c.shot_id} starts at {c.start_ms}ms but the previous "
                    f"clip ends at {cursor}ms -- timeline is not contiguous")
            cursor = c.end_ms
        return self

    @property
    def total_duration_ms(self) -> int:
        return (
            (self.title_card.duration_ms if self.title_card else 0)
            + sum(c.duration_ms for c in self.clips)
            + (self.end_card.duration_ms if self.end_card else 0)
        )

    @property
    def ordered_clips(self) -> list[Clip]:
        return sorted(self.clips, key=lambda c: c.start_ms)

    def hash(self) -> str:
        return hashlib.sha256(
            self.model_dump_json(exclude={"title"}).encode()).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> "Timeline":
        data = json.loads(Path(path).read_text())
        tl = cls.model_validate(data)
        # Paths in a timeline file are resolved relative to that file, so a
        # fixture directory can be moved or checked out anywhere.
        base = Path(path).parent
        for c in tl.clips:
            c.source.path = (base / c.source.path).resolve()
            for cue in c.audio:
                cue.path = (base / cue.path).resolve()
        if tl.audio.music_path:
            tl.audio.music_path = (base / tl.audio.music_path).resolve()
        return tl

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2))
