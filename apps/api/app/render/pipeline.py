"""The render pipeline: Timeline -> MP4.

Stages follow docs/ARCHITECTURE.md section 10.4:

    A  normalise every source to a byte-compatible intermediate
    B  cache those intermediates by content hash
    C  build the soundtrack at absolute offsets
    D  concatenate with a stream copy (free, because A enforced uniformity)
    E  mux, attach subtitles, emit a poster
    F  verify the result against the timeline

Nothing here touches a database, the network, or the AI layer.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import audio as audio_stage
from . import cards, subtitles
from .ffmpeg import FFmpegError, capabilities, concat_demuxer_file, probe, run
from .normalize import EncodeProfile, normalize_clip
from .preflight import PreflightReport, preflight
from .timeline import Card, Clip, KenBurns, Profile, Source, SourceKind, Timeline

#: Fail loudly if the finished file drifts from the timeline by more than this.
DURATION_TOLERANCE_MS = 100

StatusFn = Callable[[str, float], None]     # (stage message, fraction 0..1)


@dataclass
class RenderResult:
    video: Path
    poster: Path | None = None
    srt: Path | None = None
    vtt: Path | None = None
    duration_ms: int | None = None
    expected_duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    bytes: int = 0
    elapsed_s: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    preflight: PreflightReport | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def drift_ms(self) -> int | None:
        if self.duration_ms is None or self.expected_duration_ms is None:
            return None
        return self.duration_ms - self.expected_duration_ms


def _card_clip(card: Card, png: Path, start_ms: int, shot_id: str) -> Clip:
    """A card is an ordinary still clip -- no special stage required."""
    return Clip(
        shot_id=shot_id,
        source=Source(kind=SourceKind.STILL, path=png),
        kenburns=KenBurns(move="static"),
        start_ms=start_ms,
        duration_ms=card.duration_ms,
        label=card.text,
    )


def render(
    tl: Timeline,
    out_path: str | Path,
    *,
    workdir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    on_status: StatusFn | None = None,
    keep_workdir: bool = False,
    skip_preflight: bool = False,
) -> RenderResult:
    started = time.monotonic()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    work = Path(workdir) if workdir else out_path.parent / f".render-{out_path.stem}"
    parts_dir, log_dir = work / "parts", work / "logs"
    for d in (work, parts_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir) if cache_dir else work.parent / ".render-cache"
    cache.mkdir(parents=True, exist_ok=True)

    def status(msg: str, frac: float) -> None:
        if on_status:
            on_status(msg, max(0.0, min(1.0, frac)))

    caps = capabilities()
    caps.require("scale", "pad", "crop", "zoompan", "fps", "format", "setsar",
                 "adelay", "amix", "loudnorm", "aresample")

    result = RenderResult(video=out_path,
                          expected_duration_ms=tl.total_duration_ms)

    # ---- preflight --------------------------------------------------------
    status("preflight", 0.01)
    report = preflight(tl) if not skip_preflight else PreflightReport()
    result.preflight = report
    if not report.ok:
        raise ValueError("preflight failed:\n" + report.render())

    ep = EncodeProfile.for_timeline(tl)

    # ---- cards become clips ----------------------------------------------
    sequence: list[Clip] = []
    if tl.title_card:
        png = cards.render_card(tl.title_card.text, work / "title-card.png",
                                width=ep.width, height=ep.height,
                                subtitle=tl.title_card.subtitle)
        sequence.append(_card_clip(tl.title_card, png, 0, "__title__"))
    sequence.extend(tl.ordered_clips)
    if tl.end_card:
        png = cards.render_card(tl.end_card.text, work / "end-card.png",
                                width=ep.width, height=ep.height,
                                subtitle=tl.end_card.subtitle)
        last_end = sequence[-1].end_ms if sequence else 0
        sequence.append(_card_clip(tl.end_card, png, last_end, "__end__"))

    # ---- Stage A/B: normalise, with a content-addressed cache -------------
    part_paths: list[Path] = []
    for i, clip in enumerate(sequence):
        key = clip.cache_key(tl.profile, ep.width, ep.height, ep.fps)
        cached = cache / f"{key}.mp4"
        part = parts_dir / f"{i:04d}.mp4"
        frac = 0.05 + 0.55 * (i / max(1, len(sequence)))
        if cached.exists() and cached.stat().st_size > 0:
            result.cache_hits += 1
            status(f"clip {i + 1}/{len(sequence)} (cached) {clip.shot_id}", frac)
        else:
            result.cache_misses += 1
            status(f"clip {i + 1}/{len(sequence)} {clip.shot_id}", frac)
            normalize_clip(clip, ep, cached, log_dir=log_dir)
        # Hard-link so re-renders are instant and the cache stays authoritative.
        try:
            if part.exists():
                part.unlink()
            part.hardlink_to(cached)
        except OSError:
            shutil.copy2(cached, part)
        part_paths.append(part)

    # ---- Stage C: soundtrack ---------------------------------------------
    status("soundtrack", 0.62)
    total_ms = sum(c.duration_ms for c in sequence)
    track = audio_stage.build_soundtrack(tl, work, total_ms, log_dir=log_dir)

    # ---- Stage D: concat (stream copy) -----------------------------------
    status("concatenating", 0.72)
    listing = concat_demuxer_file(part_paths, work / "concat.txt")
    silent = work / "video-only.mp4"
    run(["-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(silent)],
        expect=silent, log_to=log_dir / "concat.log")

    # ---- subtitles --------------------------------------------------------
    cues = subtitles.collect_cues(tl)
    srt = vtt = None
    if tl.subtitles and cues:
        srt = subtitles.write_srt(cues, out_path.with_suffix(".srt"))
        vtt = subtitles.write_vtt(cues, out_path.with_suffix(".vtt"))
        if not caps.can_burn_subtitles:
            result.warnings.append(
                "subtitles soft-muxed as a selectable track: this ffmpeg has no "
                "libass, so burn-in is unavailable (and a track the viewer can "
                "switch off is usually better anyway).")

    # ---- Stage E: mux -----------------------------------------------------
    status("muxing", 0.85)
    args = ["-y", "-i", str(silent), "-i", str(track)]
    if srt:
        args += ["-i", str(srt)]
    args += ["-map", "0:v:0", "-map", "1:a:0"]
    if srt:
        args += ["-map", "2:0", "-c:s", "mov_text",
                 "-metadata:s:s:0", "language=eng"]
    # NOT -shortest: a subtitle track legitimately ends at the last cue, and
    # -shortest would truncate the whole film to it. Video and audio are each
    # built to exactly total_ms, and Stage F verifies that they agree.
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(out_path)]
    run(args, expect=out_path, total_ms=total_ms, log_to=log_dir / "mux.log")

    # ---- poster -----------------------------------------------------------
    status("poster", 0.94)
    poster = out_path.with_suffix(".jpg")
    try:
        run(["-y", "-ss", f"{total_ms * 0.3 / 1000:.3f}", "-i", str(out_path),
             "-frames:v", "1", "-q:v", "3", str(poster)], expect=poster,
            log_to=log_dir / "poster.log")
    except FFmpegError:
        poster = None
        result.warnings.append("poster extraction failed (video is still valid)")

    # ---- Stage F: verify --------------------------------------------------
    status("verifying", 0.98)
    info = probe(out_path)
    result.duration_ms = info.duration_ms
    result.width, result.height = info.width, info.height
    result.bytes = out_path.stat().st_size
    result.poster, result.srt, result.vtt = poster, srt, vtt

    if info.duration_ms and abs(info.duration_ms - total_ms) > DURATION_TOLERANCE_MS:
        raise ValueError(
            f"render drifted: timeline expects {total_ms}ms, file measures "
            f"{info.duration_ms}ms (tolerance {DURATION_TOLERANCE_MS}ms). "
            f"Workdir kept at {work}")

    if not keep_workdir:
        shutil.rmtree(work, ignore_errors=True)
    result.elapsed_s = time.monotonic() - started
    status("done", 1.0)
    return result
