"""Preflight: refuse a render that cannot succeed, warn about one that will
succeed but disappoint.

Cheaper than discovering a black frame six minutes in -- and the advisory
checks catch the two mistakes that make a hybrid film look wrong rather than
broken (see docs/ARCHITECTURE.md 10.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ffmpeg import probe
from .timeline import SourceKind, Timeline

MAX_TAIL_FREEZE_MS = 1500
#: A measured file this far from its declared duration will desync the mix.
AUDIO_TOLERANCE_MS = 150


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    where: str = ""


@dataclass
class PreflightReport:
    blocking: list[Issue] = field(default_factory=list)
    advisory: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def render(self) -> str:
        lines = []
        for i in self.blocking:
            lines.append(f"  BLOCK  [{i.code}] {i.where}: {i.message}")
        for i in self.advisory:
            lines.append(f"  warn   [{i.code}] {i.where}: {i.message}")
        return "\n".join(lines) or "  no issues"


def preflight(tl: Timeline, *, deep: bool = True) -> PreflightReport:
    r = PreflightReport()
    clips = tl.ordered_clips

    for c in clips:
        where = c.shot_id
        if not c.source.path.exists():
            r.blocking.append(Issue("source_missing",
                                    f"file not found: {c.source.path}", where))
        elif deep and c.source.kind is SourceKind.CLIP:
            info = probe(c.source.path)
            if not info.ok or not info.has_video:
                r.blocking.append(Issue("source_unreadable",
                                        f"not a readable video: {info.note}", where))
            elif info.duration_ms and c.source.native_duration_ms:
                drift = abs(info.duration_ms - c.source.native_duration_ms)
                if drift > 50:
                    r.blocking.append(Issue(
                        "duration_mismatch",
                        f"timeline says {c.source.native_duration_ms}ms but the "
                        f"file measures {info.duration_ms}ms. Rebuild the "
                        f"timeline from ffprobe.", where))

        if c.tail_freeze_ms > MAX_TAIL_FREEZE_MS:
            r.blocking.append(Issue(
                "narration_overflow",
                f"needs a {c.tail_freeze_ms}ms held frame (limit "
                f"{MAX_TAIL_FREEZE_MS}ms). Shorten the line, lengthen the shot, "
                f"or drop the shot back to Ken Burns.", where))

        for a in c.audio:
            if not a.path.exists():
                r.blocking.append(Issue("audio_missing",
                                        f"narration file not found: {a.path}",
                                        f"{where}/{a.line_id}"))
            elif deep:
                info = probe(a.path)
                if not info.ok:
                    r.blocking.append(Issue("audio_unreadable", info.note,
                                            f"{where}/{a.line_id}"))
                elif info.duration_ms and abs(info.duration_ms - a.duration_ms) > AUDIO_TOLERANCE_MS:
                    r.blocking.append(Issue(
                        "audio_duration_mismatch",
                        f"timeline says {a.duration_ms}ms, file measures "
                        f"{info.duration_ms}ms", f"{where}/{a.line_id}"))

    if tl.audio.music_path and not tl.audio.music_path.exists():
        r.blocking.append(Issue("music_missing",
                                f"music bed not found: {tl.audio.music_path}"))

    # ---- advisory ---------------------------------------------------------
    animated = [c for c in clips if c.source.kind is SourceKind.CLIP]
    models = {c.source.model_key for c in animated if c.source.model_key}
    if len(models) > 2:
        r.advisory.append(Issue(
            "mixed_models",
            f"animated shots span {len(models)} models ({', '.join(sorted(models))}). "
            "Mixed providers read as inconsistent rather than varied; consider "
            "committing to one."))
    if len(animated) == 1 and len(clips) >= 6:
        r.advisory.append(Issue(
            "isolated_motion",
            f"only {animated[0].shot_id} is animated among {len(clips)} shots. "
            "A single moving shot pulls the eye; animate more or revert it."))
    if not tl.audio.music_path:
        r.advisory.append(Issue("no_music", "no music bed selected"))

    sizes = set()
    for c in animated:
        info = probe(c.source.path) if deep else None
        if info and info.ok:
            sizes.add((info.width, info.height))
    if len(sizes) > 1:
        r.advisory.append(Issue(
            "mixed_source_sizes",
            f"clips arrive at {len(sizes)} different sizes {sorted(sizes)}; they "
            "will be letterboxed to a common frame."))

    total_s = tl.total_duration_ms / 1000
    if total_s < 30:
        r.advisory.append(Issue("very_short", f"total runtime is only {total_s:.0f}s"))
    elif total_s > 300:
        r.advisory.append(Issue("very_long", f"total runtime is {total_s / 60:.1f} min"))
    return r
