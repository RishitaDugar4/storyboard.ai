#!/usr/bin/env python3
"""Build demo Timelines from the M0.5 bake-off assets.

Produces two timelines over the same story beats:

  demo-preview.json  every shot is a still + Ken Burns   (the free default)
  demo-final.json    the same film with real clips swapped in  (the hybrid)

This is also where the duration algorithm from ARCHITECTURE 10.3 lives for M1;
in the application it moves into render_service.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "apps" / "api"))

from app.render.audio import synth_placeholder_narration      # noqa: E402
from app.render.ffmpeg import probe, run                       # noqa: E402
from app.render.timeline import (AudioCue, AudioMix, Card,     # noqa: E402
                                 Clip, KenBurns, Profile, Source,
                                 SourceKind, Timeline)

BAKEOFF = REPO / "tools" / "bakeoff"
STILLS = BAKEOFF / "inputs"
CLIPS = BAKEOFF / "out" / "clips"
OUT = REPO / "apps" / "api" / "tests" / "fixtures" / "demo"

WORDS_PER_SECOND = 2.5
PAD_MS = 900          # 300ms lead-in + 600ms tail

SHOTS = [
    ("s1", "01-character-closeup",     "push_in",   6.0,
     "It was the last winter the light would burn."),
    ("s2", "03-establishing-wide",     "pan_right", 6.0,
     "Beyond the window, the sea went on."),
    ("s3", "02-two-characters-medium", "static",    5.0,
     "She had kept it lit for forty years."),
    ("s4", "03-establishing-wide",     "pull_out",  6.0,
     "No one had ever asked her to."),
    ("s5", "01-character-closeup",     "tilt_up",   5.0,
     "Then, one evening, she heard it."),
    ("s6", "02-two-characters-medium", "push_in",   6.0,
     "A knock, from the far side of the storm."),
]


def narration_ms(text: str) -> int:
    return int(round(len(text.split()) / WORDS_PER_SECOND * 1000))


def pick_clips() -> dict[str, Path]:
    """Assign real clips to a few shots -- deliberately from more than one
    model and more than one resolution, so normalisation is exercised."""
    chosen: dict[str, Path] = {}
    real = CLIPS / "hailuo-02-standard-i2v" / "03-establishing-wide--push-in--5s--0.mp4"
    if real.exists():
        chosen["s2"] = real                      # 1364x768, the live generation
    for shot, model in (("s4", "kling-2.5-turbo-i2v"),
                        ("s6", "veo-3.1-standard-i2v")):
        d = CLIPS / model
        if d.exists():
            for cand in sorted(d.glob("*.mp4")):
                chosen[shot] = cand
                break
    return chosen


def build(profile: Profile, use_clips: bool, audio_dir: Path) -> Timeline:
    clip_for = pick_clips() if use_clips else {}
    width, height = (1280, 720) if profile is Profile.PREVIEW else (1920, 1080)

    title = Card(text="The Lighthouse Keeper", subtitle="a demo render",
                 duration_ms=2500)
    cursor = title.duration_ms
    clips: list[Clip] = []

    for shot_id, still_stem, move, target_s, line in SHOTS:
        n_ms = narration_ms(line)
        required = n_ms + PAD_MS
        src_clip = clip_for.get(shot_id)

        if src_clip:
            info = probe(src_clip)
            native = info.duration_ms or int(target_s * 1000)
            duration = max(native, required)
            tail = max(0, required - native)
            source = Source(kind=SourceKind.CLIP, path=src_clip,
                            native_duration_ms=native, has_audio=info.has_audio,
                            provider="veo" if "veo" in src_clip.parent.name else "fal",
                            model_key=src_clip.parent.name)
            kb = None
        else:
            duration = max(int(target_s * 1000), required)
            tail = 0
            source = Source(kind=SourceKind.STILL, path=STILLS / f"{still_stem}.png")
            kb = KenBurns(move=move)

        wav = audio_dir / f"{shot_id}.wav"
        if not wav.exists():
            synth_placeholder_narration(wav, n_ms)

        clips.append(Clip(
            shot_id=shot_id, scene_index=len(clips), shot_index=0,
            source=source, kenburns=kb, start_ms=cursor, duration_ms=duration,
            tail_freeze_ms=tail, label=line,
            audio=[AudioCue(line_id=f"{shot_id}-n1", path=wav, offset_ms=300,
                            duration_ms=n_ms, text=line)],
        ))
        cursor += duration

    music = audio_dir / "music-bed.wav"
    if not music.exists():
        total_s = (cursor + 3000) / 1000
        run(["-y", "-f", "lavfi", "-t", f"{total_s:.3f}",
             "-i", "sine=frequency=110:sample_rate=48000",
             "-f", "lavfi", "-t", f"{total_s:.3f}",
             "-i", "sine=frequency=164.81:sample_rate=48000",
             "-filter_complex",
             "[0][1]amix=inputs=2:normalize=0,volume=-6dB,"
             "tremolo=f=0.12:d=0.35,aresample=48000[m]",
             "-map", "[m]", "-ac", "2", str(music)], expect=music)

    return Timeline(
        profile=profile, title="The Lighthouse Keeper",
        width=width, height=height, fps=24,
        audio=AudioMix(music_path=music, music_db=-24.0),
        title_card=title,
        end_card=Card(text="Happy Birthday", duration_ms=3000),
        clips=clips, subtitles=True,
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    audio_dir = OUT / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    for name, profile, use_clips in (
        ("demo-preview.json", Profile.PREVIEW, False),
        ("demo-final.json", Profile.FINAL, True),
    ):
        tl = build(profile, use_clips, audio_dir)
        tl.save(OUT / name)
        kinds: dict[str, int] = {}
        for c in tl.clips:
            kinds[str(c.source.kind)] = kinds.get(str(c.source.kind), 0) + 1
        print(f"  {name:20} {tl.width}x{tl.height}  "
              f"{tl.total_duration_ms / 1000:5.1f}s  {len(tl.clips)} shots  {kinds}")
        freeze = [(c.shot_id, c.tail_freeze_ms) for c in tl.clips if c.tail_freeze_ms]
        if freeze:
            print(f"                       tail freeze: {freeze}")
    print(f"\n  written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
