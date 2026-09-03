"""Stage C: the soundtrack.

Every narration line is placed at an ABSOLUTE offset on the finished timeline
rather than concatenated in sequence. Sequential assembly makes one wrong
duration shift everything after it; absolute placement confines the error to
the line that owns it.
"""
from __future__ import annotations

from pathlib import Path

from .ffmpeg import ProgressFn, probe, run
from .timeline import Timeline

SILENCE = "anullsrc=channel_layout=stereo:sample_rate={sr}"


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def build_narration(tl: Timeline, dest: Path, total_ms: int,
                    *, log_dir: Path | None = None) -> Path:
    """Mix every narration line onto one continuous track.

    A silent bed of the full length is always input 0, so the track spans the
    whole film even when narration is sparse -- the mux then never has to guess
    with -shortest.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    sr = tl.audio.sample_rate
    cues = [(clip.start_ms + a.offset_ms, a)
            for clip in tl.ordered_clips for a in clip.audio]
    cues.sort(key=lambda t: t[0])

    args: list[str] = ["-y", "-f", "lavfi", "-t", _seconds(total_ms),
                       "-i", SILENCE.format(sr=sr)]
    for _, cue in cues:
        args += ["-i", str(cue.path)]

    parts, labels = [], ["[0:a]"]
    for idx, (abs_ms, _cue) in enumerate(cues, start=1):
        parts.append(f"[{idx}:a]aresample={sr},adelay={abs_ms}:all=1[d{idx}]")
        labels.append(f"[d{idx}]")

    graph = ";".join(parts)
    if parts:
        graph += ";"
    graph += (f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:"
              f"duration=first[mixed];"
              f"[mixed]loudnorm=I={tl.audio.loudness_target_lufs}:TP=-1.5:LRA=11,"
              f"aresample={sr}[out]")

    args += ["-filter_complex", graph, "-map", "[out]",
             "-ac", "2", "-ar", str(sr), str(dest)]
    run(args, expect=dest,
        log_to=(log_dir / "audio-narration.log") if log_dir else None)
    return dest


def build_music(tl: Timeline, dest: Path, total_ms: int,
                *, log_dir: Path | None = None) -> Path | None:
    """Loop the licensed bed to length, drop it well under the narration,
    and fade both ends."""
    if not tl.audio.music_path:
        return None
    src = Path(tl.audio.music_path)
    if not src.exists():
        raise FileNotFoundError(f"music bed missing: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    total_s = total_ms / 1000
    fade_out_at = max(0.0, total_s - tl.audio.music_fade_out_s)
    af = (f"volume={tl.audio.music_db}dB,"
          f"afade=t=in:st=0:d={tl.audio.music_fade_in_s},"
          f"afade=t=out:st={fade_out_at:.3f}:d={tl.audio.music_fade_out_s},"
          f"aresample={tl.audio.sample_rate}")
    run(["-y", "-stream_loop", "-1", "-i", str(src), "-t", _seconds(total_ms),
         "-af", af, "-ac", "2", "-ar", str(tl.audio.sample_rate), str(dest)],
        expect=dest, log_to=(log_dir / "audio-music.log") if log_dir else None)
    return dest


def build_soundtrack(
    tl: Timeline, workdir: Path, total_ms: int, *,
    on_progress: ProgressFn | None = None, log_dir: Path | None = None,
) -> Path:
    """Narration plus optional music bed -> one finished audio track."""
    narration = build_narration(tl, workdir / "narration.wav", total_ms,
                                log_dir=log_dir)
    music = build_music(tl, workdir / "music.wav", total_ms, log_dir=log_dir)
    if music is None:
        return narration

    dest = workdir / "soundtrack.wav"
    # normalize=0 keeps the bed genuinely quiet; amix's default averaging would
    # pull the narration down to meet it.
    run(["-y", "-i", str(narration), "-i", str(music),
         "-filter_complex",
         "[0:a][1:a]amix=inputs=2:weights=1 1:normalize=0:duration=first[out]",
         "-map", "[out]", "-ac", "2", "-ar", str(tl.audio.sample_rate),
         str(dest)],
        expect=dest, log_to=(log_dir / "audio-mix.log") if log_dir else None)
    return dest


def synth_placeholder_narration(
    dest: Path, duration_ms: int, *, sample_rate: int = 48000,
    freq: int = 190,
) -> Path:
    """A stand-in narration line: a soft, faded tone of exact length.

    Not speech, but it has the right envelope and the right duration, which is
    all the timing machinery needs before a TTS provider exists.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    secs = duration_ms / 1000
    af = (f"volume=-19dB,afade=t=in:st=0:d=0.12,"
          f"afade=t=out:st={max(0.0, secs - 0.18):.3f}:d=0.18")
    run(["-y", "-f", "lavfi", "-t", f"{secs:.3f}",
         "-i", f"sine=frequency={freq}:sample_rate={sample_rate}",
         "-af", af, "-ac", "2", str(dest)], expect=dest)
    return dest
