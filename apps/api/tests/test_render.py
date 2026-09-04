"""Renderer tests. No network, no database; ffmpeg-dependent tests are marked."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.render import preflight
from app.render.ffmpeg import capabilities, probe
from app.render.kenburns import filter_chain, zoompan_expr
from app.render.subtitles import collect_cues, write_srt
from app.render.timeline import (AudioCue, Card, CameraMove, Clip, KenBurns,
                                 Profile, Source, SourceKind, Timeline)

FIXTURES = Path(__file__).parent / "fixtures" / "demo"
needs_ffmpeg = pytest.mark.skipif(not capabilities().ffmpeg, reason="ffmpeg absent")


def _still(path="a.png", **kw):
    return Clip(shot_id=kw.pop("shot_id", "s1"),
                source=Source(kind=SourceKind.STILL, path=path),
                start_ms=kw.pop("start_ms", 0),
                duration_ms=kw.pop("duration_ms", 5000), **kw)


# ---- contract -------------------------------------------------------------
def test_still_gets_default_kenburns():
    assert _still().kenburns.move is CameraMove.PUSH_IN


def test_clip_requires_measured_duration():
    with pytest.raises(ValueError, match="native_duration_ms"):
        Clip(shot_id="s", source=Source(kind=SourceKind.CLIP, path="a.mp4"),
             start_ms=0, duration_ms=1000)


def test_kenburns_rejected_on_clip():
    with pytest.raises(ValueError, match="only valid on a still"):
        Clip(shot_id="s",
             source=Source(kind=SourceKind.CLIP, path="a.mp4",
                           native_duration_ms=1000),
             kenburns=KenBurns(), start_ms=0, duration_ms=1000)


def test_timeline_rejects_gaps():
    with pytest.raises(ValueError, match="not contiguous"):
        Timeline(clips=[_still(shot_id="a", start_ms=0, duration_ms=1000),
                        _still(shot_id="b", start_ms=5000, duration_ms=1000)])


def test_narration_may_not_outrun_its_clip():
    with pytest.raises(ValueError, match="padded the visual"):
        _still(duration_ms=2000,
               audio=[AudioCue(line_id="l", path="n.wav", offset_ms=300,
                               duration_ms=3000)])


def test_title_card_offsets_the_first_clip():
    tl = Timeline(title_card=Card(text="T", duration_ms=2500),
                  clips=[_still(start_ms=2500, duration_ms=4000)])
    assert tl.total_duration_ms == 6500


# ---- ken burns ------------------------------------------------------------
@pytest.mark.parametrize("move", list(CameraMove))
def test_every_move_yields_expressions(move):
    z, x, y = zoompan_expr(KenBurns(move=move), 120)
    assert z and x and y


def test_pans_travel_in_opposite_directions():
    _, left, _ = zoompan_expr(KenBurns(move=CameraMove.PAN_LEFT), 100)
    _, right, _ = zoompan_expr(KenBurns(move=CameraMove.PAN_RIGHT), 100)
    assert left != right and "1-" in left


def test_chain_supersamples_then_downscales():
    chain = filter_chain(KenBurns(), width=1280, height=720, fps=24,
                         duration_ms=5000)
    assert "scale=2560:1440" in chain and chain.endswith("setsar=1")
    assert "d=120:" in chain          # 5s * 24fps


def test_single_frame_clip_does_not_divide_by_zero():
    z, _, _ = zoompan_expr(KenBurns(), 1)
    assert "/1" in z or z == "1.0"


# ---- cache ----------------------------------------------------------------
def test_cache_key_tracks_duration_and_profile(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG" + b"0" * 64)
    c = _still(path=img)
    base = c.cache_key(Profile.FINAL, 1920, 1080, 24)
    assert base == c.cache_key(Profile.FINAL, 1920, 1080, 24)
    assert base != c.cache_key(Profile.PREVIEW, 1920, 1080, 24)
    assert base != _still(path=img, duration_ms=6000).cache_key(
        Profile.FINAL, 1920, 1080, 24)


def test_cache_key_follows_source_content(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"one")
    before = _still(path=img).cache_key(Profile.FINAL, 1920, 1080, 24)
    img.write_bytes(b"two")
    assert before != _still(path=img).cache_key(Profile.FINAL, 1920, 1080, 24)


# ---- subtitles ------------------------------------------------------------
def test_cues_use_absolute_timeline_offsets():
    tl = Timeline(clips=[
        _still(shot_id="a", start_ms=0, duration_ms=4000,
               audio=[AudioCue(line_id="1", path="n.wav", offset_ms=300,
                               duration_ms=2000, text="one")]),
        _still(shot_id="b", start_ms=4000, duration_ms=4000,
               audio=[AudioCue(line_id="2", path="n.wav", offset_ms=300,
                               duration_ms=2000, text="two")]),
    ])
    cues = collect_cues(tl)
    assert [c.start_ms for c in cues] == [300, 4300]


def test_srt_timestamps(tmp_path):
    tl = Timeline(clips=[_still(audio=[AudioCue(
        line_id="1", path="n.wav", offset_ms=1500, duration_ms=2000,
        text="hello")])])
    body = write_srt(collect_cues(tl), tmp_path / "s.srt").read_text()
    assert "00:00:01,500 --> 00:00:03,500" in body


# ---- preflight ------------------------------------------------------------
def test_missing_source_blocks():
    r = preflight(Timeline(clips=[_still(path="/nope/missing.png")]), deep=False)
    assert not r.ok and r.blocking[0].code == "source_missing"


def test_excessive_tail_freeze_blocks(tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"0")
    tl = Timeline(clips=[Clip(
        shot_id="s", source=Source(kind=SourceKind.CLIP, path=clip,
                                   native_duration_ms=4000),
        start_ms=0, duration_ms=6000, tail_freeze_ms=2000)])
    codes = {i.code for i in preflight(tl, deep=False).blocking}
    assert "narration_overflow" in codes


def test_missing_music_is_advisory_not_blocking():
    r = preflight(Timeline(clips=[_still(path=__file__)]), deep=False)
    assert "no_music" in {i.code for i in r.advisory}


# ---- end to end -----------------------------------------------------------
@needs_ffmpeg
@pytest.mark.skipif(not (FIXTURES / "demo-preview.json").exists(),
                    reason="run `make demo-fixtures` first")
def test_demo_timelines_pass_preflight():
    for name in ("demo-preview.json", "demo-final.json"):
        assert preflight(Timeline.load(FIXTURES / name)).ok, name


@needs_ffmpeg
@pytest.mark.skipif(not (FIXTURES / "demo-preview.json").exists(),
                    reason="run `make demo-fixtures` first")
@pytest.mark.timeout(600)
def test_preview_renders_within_tolerance(tmp_path):
    from app.render import render
    tl = Timeline.load(FIXTURES / "demo-preview.json")
    tl.clips = tl.clips[:2]
    tl.end_card = None
    for i, c in enumerate(tl.clips):     # re-tile after truncating
        c.start_ms = (tl.title_card.duration_ms if tl.title_card else 0) + sum(
            x.duration_ms for x in tl.clips[:i])
    res = render(tl, tmp_path / "out.mp4", cache_dir=tmp_path / "cache")
    assert abs(res.drift_ms) <= 100
    assert probe(res.video).has_audio
