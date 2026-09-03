"""Subtitle generation.

Soft-muxed by default rather than burned in: this ffmpeg has no libass, and a
track the viewer can switch off is better than pixels they cannot. Burn-in is
capability-gated for builds that support it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .timeline import Timeline


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str
    speaker: str = "narrator"


def collect_cues(tl: Timeline) -> list[Cue]:
    """Absolute-timeline cues from every narration line that carries text."""
    cues: list[Cue] = []
    for clip in tl.ordered_clips:
        for a in clip.audio:
            if not a.text.strip():
                continue
            start = clip.start_ms + a.offset_ms
            cues.append(Cue(start, start + a.duration_ms, a.text.strip(),
                            a.speaker))
    return sorted(cues, key=lambda c: c.start_ms)


def _ts(ms: int, sep: str) -> str:
    h, rem = divmod(max(0, ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{msec:03d}"


def write_srt(cues: list[Cue], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    blocks = [
        f"{i}\n{_ts(c.start_ms, ',')} --> {_ts(c.end_ms, ',')}\n{c.text}\n"
        for i, c in enumerate(cues, 1)
    ]
    dest.write_text("\n".join(blocks), encoding="utf-8")
    return dest


def write_vtt(cues: list[Cue], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    blocks = ["WEBVTT\n"]
    for c in cues:
        blocks.append(f"{_ts(c.start_ms, '.')} --> {_ts(c.end_ms, '.')}\n{c.text}\n")
    dest.write_text("\n".join(blocks), encoding="utf-8")
    return dest
