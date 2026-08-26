"""ffprobe/ffmpeg helpers. Both are optional; absence degrades, never crashes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

def _resolve(tool: str) -> str | None:
    """PATH first, then a pip-installed static build if one is present.

    ``pip install imageio-ffmpeg`` is an optional dev convenience so the harness
    runs on a machine without a system ffmpeg. Production pins ffmpeg in the
    Docker image (docs/ARCHITECTURE.md 10.6).
    """
    if found := shutil.which(tool):
        return found
    if env := os.getenv(f"{tool.upper()}_BINARY"):
        return env if Path(env).exists() else None
    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


FFMPEG = _resolve("ffmpeg")
FFPROBE = _resolve("ffprobe")
HAVE_FFMPEG = FFMPEG is not None
HAVE_FFPROBE = FFPROBE is not None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Probe:
    ok: bool
    duration_ms: int | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool | None = None
    note: str = ""


def probe_video(path: Path) -> Probe:
    """Measured truth about a clip. Never trust the requested duration."""
    if not HAVE_FFPROBE:
        return _probe_with_ffmpeg(path)
    if not path.exists() or path.stat().st_size == 0:
        return Probe(ok=False, note="file missing or empty")
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        data = json.loads(out)
    except Exception as exc:                      # noqa: BLE001 - report, don't raise
        return Probe(ok=False, note=f"ffprobe failed: {exc}")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = data.get("format", {}).get("duration")
    fps = None
    if video and (rate := video.get("avg_frame_rate")) and "/" in rate:
        num, den = rate.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else None
    return Probe(
        ok=True,
        duration_ms=int(round(float(duration) * 1000)) if duration else None,
        fps=fps,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        has_audio=audio is not None,
    )


def _probe_with_ffmpeg(path: Path) -> Probe:
    """Fallback measurement by parsing ``ffmpeg -i`` stderr when ffprobe is absent."""
    if not HAVE_FFMPEG:
        return Probe(ok=False, note="neither ffprobe nor ffmpeg available")
    if not path.exists() or path.stat().st_size == 0:
        return Probe(ok=False, note="file missing or empty")
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, timeout=60).stderr
    m = re.search(r"Duration: (\d+):(\d\d):(\d\d\.\d+)", err)
    duration_ms = None
    if m:
        h, mi, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration_ms = int(round((h * 3600 + mi * 60 + sec) * 1000))
    dims = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", err)
    fps_m = re.search(r"(\d+(?:\.\d+)?) fps", err)
    if duration_ms is None and dims is None:
        return Probe(ok=False, note="could not parse ffmpeg output")
    return Probe(
        ok=True, duration_ms=duration_ms,
        fps=float(fps_m.group(1)) if fps_m else None,
        width=int(dims.group(1)) if dims else None,
        height=int(dims.group(2)) if dims else None,
        has_audio="Audio:" in err,
        note="measured via ffmpeg (ffprobe unavailable)",
    )


RES_DIMS = {"480p": (854, 480), "540p": (960, 540), "720p": (1280, 720),
            "768p": (1366, 768), "1080p": (1920, 1080)}


async def synthesize_clip(
    *, first_frame: Path, dest: Path, duration_s: float, resolution: str,
    label: str,
) -> tuple[bool, str]:
    """Build a stand-in clip from the first frame: slow zoom plus a caption.

    Used by the fake adapter so the whole pipeline -- including the report and
    contact sheet -- can be exercised end to end with zero spend.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    w, h = RES_DIMS.get(resolution, (1280, 720))
    if not HAVE_FFMPEG:
        # Degrade honestly rather than fabricating a media file.
        dest.write_bytes(b"FAKE-CLIP-PLACEHOLDER " + label.encode() + b"\n")
        return False, "ffmpeg not installed; wrote a non-video placeholder"

    fps = 24
    frames = max(1, int(duration_s * fps))
    text = label.replace(":", r"\:").replace("'", "")
    vf = (
        f"scale={w * 2}:-2:flags=lanczos,"
        f"zoompan=z='min(1.0+0.12*on/{frames},1.12)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={w * 2}x{h * 2}:fps={fps},"
        f"scale={w}:{h}:flags=lanczos,"
        f"drawtext=text='{text}':fontcolor=white:fontsize={max(14, h // 28)}:"
        f"box=1:boxcolor=black@0.6:boxborderw=10:x=(w-text_w)/2:y=h-th-24,"
        f"format=yuv420p,setsar=1"
    )
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-y", "-loglevel", "error", "-loop", "1", "-i", str(first_frame),
        "-t", f"{duration_s:g}", "-vf", vf, "-r", str(fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", str(dest),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        return False, f"ffmpeg failed: {err.decode()[-400:]}"
    return True, ""
