"""Domain 7: narration.

The measured audio duration is the truth from which everything downstream is
built -- shot screen time, subtitle cues, audio offsets. Nothing here estimates
a duration that could instead be known.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.pacing import PAD_S, required_seconds, word_budget
from ..db.models import Asset, Character, NarrationLine, Shot

#: Beyond this, a held frame stops reading as a beat and starts reading as a
#: fault. Preflight blocks a final render past it.
MAX_TAIL_FREEZE_MS = 1500
#: Below this much slack the line lands but has no room to breathe.
TIGHT_SLACK_MS = 750


class FitStatus(StrEnum):
    FITS = "fits"
    TIGHT = "tight"
    OVERFLOW = "overflow"
    UNKNOWN = "unknown"          # no audio recorded yet


@dataclass
class NarrationFit:
    status: FitStatus
    words: int
    word_budget: int
    shot_ms: int
    narration_ms: int
    required_ms: int
    slack_ms: int

    @property
    def tail_freeze_ms(self) -> int:
        return max(0, -self.slack_ms)

    @property
    def blocks_render(self) -> bool:
        return self.tail_freeze_ms > MAX_TAIL_FREEZE_MS

    def message(self) -> str:
        if self.status is FitStatus.UNKNOWN:
            return "no narration recorded yet"
        if self.status is FitStatus.FITS:
            return (f"{self.words} words fit the {self.shot_ms / 1000:.1f}s shot "
                    f"with {self.slack_ms / 1000:.1f}s spare")
        if self.status is FitStatus.TIGHT:
            return (f"{self.words} words only just fit "
                    f"({self.slack_ms / 1000:.1f}s spare)")
        return (f"{self.words} words need {self.required_ms / 1000:.1f}s but the "
                f"shot is {self.shot_ms / 1000:.1f}s. Shorten the line, lengthen "
                f"the shot, or accept a {self.tail_freeze_ms / 1000:.1f}s held frame")


def evaluate_fit(shot: Shot, lines: list[NarrationLine]) -> NarrationFit:
    """Fit is measured against recorded audio when it exists, and estimated
    from the word count only while it does not."""
    words = sum(len(l.text.split()) for l in lines)
    shot_ms = int(float(shot.target_duration_s) * 1000)
    budget = word_budget(float(shot.target_duration_s))

    recorded = [l for l in lines if l.duration_ms]
    if not recorded:
        est_ms = int(required_seconds(words) * 1000)
        slack = shot_ms - est_ms
        return NarrationFit(FitStatus.UNKNOWN, words, budget, shot_ms, 0,
                            est_ms, slack)

    narration_ms = sum(int(l.duration_ms or 0) for l in lines)
    required_ms = narration_ms + int(PAD_S * 1000)
    slack = shot_ms - required_ms
    status = (FitStatus.OVERFLOW if slack < 0
              else FitStatus.TIGHT if slack < TIGHT_SLACK_MS
              else FitStatus.FITS)
    return NarrationFit(status, words, budget, shot_ms, narration_ms,
                        required_ms, slack)


async def voice_for_line(session: AsyncSession, line: NarrationLine,
                         default_voice: str) -> str:
    """The narrator's voice unless the line belongs to a character with one."""
    if line.speaker_slug and line.speaker_slug != "narrator":
        character = (await session.execute(
            select(Character).where(Character.project_id == line.project_id,
                                    Character.slug == line.speaker_slug)
        )).scalar_one_or_none()
        if character and (voice := (character.voice or {}).get("voice_name")):
            return voice
    return default_voice


def audio_is_fresh(line: NarrationLine, asset: Asset | None) -> bool:
    """Same rule as stills: derived from the hash of what produced it."""
    from ..db.models import AssetSource
    if asset is None:
        return False
    if asset.source is AssetSource.MANUAL:
        return True
    return bool(line.input_hash) and asset.input_hash == line.input_hash


def line_input_hash(text: str, voice: str, delivery: str, model: str) -> str:
    import hashlib
    return hashlib.sha256(
        f"{text}|{voice}|{delivery}|{model}".encode()).hexdigest()
