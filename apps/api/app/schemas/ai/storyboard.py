"""Stage 2 contract: the storyboard.

The most important object in the system. Two rules shape it:

1. **No provider constants.** `target_duration_s` is a float expressing
   authorial intent. Which clip lengths a model can actually produce is
   resolved later, against the capability catalogue -- a storyboard must
   outlive any one vendor's duration grid.

2. **Cross-field integrity is validated, not hoped for.** Referential errors
   (a shot naming a character that does not exist) are exactly what language
   models get wrong and exactly what a validator catches for free. The repair
   loop feeds these messages back, so they are written to be actionable by the
   model that must fix them.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ...ai.pacing import MAX_SHOT_S, MIN_SHOT_S, word_budget

SCHEMA_VERSION = "3.0"

SLUG = r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"

ShotType = Literal["establishing", "wide", "medium", "close_up", "insert",
                   "over_shoulder", "pov"]
CameraMove = Literal["static", "push_in", "pull_out", "pan_left", "pan_right",
                     "tilt_up", "tilt_down", "orbit", "handheld"]
TimeOfDay = Literal["dawn", "day", "dusk", "night", "unspecified"]
Delivery = Literal["neutral", "warm", "wistful", "excited", "tense", "playful"]
MotionPriority = Literal["low", "medium", "high"]

NARRATOR = "narrator"


class StyleBible(BaseModel):
    """The art direction, written once and injected into every prompt."""

    art_style: str = Field(min_length=1, max_length=300)
    palette: list[str] = Field(min_length=2, max_length=6)
    lighting: str = Field(min_length=1, max_length=200)
    camera_language: str = Field(min_length=1, max_length=200)
    line_and_texture: str = Field(min_length=1, max_length=200)
    #: Applies to generated motion, not to still framing.
    motion_language: str = Field(default="Gentle, unhurried camera movement.",
                                 max_length=200)
    #: Only some image models accept a negative prompt; where they do not, the
    #: composer folds these in positively.
    negative: list[str] = Field(default_factory=list, max_length=12)


class VoiceProfile(BaseModel):
    age_range: str = Field(max_length=40, default="adult")
    timbre: str = Field(max_length=80, default="warm")
    pace: Literal["slow", "measured", "brisk"] = "measured"
    accent: str = Field(max_length=60, default="unspecified")


class CharacterCanon(BaseModel):
    """Structured appearance, never a prose blob.

    Fields are rendered into a frozen `appearance_prompt` at lock time and then
    embedded byte-for-byte in every image prompt. Paraphrasing between shots is
    the single biggest cause of character drift, so the canon is structured to
    make paraphrasing impossible.
    """

    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=40)
    #: Impression, never a number: "early twenties", not "23".
    age_impression: str = Field(min_length=1, max_length=60)
    build: str = Field(min_length=1, max_length=120)
    hair: str = Field(min_length=1, max_length=160)
    eyes: str = Field(min_length=1, max_length=120)
    skin: str = Field(min_length=1, max_length=120)
    distinguishing_features: list[str] = Field(default_factory=list, max_length=4)
    default_wardrobe: str = Field(min_length=1, max_length=240)
    voice: VoiceProfile = Field(default_factory=VoiceProfile)


class LocationCanon(BaseModel):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    prompt_fragment: str = Field(min_length=1, max_length=300)


class Shot(BaseModel):
    local_index: int = Field(ge=0)
    shot_type: ShotType
    #: Capped at 3 because every reference-image-capable model we know of
    #: accepts at most 3, and multi-character frames degrade fast regardless.
    subject_slugs: list[str] = Field(default_factory=list, max_length=3)
    action: str = Field(min_length=1, max_length=400)
    composition_note: str = Field(default="", max_length=240)
    camera_move: CameraMove = "push_in"
    #: What visibly moves. Used only if this shot is later animated.
    subject_motion: str = Field(default="", max_length=300)
    ambient_sound: str = Field(default="", max_length=120)
    motion_priority: MotionPriority = "low"
    #: Authorial intent in seconds. NOT a provider grid.
    target_duration_s: float = Field(default=6.0, ge=MIN_SHOT_S, le=MAX_SHOT_S)


class NarrationLine(BaseModel):
    local_index: int = Field(ge=0)
    shot_local_index: int | None = None
    speaker: str = Field(default=NARRATOR, max_length=40)
    text: str = Field(min_length=1, max_length=400)
    delivery: Delivery = "neutral"

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class Scene(BaseModel):
    local_index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=400)
    location_slug: str | None = None
    present_slugs: list[str] = Field(default_factory=list, max_length=6)
    time_of_day: TimeOfDay = "unspecified"
    mood: str = Field(default="", max_length=120)
    shots: list[Shot] = Field(min_length=1, max_length=3)
    narration: list[NarrationLine] = Field(min_length=1, max_length=4)


class Storyboard(BaseModel):
    schema_version: Literal["3.0"] = SCHEMA_VERSION
    title: str = Field(min_length=1, max_length=120)
    logline: str = Field(min_length=1, max_length=240)
    style_bible: StyleBible
    characters: list[CharacterCanon] = Field(min_length=1, max_length=10)
    locations: list[LocationCanon] = Field(default_factory=list, max_length=12)
    scenes: list[Scene] = Field(min_length=4, max_length=20)

    # ---- integrity --------------------------------------------------------
    @model_validator(mode="after")
    def _unique_slugs(self):
        for label, items in (("character", self.characters),
                             ("location", self.locations)):
            slugs = [i.slug for i in items]
            dupes = {s for s in slugs if slugs.count(s) > 1}
            if dupes:
                raise ValueError(
                    f"duplicate {label} slug(s): {', '.join(sorted(dupes))}. "
                    "Every slug must be unique.")
        return self

    @model_validator(mode="after")
    def _referential_integrity(self):
        chars = {c.slug for c in self.characters}
        locs = {l.slug for l in self.locations}
        for sc in self.scenes:
            where = f"scene {sc.local_index} ('{sc.title}')"
            if sc.location_slug and sc.location_slug not in locs:
                raise ValueError(
                    f"{where}: location_slug '{sc.location_slug}' is not in "
                    f"locations. Known: {sorted(locs) or 'none'}.")
            if bad := set(sc.present_slugs) - chars:
                raise ValueError(
                    f"{where}: present_slugs {sorted(bad)} are not in "
                    f"characters. Known: {sorted(chars)}.")

            shot_indices = {s.local_index for s in sc.shots}
            if len(shot_indices) != len(sc.shots):
                raise ValueError(f"{where}: shot local_index values must be unique")

            for sh in sc.shots:
                if bad := set(sh.subject_slugs) - chars:
                    raise ValueError(
                        f"{where}, shot {sh.local_index}: subject_slugs "
                        f"{sorted(bad)} are not in characters. "
                        f"Known: {sorted(chars)}.")

            line_indices = {n.local_index for n in sc.narration}
            if len(line_indices) != len(sc.narration):
                raise ValueError(f"{where}: narration local_index must be unique")

            for n in sc.narration:
                if n.speaker != NARRATOR and n.speaker not in chars:
                    raise ValueError(
                        f"{where}, narration {n.local_index}: speaker "
                        f"'{n.speaker}' is neither '{NARRATOR}' nor a known "
                        f"character slug {sorted(chars)}.")
                if (n.shot_local_index is not None
                        and n.shot_local_index not in shot_indices):
                    raise ValueError(
                        f"{where}, narration {n.local_index}: shot_local_index "
                        f"{n.shot_local_index} does not exist in this scene "
                        f"(shots: {sorted(shot_indices)}).")
        return self

    @model_validator(mode="after")
    def _scenes_are_ordered(self):
        idx = [s.local_index for s in self.scenes]
        if idx != sorted(idx) or len(set(idx)) != len(idx):
            raise ValueError("scene local_index values must be unique and ordered")
        return self

    # ---- pacing -----------------------------------------------------------
    @model_validator(mode="after")
    def _narration_fits_the_shot(self):
        """Words must fit the screen time they are given.

        Checked against `target_duration_s` -- authorial intent -- not against
        any provider's clip grid. Whether a chosen model can hit that duration
        is a separate question, answered at motion-plan time.
        """
        for sc in self.scenes:
            per_shot: dict[int, int] = {}
            for n in sc.narration:
                idx = (n.shot_local_index if n.shot_local_index is not None
                       else sc.shots[0].local_index)
                per_shot[idx] = per_shot.get(idx, 0) + n.word_count
            for sh in sc.shots:
                used = per_shot.get(sh.local_index, 0)
                budget = word_budget(sh.target_duration_s)
                if used > budget:
                    raise ValueError(
                        f"scene {sc.local_index}, shot {sh.local_index}: "
                        f"{used} words of narration cannot be spoken over a "
                        f"{sh.target_duration_s:g}s shot (budget {budget} words). "
                        f"Either cut about {used - budget} words or raise "
                        f"target_duration_s to at least "
                        f"{used / 2.5 + 0.9:.1f}.")
        return self

    # ---- convenience ------------------------------------------------------
    @property
    def total_target_duration_s(self) -> float:
        return sum(sh.target_duration_s for sc in self.scenes for sh in sc.shots)

    @property
    def shot_count(self) -> int:
        return sum(len(sc.shots) for sc in self.scenes)
