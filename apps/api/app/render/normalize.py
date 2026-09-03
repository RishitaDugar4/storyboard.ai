"""Stage A: every source becomes a byte-compatible intermediate.

Uniformity is the whole point: identical codec, pixel format, fps, SAR and
dimensions let Stage D concatenate with a stream copy instead of a re-encode,
which is both faster and lossless. It also means a mixed timeline -- some shots
generated, some Ken Burns over stills -- concatenates without special cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import kenburns
from .ffmpeg import ProgressFn, run
from .timeline import Clip, Profile, SourceKind, Timeline


@dataclass(frozen=True)
class EncodeProfile:
    width: int
    height: int
    fps: int
    crf: int
    preset: str

    @classmethod
    def for_timeline(cls, tl: Timeline) -> "EncodeProfile":
        if tl.profile is Profile.PREVIEW:
            return cls(tl.width, tl.height, tl.fps, crf=26, preset="veryfast")
        return cls(tl.width, tl.height, tl.fps, crf=18, preset="medium")


def _tpad(tail_freeze_ms: int) -> str:
    if tail_freeze_ms <= 0:
        return ""
    return f"tpad=stop_mode=clone:stop_duration={tail_freeze_ms / 1000:.3f},"


def clip_filter_chain(clip: Clip, ep: EncodeProfile) -> str:
    """Scale-to-fit + pad, never crop.

    A generated clip is content the user approved; letterboxing preserves all
    of it, whereas cropping would silently remove framing they chose. Stills
    are the opposite case -- see kenburns.filter_chain.
    """
    return (
        f"scale={ep.width}:{ep.height}:force_original_aspect_ratio=decrease:"
        f"flags=lanczos,"
        f"pad={ep.width}:{ep.height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={ep.fps},"
        f"{_tpad(clip.tail_freeze_ms)}"
        f"format=yuv420p,setsar=1"
    )


def normalize_clip(
    clip: Clip, ep: EncodeProfile, dest: Path, *,
    on_progress: ProgressFn | None = None, log_dir: Path | None = None,
) -> Path:
    """Render one Timeline clip to a uniform intermediate."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = clip.source.path
    if not src.exists():
        raise FileNotFoundError(f"{clip.shot_id}: source missing: {src}")

    if clip.source.kind is SourceKind.STILL:
        vf = kenburns.filter_chain(
            clip.kenburns, width=ep.width, height=ep.height, fps=ep.fps,
            duration_ms=clip.duration_ms)
        args = ["-y", "-loop", "1", "-i", str(src),
                "-t", f"{clip.duration_ms / 1000:.3f}", "-vf", vf]
    else:
        # Discard provider audio here, uniformly: several models force audio on
        # and invent dialogue that would fight the narrator.
        args = ["-y", "-i", str(src), "-an",
                "-vf", clip_filter_chain(clip, ep)]

    args += ["-r", str(ep.fps), "-c:v", "libx264", "-preset", ep.preset,
             "-crf", str(ep.crf), "-pix_fmt", "yuv420p", str(dest)]
    run(args, expect=dest, total_ms=clip.duration_ms, on_progress=on_progress,
        log_to=(log_dir / f"normalize-{clip.shot_id}.log") if log_dir else None)
    return dest
