"""M6: image-to-video — the catalogue, planning, validation, and assembly.

No network and no spend. The fake video adapter produces a real, probe-able
MP4, so the validation stage is exercised here rather than discovered on the
first paid generation.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ["AI_TEXT_PROVIDER"] = "fake"
os.environ["AI_IMAGE_PROVIDER"] = "fake"
os.environ["AI_SPEECH_PROVIDER"] = "fake"
os.environ["AI_VIDEO_PROVIDER"] = "fake"
os.environ["JOB_QUEUE"] = "inline"
# The catalogue's real latencies are 79-180s and the inline queue honours
# defer_s, so without this the suite sleeps for minutes waiting on a fake
# provider that finished instantly.
os.environ["MOTION_POLL_SCALE"] = "0"

from app.ai.adapters.fakes import FakeVideoAdapter          # noqa: E402
from app.ai.catalog import CATALOG, ModelStatus             # noqa: E402
from app.ai.catalog import get as get_caps                  # noqa: E402
from app.ai.ports import VideoRequest                       # noqa: E402
from app.ai.prompts.compose import (MOTION_NEGATIVE,        # noqa: E402
                                    compose_motion_prompt)
from app.db.models import (Asset, AssetSource, MotionMode,  # noqa: E402
                           Shot)
from app.jobs.handlers.motion import (ClipInvalid,          # noqa: E402
                                      DURATION_TOLERANCE_MS, validate_clip)
from app.render.ffmpeg import capabilities                  # noqa: E402
from app.services.motion_service import (DEFAULT_MODEL_KEY,  # noqa: E402
                                         cheapest_capable, clip_is_fresh,
                                         plan_film_durations,
                                         selectable_models)

pytestmark = pytest.mark.asyncio
needs_ffmpeg = pytest.mark.skipif(not capabilities().ffmpeg,
                                  reason="ffmpeg absent")


# ---- the catalogue is the only place provider facts live ------------------
def test_every_active_model_can_animate_an_image():
    """The pipeline is image-to-video. A catalogue entry that cannot take a
    first frame has no place being selectable."""
    for caps in CATALOG.values():
        if caps.status is ModelStatus.ACTIVE:
            assert caps.image_to_video, caps.model_key


def test_request_fields_are_declared_per_model():
    """The bake-off found three separate 422s from sending fields an endpoint
    does not define. The allowlist makes that impossible by construction, so
    it must actually be populated.

    Its scope differs by adapter: fal takes one flat body, so `prompt` is in
    the allowlist; Veo sends prompt and keyframe in `instances` and the
    allowlist governs `parameters` only.
    """
    for caps in CATALOG.values():
        assert caps.request_fields, caps.model_key
        if caps.adapter == "fal":
            assert "prompt" in caps.request_fields, caps.model_key
        elif caps.adapter == "veo":
            assert "prompt" not in caps.request_fields, caps.model_key
            assert "durationSeconds" in caps.request_fields, caps.model_key


def test_no_model_declares_a_duration_it_cannot_produce():
    for caps in CATALOG.values():
        if caps.durations.kind == "discrete":
            assert caps.durations.values, caps.model_key
            for v in caps.durations.values:
                assert caps.durations.resolve(v) == v, caps.model_key


def test_duration_resolution_always_rounds_up():
    """Rounding down would silently cut the end off a narrated line."""
    caps = get_caps("kling-2.5-turbo-i2v")          # 5s / 10s
    assert caps.durations.resolve(4.0) == 5.0
    assert caps.durations.resolve(5.0) == 5.0
    assert caps.durations.resolve(5.1) == 10.0
    # Past the longest available there is nowhere to round to; the longest is
    # the honest answer, and planning warns about the shortfall separately.
    assert caps.durations.resolve(30.0) == 10.0


def test_the_default_model_is_active_and_selectable():
    caps = get_caps(DEFAULT_MODEL_KEY)
    assert caps.status is ModelStatus.ACTIVE
    assert caps.model_key in {c.model_key for c in selectable_models()}


def test_experimental_models_are_not_offered_by_default():
    """Their request shape has never been verified against the live API, so
    the first call would be the test -- and a paid one."""
    keys = {c.model_key for c in selectable_models()}
    assert "wan-2.5-i2v" not in keys
    assert "wan-2.5-i2v" in {
        c.model_key for c in selectable_models(allow_experimental=True)}


def test_premium_is_opt_in():
    """Two independent gates, and Veo currently trips both: it is premium AND
    its request shape has never been exercised against the live API."""
    assert "veo-3.1-standard-i2v" not in {c.model_key for c in selectable_models()}
    assert "veo-3.1-standard-i2v" not in {
        c.model_key for c in selectable_models(allow_premium=True)}
    assert "veo-3.1-standard-i2v" in {
        c.model_key for c in selectable_models(allow_premium=True,
                                               allow_experimental=True)}


def test_cheapest_capable_is_actually_the_cheapest():
    key = cheapest_capable()
    options = selectable_models()
    cheapest = min(o.pricing.cents(o.durations.resolve(6.0), o.resolutions[0])
                   for o in options)
    caps = get_caps(key)
    assert caps.pricing.cents(caps.durations.resolve(6.0),
                              caps.resolutions[0]) == cheapest


# ---- duration planning across a whole film --------------------------------
def _film(n: int, words: int, intent: float = 6.0):
    return [(f"s{i}", intent, words) for i in range(n)]


def test_planning_lands_near_the_requested_runtime():
    """The point of planning the film rather than each shot: every provider
    rounds a request UP, so shot-by-shot snapping overshoots by the rounding
    error times the shot count."""
    for key in ("kling-2.5-turbo-i2v", "hailuo-02-standard-i2v",
                "veo-3.1-fast-i2v"):
        caps = get_caps(key)
        plan = plan_film_durations(_film(15, words=10), caps, 90.0)
        assert abs(plan.drift_s) <= 10, (key, plan.summary())


def test_planning_never_overshoots_the_target_by_upgrading():
    caps = get_caps("kling-2.5-turbo-i2v")
    plan = plan_film_durations(_film(10, words=8), caps, 60.0)
    assert plan.planned_total_s <= 60.0


def test_every_shot_can_hold_its_own_narration():
    """A clip shorter than the words spoken over it would be a freeze, and
    audio is never compressed to fit a picture."""
    from app.ai.pacing import required_seconds
    caps = get_caps("kling-2.5-turbo-i2v")
    plan = plan_film_durations(_film(8, words=20), caps, 90.0)
    for a in plan.allocations:
        assert a.resolved_s >= required_seconds(20) - 0.01


def test_a_film_whose_floor_exceeds_the_target_is_reported_not_truncated():
    """Twenty shots of long narration cannot be squeezed into 30s. The honest
    answer is a number the operator can act on."""
    caps = get_caps("kling-2.5-turbo-i2v")
    plan = plan_film_durations(_film(20, words=20), caps, 30.0)
    assert plan.planned_total_s > 30.0
    assert plan.floor_total_s > 30.0
    assert plan.drift_s > 0


def test_allocation_covers_every_shot():
    caps = get_caps("kling-2.5-turbo-i2v")
    plan = plan_film_durations(_film(14, words=10), caps, 90.0)
    assert len(plan.allocations) == 14
    assert all(a.resolved_s in caps.durations.values for a in plan.allocations)


# ---- the motion prompt ----------------------------------------------------
def test_motion_prompt_leads_with_what_the_subject_does():
    m = compose_motion_prompt(subject_motion="She turns her head to the door",
                              environment_motion="Curtains lift in the draught",
                              camera_move="push_in", motion_pacing="slow")
    assert m.positive.startswith("She turns her head to the door")
    origins = [o for o, _ in m.fragments]
    assert origins[:2] == ["subject", "environment"]


def test_motion_prompt_anchors_the_composition():
    """The image-to-video model already has the frame. The instruction that
    matters most is 'do not reinvent it' -- that is what stops a character
    changing coat halfway through the film."""
    m = compose_motion_prompt(subject_motion="She blinks")
    assert "first frame" in m.positive
    assert any(o == "anchor" for o, _ in m.fragments)


def test_motion_prompt_does_not_restate_the_composition():
    """Describing the scene again invites reinterpretation, so the composer
    is given no style bible, canon or location to leak."""
    m = compose_motion_prompt(subject_motion="She blinks",
                              motion_language="Gentle camera work.")
    for leaked in ("gouache", "palette", "close_up", "shot"):
        assert leaked not in m.positive.lower()


def test_camera_move_becomes_a_sentence_not_a_token():
    m = compose_motion_prompt(subject_motion="x", camera_move="tilt_up")
    assert "tilts slowly upward" in m.positive
    assert "tilt_up" not in m.positive


def test_pacing_is_directed_rather_than_left_to_the_model():
    still = compose_motion_prompt(subject_motion="x", motion_pacing="still")
    brisk = compose_motion_prompt(subject_motion="x", motion_pacing="brisk")
    assert "nearly a photograph" in still.positive
    assert "quick and urgent" in brisk.positive


def test_negative_prompt_targets_the_i2v_failure_modes():
    m = compose_motion_prompt(subject_motion="x")
    assert "static frozen frame" in m.negative
    assert "morphing faces" in m.negative


def test_models_without_a_negative_prompt_get_it_folded_in():
    """A capability difference, never a branch on a provider name."""
    m = compose_motion_prompt(subject_motion="x",
                              supports_negative_prompt=False)
    assert m.negative == ""
    assert "no cuts" in m.positive


def test_override_replaces_the_composed_motion():
    m = compose_motion_prompt(subject_motion="ignored",
                              motion_override="Only this happens")
    assert m.positive.startswith("Only this happens")
    assert "ignored" not in m.positive


def test_hash_changes_when_the_keyframe_changes():
    """A clip animated from a still that has since been replaced is stale.
    Without the keyframe in the hash it would report itself as current and
    ship a film whose motion does not match its own frames."""
    m = compose_motion_prompt(subject_motion="She blinks")
    kw = dict(model_key="kling-2.5-turbo-i2v", duration_s=5.0,
              resolution="1080p", seed=None)
    assert m.hash(first_frame_checksum="aaa", **kw) != \
           m.hash(first_frame_checksum="bbb", **kw)
    assert m.hash(first_frame_checksum="aaa", **kw) == \
           m.hash(first_frame_checksum="aaa", **kw)


def test_hash_changes_with_the_model_and_the_duration():
    m = compose_motion_prompt(subject_motion="She blinks")
    base = dict(model_key="kling-2.5-turbo-i2v", duration_s=5.0,
                resolution="1080p", seed=None, first_frame_checksum="a")
    assert m.hash(**{**base, "model_key": "hailuo-02-pro-i2v"}) != m.hash(**base)
    assert m.hash(**{**base, "duration_s": 10.0}) != m.hash(**base)


# ---- continuity: the join between one shot and the next -------------------
def test_hash_changes_when_the_closing_frame_changes():
    """A clip told to end on the next shot's still must go stale when that
    still is replaced -- otherwise the join it was generated for no longer
    exists and the seam reappears in a film that reports itself current."""
    m = compose_motion_prompt(subject_motion="She blinks")
    base = dict(model_key="kling-2.5-turbo-i2v", duration_s=5.0,
                resolution="1080p", seed=None, first_frame_checksum="a")
    assert m.hash(**base, last_frame_checksum="x") != \
           m.hash(**base, last_frame_checksum="y")
    assert m.hash(**base, chain_from_checksum="x") != \
           m.hash(**base, chain_from_checksum="y")


def test_continuity_off_hashes_exactly_as_it_did_before_continuity_existed():
    """The regression this guards is expensive, not cosmetic. If the new
    inputs were hashed as "" instead of omitted, every clip in every existing
    project would go stale the moment this code shipped -- and a clip is
    roughly fourteen times a still to regenerate."""
    m = compose_motion_prompt(subject_motion="She blinks")
    base = dict(model_key="kling-2.5-turbo-i2v", duration_s=5.0,
                resolution="1080p", seed=None, first_frame_checksum="a")
    assert m.hash(**base) == m.hash(**base, last_frame_checksum="",
                                    chain_from_checksum="")


def test_last_frame_support_is_declared_once_not_twice():
    """`supports_last_frame` is derived from the wire field rather than
    stored beside it, so the two can never disagree."""
    for caps in CATALOG.values():
        assert caps.supports_last_frame == (caps.last_frame_field is not None)
        if caps.supports_last_frame:
            assert "last frame" in caps.capability_chips()


def test_at_least_one_model_can_be_told_where_to_end():
    """Without one, the seamless-join path is unreachable and this whole
    mechanism is dead code."""
    assert any(c.supports_last_frame for c in CATALOG.values())


# ---- the adapter sends only what the endpoint declares --------------------
async def test_the_payload_is_filtered_to_declared_fields():
    """Kling declares no seed, resolution or aspect_ratio input. Sending them
    is a 422, and the bake-off paid to learn that."""
    import app.ai.adapters.fal_video as fv

    sent: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"request_id": "r1", "status_url": "s", "response_url": "r"}

    class FakeClient:
        async def post(self, url, json):
            sent.update(json)
            return FakeResponse()

    adapter = object.__new__(fv.FalVideoAdapter)
    adapter._client = FakeClient()
    caps = get_caps("kling-2.5-turbo-i2v")
    await adapter.submit(VideoRequest(
        model_key=caps.model_key, model_id=caps.model_id,
        first_frame=b"\x89PNG", first_frame_mime="image/png",
        prompt="p", negative_prompt="n", reference_images=[],
        duration_s=5.0, resolution="1080p", aspect_ratio="16:9", seed=42))

    assert set(sent) <= set(caps.request_fields) | set(caps.extra_params)
    assert "seed" not in sent and "resolution" not in sent
    assert "aspect_ratio" not in sent
    assert sent["duration"] == "5"
    assert sent["cfg_scale"] == 0.5           # the model's fixed extra param
    assert sent["image_url"].startswith("data:image/png;base64,")


async def test_a_closing_frame_is_sent_only_when_the_model_declares_one():
    """Kling names no closing-frame field, so passing one must not put it on
    the wire -- an undeclared field is a 422 and a wasted round trip."""
    import app.ai.adapters.fal_video as fv

    sent: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"request_id": "r1", "status_url": "s", "response_url": "r"}

    class FakeClient:
        async def post(self, url, json):
            sent.update(json)
            return FakeResponse()

    adapter = object.__new__(fv.FalVideoAdapter)
    adapter._client = FakeClient()
    caps = get_caps("kling-2.5-turbo-i2v")
    assert not caps.supports_last_frame
    await adapter.submit(VideoRequest(
        model_key=caps.model_key, model_id=caps.model_id,
        first_frame=b"\x89PNG", first_frame_mime="image/png",
        prompt="p", negative_prompt="n", reference_images=[],
        duration_s=5.0, resolution="1080p", aspect_ratio="16:9", seed=42,
        last_frame=b"\x89PNGtail", last_frame_mime="image/png"))
    assert not any("tail" in k or "last" in k.lower() for k in sent)


async def test_declaring_the_field_is_all_it_takes_to_send_it(monkeypatch):
    """Enabling continuity on a new fal model must be one string in the
    catalogue and no code anywhere -- that is the whole design. This proves
    the adapter honours a declaration it has never seen before."""
    import dataclasses

    import app.ai.adapters.fal_video as fv

    sent: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"request_id": "r1", "status_url": "s", "response_url": "r"}

    class FakeClient:
        async def post(self, url, json):
            sent.update(json)
            return FakeResponse()

    base = get_caps("kling-2.5-turbo-i2v")
    declared = dataclasses.replace(base, last_frame_field="tail_image_url")
    monkeypatch.setattr(fv, "get_caps", lambda key: declared)

    adapter = object.__new__(fv.FalVideoAdapter)
    adapter._client = FakeClient()
    await adapter.submit(VideoRequest(
        model_key=base.model_key, model_id=base.model_id,
        first_frame=b"\x89PNG", first_frame_mime="image/png",
        prompt="p", negative_prompt="n", reference_images=[],
        duration_s=5.0, resolution="1080p", aspect_ratio="16:9", seed=42,
        last_frame=b"\x89PNGtail", last_frame_mime="image/png"))
    assert sent["tail_image_url"].startswith("data:image/png;base64,")


def test_veo_puts_the_closing_frame_beside_the_opening_one():
    """Veo's closing frame is an *instance* field, a sibling of `image`, not a
    generation parameter. Filtering it through `request_fields` -- which is the
    parameters allowlist -- would drop it silently and produce a normal-looking
    clip that simply does not join."""
    import app.ai.adapters.veo_video as vv

    caps = get_caps("veo-3.1-fast-i2v")
    assert caps.supports_last_frame
    adapter = object.__new__(vv.VeoVideoAdapter)
    adapter._person_generation = "allow_adult"
    payload = adapter.build_payload(VideoRequest(
        model_key=caps.model_key, model_id=caps.model_id,
        first_frame=b"\x89PNGhead", first_frame_mime="image/png",
        prompt="p", negative_prompt=None, reference_images=[],
        duration_s=8.0, resolution="1080p", aspect_ratio="16:9", seed=7,
        last_frame=b"\x89PNGtail", last_frame_mime="image/png"))
    instance = payload["instances"][0]
    assert "lastFrame" in instance
    assert instance["lastFrame"] != instance["image"]
    assert "lastFrame" not in payload["parameters"]


def test_veo_omits_the_closing_frame_when_there_is_none():
    """Most shots have no join to make -- the last shot of the film, or a
    project with continuity off. Sending an empty key would be a 400."""
    import app.ai.adapters.veo_video as vv

    caps = get_caps("veo-3.1-fast-i2v")
    adapter = object.__new__(vv.VeoVideoAdapter)
    adapter._person_generation = "allow_adult"
    payload = adapter.build_payload(VideoRequest(
        model_key=caps.model_key, model_id=caps.model_id,
        first_frame=b"\x89PNGhead", first_frame_mime="image/png",
        prompt="p", negative_prompt=None, reference_images=[],
        duration_s=8.0, resolution="1080p", aspect_ratio="16:9", seed=7))
    assert "lastFrame" not in payload["instances"][0]


# ---- validation: measure what arrived, never trust the request ------------
@needs_ffmpeg
async def test_a_real_clip_validates_and_reports_measured_truth():
    adapter = FakeVideoAdapter()
    req = VideoRequest(
        model_key="kling-2.5-turbo-i2v", model_id="x", first_frame=b"\x89PNG",
        first_frame_mime="image/png", prompt="p", negative_prompt=None,
        reference_images=[], duration_s=5.0, resolution="1080p",
        aspect_ratio="16:9", seed=None)
    sub = await adapter.submit(req)
    state = await adapter.poll(sub)
    result = await adapter.fetch(state)
    measured = validate_clip(result.data, 5.0)
    assert abs(measured["duration_ms"] - 5000) <= DURATION_TOLERANCE_MS
    assert measured["width"] and measured["height"]
    assert measured["fps"]


@needs_ffmpeg
async def test_a_clips_closing_frame_can_be_read_back(tmp_path):
    """The mechanism a chained join is built on. If this cannot produce a
    readable image, every chained shot fails at submit time."""
    from app.render.ffmpeg import extract_tail_frame, probe

    adapter = FakeVideoAdapter()
    sub = await adapter.submit(VideoRequest(
        model_key="k", model_id="x", first_frame=b"",
        first_frame_mime="image/png", prompt="p", negative_prompt=None,
        reference_images=[], duration_s=3.0, resolution="1080p",
        aspect_ratio="16:9", seed=None))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes((await adapter.fetch(await adapter.poll(sub))).data)

    frame = extract_tail_frame(clip, tmp_path / "tail.png")
    assert frame.exists() and frame.stat().st_size > 0
    # PNG, not JPEG: the frame is re-encoded twice more downstream, and
    # starting that chain lossy puts a visible step exactly at the seam.
    assert frame.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    info = probe(frame)
    assert info.ok and info.width and info.height


def test_reading_a_closing_frame_from_a_non_clip_fails_loudly(tmp_path):
    """Never silently: a chain that falls back to the approved still puts a
    visible cut in a film the user paid to have joined."""
    from app.render.ffmpeg import FFmpegError, extract_tail_frame

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00" * 4096)
    with pytest.raises(FFmpegError):
        extract_tail_frame(junk, tmp_path / "tail.png")

    with pytest.raises(FFmpegError, match="missing or empty"):
        extract_tail_frame(tmp_path / "absent.mp4", tmp_path / "tail.png")


def test_a_truncated_download_is_rejected():
    with pytest.raises(ClipInvalid, match="too small"):
        validate_clip(b"not a video", 5.0)


def test_an_unreadable_file_is_rejected():
    with pytest.raises(ClipInvalid):
        validate_clip(b"\x00" * 50_000, 5.0)


@needs_ffmpeg
async def test_a_clip_of_the_wrong_length_is_rejected():
    """Drift beyond the tolerance would desync the narration under it."""
    adapter = FakeVideoAdapter()
    sub = await adapter.submit(VideoRequest(
        model_key="k", model_id="x", first_frame=b"", first_frame_mime="image/png",
        prompt="p", negative_prompt=None, reference_images=[], duration_s=2.0,
        resolution="1080p", aspect_ratio="16:9", seed=None))
    result = await adapter.fetch(await adapter.poll(sub))
    with pytest.raises(ClipInvalid, match="was requested"):
        validate_clip(result.data, 10.0)


@needs_ffmpeg
async def test_measured_drift_within_tolerance_is_accepted():
    """The bake-off saw +42ms and -125ms from real providers. The tolerance
    has to admit that or every real clip would be rejected."""
    adapter = FakeVideoAdapter()
    sub = await adapter.submit(VideoRequest(
        model_key="k", model_id="x", first_frame=b"", first_frame_mime="image/png",
        prompt="p", negative_prompt=None, reference_images=[], duration_s=5.0,
        resolution="1080p", aspect_ratio="16:9", seed=None))
    result = await adapter.fetch(await adapter.poll(sub))
    assert validate_clip(result.data, 5.4)["duration_ms"] == 5000


# ---- the polling contract -------------------------------------------------
async def test_polling_reports_progress_before_completing():
    adapter = FakeVideoAdapter(ticks_pending=2)
    sub = await adapter.submit(VideoRequest(
        model_key="k", model_id="x", first_frame=b"", first_frame_mime="image/png",
        prompt="p", negative_prompt=None, reference_images=[], duration_s=5.0,
        resolution="1080p", aspect_ratio="16:9", seed=None))
    first = await adapter.poll(sub)
    assert first.done is False and first.progress_hint
    await adapter.poll(sub)
    assert (await adapter.poll(sub)).done is True


# ---- freshness ------------------------------------------------------------
def _shot(motion_hash=None):
    s = Shot()
    s.motion_input_hash = motion_hash
    return s


def _clip(source, input_hash):
    a = Asset()
    a.source = source
    a.input_hash = input_hash
    return a


def test_a_shot_with_no_clip_is_not_fresh():
    assert clip_is_fresh(_shot("a"), None) is False


def test_a_matching_hash_is_fresh():
    assert clip_is_fresh(_shot("a"), _clip(AssetSource.GENERATED, "a")) is True


def test_changing_the_motion_plan_makes_the_clip_stale():
    assert clip_is_fresh(_shot("b"), _clip(AssetSource.GENERATED, "a")) is False


def test_an_uploaded_clip_is_permanently_fresh():
    """It was not produced by a prompt, so no prompt change can invalidate it,
    and it must never be silently regenerated over."""
    assert clip_is_fresh(_shot("b"), _clip(AssetSource.MANUAL, "a")) is True


# --------------------------------------------------------------------------- #
# End to end, against the real database and the real job handlers.
#
# This is the test the whole milestone exists for: a shot with an approved
# still becomes a generated CLIP in the timeline, and the renderer is handed a
# video file rather than a photograph with a Ken Burns move over it.
# --------------------------------------------------------------------------- #
from httpx import ASGITransport, AsyncClient                # noqa: E402

from app.auth import create_user                            # noqa: E402
from app.db import models as jobs_model                     # noqa: E402
from app.db.models import AssetKind, Project, User          # noqa: E402
from app.db.session import (dispose_engine, get_engine,     # noqa: E402
                            get_sessionmaker)
from app.jobs import get_queue, reset_queue                 # noqa: E402
from app.main import create_app                             # noqa: E402
from app.render.timeline import SourceKind                  # noqa: E402
from app.services.timeline_builder import build_timeline    # noqa: E402

EMAIL, PASS = "motion@local", "motion-pass"
STORY = ("A keeper kept a light for forty years. One winter night the power "
         "failed and she turned the lens by hand until dawn.")


@pytest.fixture
async def client(tmp_path):
    os.environ["STORAGE_DIR"] = str(tmp_path)
    from app.config import get_settings
    from app.storage import reset_storage
    get_settings.cache_clear(); reset_storage(); reset_queue()
    async with get_sessionmaker()() as s:
        user = (await s.execute(
            select(User).where(User.email == EMAIL))).scalar_one_or_none()
        if user is None:
            user = await create_user(s, email=EMAIL, display_name="M",
                                     passphrase=PASS)
        user.is_active = True
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=create_app()),
                           base_url="http://test") as c:
        await c.post("/api/v1/auth/session",
                     json={"email": EMAIL, "passphrase": PASS})
        yield c


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with get_sessionmaker()() as s:
        for t in ("job_events", "jobs", "ai_calls", "assets", "projects"):
            await s.execute(text(f"DELETE FROM {t}"))
        await s.commit()
    await dispose_engine(); get_engine.cache_clear(); get_sessionmaker.cache_clear()
    reset_queue()


async def _project_with_stills(client):
    pid = (await client.post("/api/v1/projects",
                             json={"title": "motion"})).json()["id"]
    await client.put(f"/api/v1/projects/{pid}/story", json={"raw_text": STORY})
    await client.post(f"/api/v1/projects/{pid}/story:analyze")
    await get_queue().drain()
    await client.post(f"/api/v1/projects/{pid}/storyboard:generate", json={})
    await get_queue().drain()
    sb = (await client.get(
        f"/api/v1/projects/{pid}/storyboards")).json()["items"][0]["id"]
    await client.post(f"/api/v1/projects/{pid}/storyboards/{sb}:apply", json={})
    shots = (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]
    # An approved still is the precondition for motion -- it is the clip's
    # first frame -- and the timeline refuses to build while any shot lacks
    # one, so every shot needs it, not just the one under test.
    for shot in shots:
        await client.post(f"/api/v1/shots/{shot['id']}/image:generate",
                          json={"n": 1})
    await get_queue().drain()
    return pid, shots


async def _unanimated(client, pid):
    """A project where exactly one shot could be animated but none is."""
    shots = (await client.get(f"/api/v1/projects/{pid}/shots")).json()["items"]
    return shots


async def test_the_catalogue_is_served_to_the_client(client):
    body = (await client.get("/api/v1/video-models")).json()
    assert body["default"] == DEFAULT_MODEL_KEY
    assert any(m["is_default"] for m in body["items"])
    # Capability chips exist so a picker can explain a model without the UI
    # knowing anything about providers.
    assert all(m["capability_chips"] for m in body["items"])


async def test_planning_refuses_a_shot_with_no_approved_still(client):
    """The keyframe is the clip's first frame and its only consistency
    anchor. Without one there is nothing to animate."""
    pid, shots = await _project_with_stills(client)
    async with get_sessionmaker()() as s:
        shot = await s.get(Shot, uuid.UUID(shots[1]["id"]))
        shot.selected_image_id = None
        await s.commit()
    plan = (await client.post(f"/api/v1/shots/{shots[1]['id']}/motion:plan",
                              json={})).json()
    assert plan["ok"] is False
    assert any(b["code"] == "no_approved_still" for b in plan["blocking"])


async def test_planning_prices_a_ready_shot_without_spending(client):
    pid, shots = await _project_with_stills(client)
    plan = (await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:plan",
                              json={"model_key": "kling-2.5-turbo-i2v"})).json()
    assert plan["ok"] is True
    assert plan["resolved_duration_s"] in (5.0, 10.0)
    assert plan["estimated_cost_cents"] > 0
    assert "first frame" in plan["prompt"]
    async with get_sessionmaker()() as s:
        project = await s.get(Project, uuid.UUID(pid))
        assert float(project.spent_cents) == 0        # planning is free


@needs_ffmpeg
async def test_a_shot_becomes_a_real_clip_and_lands_in_the_timeline(client):
    """The whole point. Before this, every shot in the timeline was a STILL."""
    pid, shots = await _project_with_stills(client)
    r = await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate",
                          json={"model_key": "kling-2.5-turbo-i2v"})
    assert r.status_code == 202
    await get_queue().drain()

    async with get_sessionmaker()() as s:
        clip = (await s.execute(
            select(Asset).where(Asset.kind == AssetKind.CLIP))).scalar_one()
        # Measured with ffprobe, never the duration we asked for.
        assert clip.duration_ms and clip.width and clip.height
        assert clip.provider == "fal"

        shot = await s.get(Shot, uuid.UUID(shots[0]["id"]))
        assert shot.selected_clip_id == clip.id
        assert shot.motion_mode is MotionMode.GENERATED

        project = await s.get(Project, uuid.UUID(pid))
        result = await build_timeline(s, project)
        animated = [c for c in result.timeline.clips
                    if c.source.kind is SourceKind.CLIP]
        assert len(animated) == 1, "the generated clip did not reach the timeline"
        assert animated[0].source.native_duration_ms == clip.duration_ms
        assert animated[0].kenburns is None, "an animated shot must not be panned"


@needs_ffmpeg
async def test_identical_inputs_reuse_the_paid_clip(client):
    """A clip is ~14x a still. Paying twice for byte-identical inputs is the
    most expensive mistake this pipeline could make quietly."""
    pid, shots = await _project_with_stills(client)
    body = {"model_key": "kling-2.5-turbo-i2v"}
    await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate", json=body)
    await get_queue().drain()
    async with get_sessionmaker()() as s:
        spent_after_first = float(
            (await s.get(Project, uuid.UUID(pid))).spent_cents)

    await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate", json=body)
    await get_queue().drain()
    async with get_sessionmaker()() as s:
        clips = (await s.execute(
            select(Asset).where(Asset.kind == AssetKind.CLIP))).scalars().all()
        assert len(clips) == 1, "an identical generation was paid for twice"
        assert float((await s.get(Project, uuid.UUID(pid))).spent_cents) \
            == spent_after_first


async def test_generation_is_refused_over_budget(client):
    pid, shots = await _project_with_stills(client)
    async with get_sessionmaker()() as s:
        project = await s.get(Project, uuid.UUID(pid))
        project.budget_cents = 1          # a clip costs far more than this
        await s.commit()
    r = await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate",
                          json={"model_key": "kling-2.5-turbo-i2v"})
    assert r.status_code == 402
    async with get_sessionmaker()() as s:
        assert (await s.execute(select(Asset).where(
            Asset.kind == AssetKind.CLIP))).scalars().all() == []


@needs_ffmpeg
async def test_a_film_without_motion_still_renders_as_stills(client):
    """Motion is per-shot and optional. Removing that would turn a free
    preview into a paid one."""
    pid, shots = await _project_with_stills(client)
    async with get_sessionmaker()() as s:
        project = await s.get(Project, uuid.UUID(pid))
        result = await build_timeline(s, project)
        assert result.timeline is None or all(
            c.source.kind is SourceKind.STILL for c in result.timeline.clips)


@needs_ffmpeg
async def test_require_motion_refuses_to_degrade_into_a_slideshow(client):
    """A shot whose generation failed must be visible as a failure, not
    silently swapped for a still and called finished."""
    pid, shots = await _project_with_stills(client)
    await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate",
                      json={"model_key": "kling-2.5-turbo-i2v"})
    await get_queue().drain()
    async with get_sessionmaker()() as s:
        project = await s.get(Project, uuid.UUID(pid))
        result = await build_timeline(s, project, require_motion=True)
        assert result.timeline is None
        assert any(p.code == "no_motion" for p in result.blocking)
        # And it says which shots, so the failure is actionable.
        assert all(p.shot_id for p in result.blocking if p.code == "no_motion")


@needs_ffmpeg
async def test_changing_the_motion_plan_makes_the_clip_stale_in_the_timeline(client):
    """A clip animated from inputs that no longer apply must not ship."""
    pid, shots = await _project_with_stills(client)
    await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate",
                      json={"model_key": "kling-2.5-turbo-i2v"})
    await get_queue().drain()

    async with get_sessionmaker()() as s:
        shot = await s.get(Shot, uuid.UUID(shots[0]["id"]))
        shot.motion_input_hash = "something-else-entirely"
        await s.commit()
        project = await s.get(Project, uuid.UUID(pid))
        result = await build_timeline(s, project)
        assert all(c.source.kind is SourceKind.STILL
                   for c in result.timeline.clips)
        assert any(p.code == "stale_clip" for p in result.advisory)


async def test_each_poll_tick_is_a_distinct_delivery_to_the_broker(client):
    """A poll that re-enqueues itself under a constant key is accepted by us
    and dropped by arq, which de-duplicates on (kind, job id, attempt). The
    clip would then be generated, paid for, and never collected.

    The inline queue used everywhere else in this file ignores `attempt`
    entirely, so nothing else here can catch this.
    """
    from app.jobs.handlers import motion as motion_handlers

    pid, shots = await _project_with_stills(client)
    seen: list[int] = []

    class RecordingQueue:
        async def enqueue(self, kind, job_id, defer_s=0.0, attempt=0):
            if kind == "motion.poll":
                seen.append(attempt)

        async def close(self):
            return None

    # Two pending ticks, so the re-enqueue path runs more than once.
    import app.ai.registry as registry
    registry.get_video_port.cache_clear()
    port = FakeVideoAdapter(ticks_pending=3)
    registry.get_video_port.__wrapped__.__globals__  # keep the import honest

    async def fake_port(adapter="fal"):
        return port

    from unittest.mock import patch
    with patch.object(motion_handlers, "get_video_port", lambda a="fal": port), \
         patch("app.jobs.get_queue", lambda: RecordingQueue()):
        await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate",
                          json={"model_key": "kling-2.5-turbo-i2v"})
        await get_queue().drain()
        async with get_sessionmaker()() as s:
            job = (await s.execute(select(jobs_model.Job).where(
                jobs_model.Job.kind == "motion.submit"))).scalars().first()
        for _ in range(3):
            await motion_handlers.poll_motion_job(job.id)

    assert len(seen) >= 2, "the poll never re-enqueued itself"
    assert len(set(seen)) == len(seen), (
        f"poll ticks reused a broker key: {seen}. arq would drop every tick "
        f"after the first and the paid clip would never be collected.")


# --------------------------------------------------------------------------- #
# Veo. This adapter has NEVER made a successful call (ADR-001: "0 calls ... its
# adapter has never executed"), so everything asserted here is structural: the
# request it would build, not a response it has seen. That distinction is the
# point -- these tests stop the payload regressing, they do not claim the
# endpoint accepts it.
# --------------------------------------------------------------------------- #
def _veo_adapter():
    from app.ai.adapters.veo_video import VeoVideoAdapter
    a = object.__new__(VeoVideoAdapter)
    a._key = "AIza" + "x" * 35
    a._person_generation = "allow_adult"
    return a


def _veo_request(model_key="veo-3.1-fast-i2v", **kw):
    caps = get_caps(model_key)
    defaults = dict(
        model_key=caps.model_key, model_id=caps.model_id,
        first_frame=b"\x89PNG", first_frame_mime="image/png",
        prompt="she turns toward the door", negative_prompt=None,
        reference_images=[], duration_s=6.0, resolution="720p",
        aspect_ratio="16:9", seed=None)
    return VideoRequest(**{**defaults, **kw})


def test_veo_payload_uses_the_documented_predict_shape():
    body = _veo_adapter().build_payload(_veo_request())
    assert set(body) == {"instances", "parameters"}
    assert len(body["instances"]) == 1
    inst = body["instances"][0]
    assert inst["prompt"] == "she turns toward the door"
    # The keyframe travels inline, base64, with its mime -- Veo takes no URL.
    assert inst["image"]["mimeType"] == "image/png"
    assert inst["image"]["bytesBase64Encoded"]


def test_veo_payload_is_filtered_through_the_catalogue_allowlist():
    """The gap this closes: the bake-off's Veo adapter built its parameters
    dict unconditionally and consulted `caps` only for retention_hours. The
    per-model allowlist -- introduced after Kling and Hailuo rejected fields
    they do not declare -- was applied to the fal adapter only, so the same
    class of 400 was still live on Veo."""
    caps = get_caps("veo-3.1-fast-i2v")
    body = _veo_adapter().build_payload(_veo_request(seed=42))
    assert set(body["parameters"]) <= set(caps.request_fields) | set(caps.extra_params)


def test_veo_allowlist_is_in_veos_own_vocabulary():
    """Veo's parameters are camelCase; the generic fallback is fal's
    snake_case. An unfiltered fallback here would have matched nothing at all,
    which is a silent no-op rather than a visible error."""
    from app.ai.catalog import GENERIC_REQUEST_FIELDS
    for key in ("veo-3.1-fast-i2v", "veo-3.1-standard-i2v"):
        caps = get_caps(key)
        assert caps.request_fields != GENERIC_REQUEST_FIELDS, key
        assert "durationSeconds" in caps.request_fields, key
        assert "duration" not in caps.request_fields, key


def test_veo_truncates_reference_images_to_the_declared_limit():
    """Veo is the only model here that takes references at all. Sending more
    than it accepts is a 400, and silently sending fewer is the honest
    behaviour planning already warns about."""
    caps = get_caps("veo-3.1-fast-i2v")
    body = _veo_adapter().build_payload(
        _veo_request(reference_images=[b"a", b"b", b"c", b"d", b"e"]))
    assert len(body["parameters"]["referenceImages"]) == caps.max_reference_images


def test_veo_models_are_experimental_until_a_call_succeeds():
    """ACTIVE means the request shape is verified against the live API. Nothing
    in this adapter has ever run, so ACTIVE would be a claim the project cannot
    support -- and `plan_motion` must refuse to spend on it by default."""
    for key in ("veo-3.1-fast-i2v", "veo-3.1-standard-i2v"):
        assert get_caps(key).status is ModelStatus.EXPERIMENTAL, key
    assert "veo-3.1-fast-i2v" not in {c.model_key for c in selectable_models(
        allow_premium=True)}


def test_an_ephemeral_token_is_named_as_such_not_reported_as_a_server_error():
    """An AQ.… token authenticates for minutes and then 401s on everything,
    which reads as a broken integration rather than an expired credential."""
    from app.ai.adapters.veo_video import VeoVideoAdapter
    from app.ai.ports import AIError
    with pytest.raises(AIError) as err:
        VeoVideoAdapter("AQ." + "x" * 50)
    assert err.value.code == "not_an_api_key"
    assert "expire" in err.value.detail.lower()


def test_veo_classifies_a_policy_refusal_separately_from_a_bad_request():
    """A person-generation refusal must never be retried as-is; a malformed
    request is a catalogue bug. Both are HTTP 400."""
    from app.ai.adapters.veo_video import _classify
    from app.ai.ports import AIErrorKind
    assert _classify(400, '{"message":"blocked by safety policy"}').kind \
        is AIErrorKind.REFUSAL
    assert _classify(400, '{"message":"invalid field"}').kind \
        is AIErrorKind.INVALID
    assert _classify(429, "").kind is AIErrorKind.QUOTA
    assert _classify(404, "").code == "model_not_found"


def test_veo_operation_errors_are_read_as_grpc_not_http():
    """A failed long-running operation reports a google.rpc.Status, whose code
    is a gRPC code. The bake-off adapter fed it to the HTTP classifier, which
    turned every operation failure into `unknown:http_N` -- and an UNKNOWN is
    not retryable, so a RESOURCE_EXHAUSTED blip would permanently fail a
    generation that merely needed to wait."""
    from app.ai.adapters.veo_video import _classify_operation_error
    from app.ai.ports import AIErrorKind

    quota = _classify_operation_error({"code": 8, "message": "quota"})
    assert quota.kind is AIErrorKind.QUOTA
    assert quota.retryable, "a quota error must back off, not fail the shot"

    assert _classify_operation_error({"code": 7}).kind is AIErrorKind.AUTH
    assert _classify_operation_error({"code": 16}).kind is AIErrorKind.AUTH
    assert _classify_operation_error({"code": 3}).kind is AIErrorKind.INVALID
    assert _classify_operation_error({"code": 14}).kind is AIErrorKind.TRANSIENT

    # Safety refusals arrive as prose rather than a distinct code, and must
    # never be retried as-is.
    refusal = _classify_operation_error({"message": "blocked by safety policy"})
    assert refusal.kind is AIErrorKind.REFUSAL
    assert not refusal.retryable


# --------------------------------------------------------------------------- #
# Continuity, end to end.
#
# The failure these guard against is not a crash. It is a film that renders
# perfectly and simply cuts where the user asked it to flow -- which is
# invisible in every assertion except the ones below.
# --------------------------------------------------------------------------- #
async def _plan(client, shot_id, **body):
    return (await client.post(f"/api/v1/shots/{shot_id}/motion:plan",
                              json=body)).json()


async def _set_continuity(client, pid, mode):
    r = await client.patch(f"/api/v1/projects/{pid}",
                           json={"motion_continuity": mode})
    assert r.status_code == 200, r.text
    assert r.json()["motion_continuity"] == mode


async def test_continuity_is_off_unless_asked_for(client):
    """The default must stay NONE. Turning joins on for every existing project
    would change films people have already approved."""
    pid, shots = await _project_with_stills(client)
    body = (await client.get(f"/api/v1/projects/{pid}")).json()
    assert body["motion_continuity"] == "none"
    plan = await _plan(client, shots[0]["id"], model_key="kling-2.5-turbo-i2v")
    assert plan["continuity"] == "none"


async def test_a_last_frame_join_pins_the_next_shots_still(client):
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "last_frame")
    plan = await _plan(client, shots[0]["id"],
                       model_key="veo-3.1-fast-i2v", allow_experimental=True)
    assert plan["continuity"] == "last_frame"
    assert plan["ok"] is True


async def test_the_last_shot_in_the_film_has_nothing_to_join_to(client):
    """And says so by reporting 'none' rather than warning. A warning nobody
    can act on is noise that trains people to ignore the real ones."""
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "last_frame")
    plan = await _plan(client, shots[-1]["id"],
                       model_key="veo-3.1-fast-i2v", allow_experimental=True)
    assert plan["continuity"] == "none"
    assert not any(w["code"].startswith("continuity_next")
                   for w in plan["warnings"])


async def test_a_model_that_cannot_end_where_told_says_so(client):
    """Silence here would produce a normal-looking, fully-paid-for clip that
    simply does not join -- discoverable only by watching the film."""
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "last_frame")
    plan = await _plan(client, shots[0]["id"], model_key="kling-2.5-turbo-i2v")
    assert plan["continuity"] == "none"
    assert any(w["code"] == "continuity_unsupported" for w in plan["warnings"])


async def test_auto_picks_the_join_the_model_can_actually_make(client):
    """'auto' is what people mean by "make it continuous". On a model with a
    closing-frame input that is LAST_FRAME; on one without, chaining is the
    only mechanism left."""
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "auto")
    veo = await _plan(client, shots[0]["id"],
                      model_key="veo-3.1-fast-i2v", allow_experimental=True)
    assert veo["continuity"] == "last_frame"


async def test_chaining_requires_the_previous_shot_to_exist_first(client):
    """Chained shots are strictly sequential. Generating shot 2 before shot 1
    has to be reported, not silently cut."""
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "chained")
    plan = await _plan(client, shots[1]["id"], model_key="kling-2.5-turbo-i2v")
    assert plan["continuity"] == "none"
    assert any(w["code"] == "continuity_previous_clip_missing"
               for w in plan["warnings"])


async def test_the_first_shot_has_nothing_to_chain_from(client):
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "chained")
    plan = await _plan(client, shots[0]["id"], model_key="kling-2.5-turbo-i2v")
    assert plan["continuity"] == "none"
    assert not any(w["code"].startswith("continuity_previous")
                   for w in plan["warnings"])


@needs_ffmpeg
async def test_a_chained_shot_starts_from_the_previous_clips_closing_frame(client):
    """The end-to-end proof, and the one that matters: once shot 1 has a clip,
    shot 2 is generated from that clip's final frame rather than from its own
    still, so the boundary between them is a repeated image and not a cut."""
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "chained")

    await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate",
                      json={"model_key": "kling-2.5-turbo-i2v"})
    await get_queue().drain()

    plan = await _plan(client, shots[1]["id"], model_key="kling-2.5-turbo-i2v")
    assert plan["continuity"] == "chained"
    assert any(w["code"] == "continuity_chained" for w in plan["warnings"])

    r = await client.post(f"/api/v1/shots/{shots[1]['id']}/motion:generate",
                          json={"model_key": "kling-2.5-turbo-i2v"})
    assert r.status_code == 202
    await get_queue().drain()

    async with get_sessionmaker()() as s:
        shot = await s.get(Shot, uuid.UUID(shots[1]["id"]))
        assert shot.motion_mode is MotionMode.GENERATED
        clip = await s.get(Asset, shot.selected_clip_id)
        # Recorded on the asset so a finished film can be explained months
        # later without re-deriving it from the project's current settings.
        assert clip.params["continuity"] == "chained"


@needs_ffmpeg
async def test_a_chained_clip_goes_stale_when_the_clip_before_it_changes(client):
    """The cascade is the point. A chain whose first link is regenerated no
    longer joins, and every clip downstream of it has to know."""
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "chained")

    await client.post(f"/api/v1/shots/{shots[0]['id']}/motion:generate",
                      json={"model_key": "kling-2.5-turbo-i2v"})
    await get_queue().drain()
    chained = (await _plan(client, shots[1]["id"],
                           model_key="kling-2.5-turbo-i2v"))["input_hash"]

    # Replace the first shot's clip with a different one; the second shot was
    # generated to continue from a frame that no longer exists.
    async with get_sessionmaker()() as s:
        first = await s.get(Shot, uuid.UUID(shots[0]["id"]))
        clip = await s.get(Asset, first.selected_clip_id)
        clip.checksum = "a-different-clip-entirely"
        await s.commit()

    after = (await _plan(client, shots[1]["id"],
                         model_key="kling-2.5-turbo-i2v"))["input_hash"]
    assert after != chained


async def test_a_last_frame_clip_goes_stale_when_the_next_still_changes(client):
    """Same rule from the other end: the clip was generated to land on a
    specific image, and that image has been replaced."""
    pid, shots = await _project_with_stills(client)
    await _set_continuity(client, pid, "last_frame")
    kw = dict(model_key="veo-3.1-fast-i2v", allow_experimental=True)
    before = (await _plan(client, shots[0]["id"], **kw))["input_hash"]

    async with get_sessionmaker()() as s:
        nxt = await s.get(Shot, uuid.UUID(shots[1]["id"]))
        still = await s.get(Asset, nxt.selected_image_id)
        still.checksum = "a-different-still"
        await s.commit()

    assert (await _plan(client, shots[0]["id"], **kw))["input_hash"] != before


async def test_an_unknown_continuity_value_degrades_rather_than_crashes(client):
    """A project written by a newer build, opened by an older one. Failing to
    plan would lock the user out of their own film."""
    pid, shots = await _project_with_stills(client)
    async with get_sessionmaker()() as s:
        project = await s.get(Project, uuid.UUID(pid))
        project.motion_continuity = "some-future-mode"
        await s.commit()
    plan = await _plan(client, shots[0]["id"], model_key="kling-2.5-turbo-i2v")
    assert plan["ok"] is True
    assert plan["continuity"] == "none"
    assert any(w["code"] == "continuity_unknown" for w in plan["warnings"])
