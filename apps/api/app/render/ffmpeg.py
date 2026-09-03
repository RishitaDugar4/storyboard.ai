"""ffmpeg/ffprobe process layer: capability probing, execution, measurement.

Every filter this renderer depends on is checked at import rather than assumed.
Builds differ substantially -- Homebrew's default ffmpeg ships without
`drawtext`, `subtitles` and `ass` (no libfreetype/libass) -- and a missing
filter must degrade to a documented fallback, never to a failed render on the
night before a deadline.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence


class FFmpegError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str) -> None:
        tail = "\n".join(stderr.strip().splitlines()[-25:])
        super().__init__(f"ffmpeg exited {returncode}\n{tail}")
        self.cmd, self.returncode, self.stderr = list(cmd), returncode, stderr


def _which(tool: str) -> str | None:
    if found := shutil.which(tool):
        return found
    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


FFMPEG = _which("ffmpeg")
FFPROBE = _which("ffprobe")


@dataclass(frozen=True)
class Capabilities:
    ffmpeg: bool
    ffprobe: bool
    filters: frozenset[str]
    encoders: frozenset[str]

    def has(self, *names: str) -> bool:
        return all(n in self.filters for n in names)

    @property
    def can_burn_subtitles(self) -> bool:
        """Burn-in needs libass. Without it we soft-mux a sidecar track, which
        is arguably better anyway -- the viewer can turn it off."""
        return "subtitles" in self.filters or "ass" in self.filters

    @property
    def can_draw_text(self) -> bool:
        """Text cards are rendered with Pillow instead when this is False."""
        return "drawtext" in self.filters

    def require(self, *names: str) -> None:
        missing = [n for n in names if n not in self.filters]
        if missing:
            raise RuntimeError(
                f"this ffmpeg build lacks required filter(s): {', '.join(missing)}. "
                f"Install a fuller build (macOS: brew install ffmpeg).")


@lru_cache(maxsize=1)
def capabilities() -> Capabilities:
    if not FFMPEG:
        return Capabilities(False, bool(FFPROBE), frozenset(), frozenset())
    filters: set[str] = set()
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=30).stdout
        # Lines look like "  T.. scale   V->V   Scale the input video."
        # The 3-char flag column may contain spaces, so anchor on the "A->A"
        # arrow token and take the name immediately before it.
        for line in out.splitlines():
            parts = line.split()
            for i, tok in enumerate(parts):
                if "->" in tok and i >= 1:
                    filters.add(parts[i - 1])
                    break
    except Exception:
        pass
    encoders: set[str] = set()
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            m = re.match(r"^\s*[A-Z.]{6}\s+(\S+)\s", line)
            if m:
                encoders.add(m.group(1))
    except Exception:
        pass
    return Capabilities(True, bool(FFPROBE), frozenset(filters), frozenset(encoders))


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
@dataclass
class MediaInfo:
    ok: bool
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool = False
    has_video: bool = False
    codec: str | None = None
    note: str = ""


def probe(path: str | Path) -> MediaInfo:
    """Measured truth about a media file."""
    path = Path(path)
    if not FFPROBE:
        return MediaInfo(False, note="ffprobe not installed")
    if not path.exists() or path.stat().st_size == 0:
        return MediaInfo(False, note=f"missing or empty: {path}")
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120, check=True).stdout
        data = json.loads(out)
    except Exception as exc:
        return MediaInfo(False, note=f"ffprobe failed: {exc}")

    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = data.get("format", {}).get("duration")
    fps = None
    if v and (rate := v.get("avg_frame_rate", "0/0")) and "/" in rate:
        num, den = rate.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else None
    return MediaInfo(
        ok=True,
        duration_ms=int(round(float(dur) * 1000)) if dur else None,
        width=v.get("width") if v else None,
        height=v.get("height") if v else None,
        fps=fps, has_audio=a is not None, has_video=v is not None,
        codec=v.get("codec_name") if v else None,
    )


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
_TIME_RE = re.compile(r"time=(\d+):(\d\d):(\d\d\.\d+)")

ProgressFn = Callable[[float], None]     # fraction 0..1


def run(
    args: Sequence[str],
    *,
    expect: Path | None = None,
    total_ms: int | None = None,
    on_progress: ProgressFn | None = None,
    log_to: Path | None = None,
) -> str:
    """Run ffmpeg, streaming stderr so real progress can be reported.

    Users tolerate a long render when a bar is moving; they do not tolerate a
    frozen terminal.
    """
    if not FFMPEG:
        raise RuntimeError("ffmpeg not installed (macOS: brew install ffmpeg)")
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
           "-stats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    collected: list[str] = []
    assert proc.stderr is not None
    for line in proc.stderr:
        collected.append(line)
        if on_progress and total_ms:
            if m := _TIME_RE.search(line):
                h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                done = (h * 3600 + mi * 60 + s) * 1000
                on_progress(max(0.0, min(1.0, done / total_ms)))
    proc.wait()
    stderr = "".join(collected)
    if log_to:
        log_to.parent.mkdir(parents=True, exist_ok=True)
        log_to.write_text(" ".join(cmd) + "\n\n" + stderr)
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, stderr)
    if expect is not None and (not expect.exists() or expect.stat().st_size == 0):
        raise FFmpegError(cmd, 0, stderr + f"\nexpected output missing: {expect}")
    return stderr


def concat_demuxer_file(paths: Iterable[Path], dest: Path) -> Path:
    """Write the concat list. Paths are quoted per the demuxer's escaping rules."""
    lines = [f"file '{str(p.resolve()).replace(chr(39), chr(39) * 3)}'"
             for p in paths]
    dest.write_text("\n".join(lines) + "\n")
    return dest
