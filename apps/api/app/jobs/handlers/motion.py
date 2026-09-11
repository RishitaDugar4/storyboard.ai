"""Image-to-video generation as background jobs.

Split into submit / poll / download for one reason, and it is the most
important reliability property of the whole motion integration: the provider's
job id is durable in Postgres *before* any polling begins. A worker killed
mid-flight then loses at most one poll tick, never a generation that has
already been paid for.

The three handlers serve every provider in the catalogue. Nothing here names
one -- the adapter normalises submit/poll/fetch, and every constraint comes
from the catalogue via `plan_motion`.
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

from ...ai.ports import AIError, AIErrorKind, Submission, VideoRequest
from ...ai.registry import get_video_port
from ...db.ids import uuid7
from ...db.models import (AICall, Asset, AssetKind, AssetSource,
                          MotionContinuity, MotionMode, Project, ProjectStage,
                          Shot)
from ...db.session import get_sessionmaker
from ...render.ffmpeg import FFmpegError, extract_tail_frame, probe
from ...services.motion_service import cached_clip, plan_motion
from ...services.still_service import (BudgetExceeded, PriceUnknown,
                                       check_budget)
from ...storage import asset_key, get_storage
from .. import service as jobs

log = logging.getLogger("hbz.motion")

#: How long a clip may differ from what was asked for before it is rejected.
#: The bake-off measured real drift of +42ms (Kling) and -125ms (Hailuo), so
#: the tolerance has to admit that; a clip half a second out is a different
#: shot and would desync the narration under it.
DURATION_TOLERANCE_MS = 750

#: Smallest plausible video file. Anything under this is a truncated download
#: or an error page with a video content-type, not a clip.
MIN_CLIP_BYTES = 10_000


def _poll_scale() -> float:
    """Multiplier on every provider wait.

    Read per call rather than at import, so tests can set it without caring
    about import order. Set MOTION_POLL_SCALE=0 to poll immediately: the
    catalogue's real latencies are 79-180s, and an end-to-end test against the
    fake adapter would otherwise spend a minute asleep per shot waiting for a
    provider that has already finished.
    """
    import os
    try:
        return max(0.0, float(os.getenv("MOTION_POLL_SCALE", "1")))
    except ValueError:
        return 1.0


class ChainFrameUnavailable(RuntimeError):
    pass


async def closing_frame_of(clip_key: str) -> bytes:
    """The last frame of a stored clip, as PNG bytes.

    The whole mechanism of a chained join: shot N ends on this image and shot
    N+1 begins on it, so the boundary between them is one repeated frame
    rather than a cut between two separately-imagined pictures.

    Raises rather than returning None on failure. A chain that quietly falls
    back to the approved still would produce a visible cut in a film the user
    asked to be continuous, and -- because the clip is paid for either way --
    they would find out after the money was spent.
    """
    storage = get_storage()
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        src = storage.local_path(clip_key)
        if src is None:
            # Remote storage: pull it down, since ffmpeg needs a real file.
            src = work / "previous.mp4"
            src.write_bytes(await storage.get(clip_key))
        dest = work / "tail.png"
        try:
            extract_tail_frame(src, dest)
        except (FFmpegError, RuntimeError) as exc:
            raise ChainFrameUnavailable(str(exc)) from exc
        return dest.read_bytes()


# --------------------------------------------------------------------------- #
# 1. submit
# --------------------------------------------------------------------------- #
async def submit_motion_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        async with jobs.running(session, job_id) as job:
            if job is None:
                return

            shot = await session.get(Shot, job.target_id)
            project = await session.get(Project, job.project_id)
            if shot is None or project is None:
                await jobs.fail(session, job, "not_found",
                                "the shot or project no longer exists")
                return

            plan = await plan_motion(
                session, shot, project,
                model_key=job.payload.get("model_key"),
                duration_s=job.payload.get("duration_s"),
                allow_experimental=bool(job.payload.get("allow_experimental")))

            # Record what the inputs hash to now, whether or not this run
            # succeeds: freshness is measured against the plan we intended.
            shot.motion_input_hash = plan.input_hash

            if plan.blocking:
                await jobs.fail(session, job, "motion_blocked",
                                " ".join(n.message for n in plan.blocking))
                return
            for note in plan.warnings:
                await jobs.progress(session, job, 5, note.message,
                                    level="warning", data={"code": note.code})

            if existing := await cached_clip(session, project.id, plan.input_hash):
                # A clip is roughly fourteen times a still. Paying twice for
                # byte-identical inputs is the single most expensive mistake
                # this pipeline could make quietly.
                shot.selected_clip_id = existing.id
                shot.motion_mode = MotionMode.GENERATED
                await jobs.succeed(session, job, {
                    "asset_id": str(existing.id), "cached": True,
                    "duration_ms": existing.duration_ms, "cost_cents": 0.0})
                await jobs.notify_entity(project.id, "shot", shot.id, "clip_ready")
                return

            try:
                check_budget(project, plan.estimated_cost_cents,
                             price_known=plan.price_is_known,
                             model=plan.caps.display_name)
            except (BudgetExceeded, PriceUnknown) as exc:
                await jobs.fail(
                    session, job,
                    "budget_exceeded" if isinstance(exc, BudgetExceeded)
                    else "price_unknown", str(exc))
                return

            still = await session.get(Asset, shot.selected_image_id)
            frame = await get_storage().get(still.storage_key)
            frame_mime = still.mime
            frame_origin = still.storage_key.split("/")[-1]

            # A chained shot opens on the previous clip's closing frame
            # instead of its own still. Read it here rather than in planning:
            # planning must stay cheap enough to run on every edit that
            # touches a shot, and this decodes a video.
            if plan.chain_from_clip_key:
                try:
                    frame = await closing_frame_of(plan.chain_from_clip_key)
                except ChainFrameUnavailable as exc:
                    await jobs.fail(
                        session, job, "chain_frame_unreadable",
                        f"This shot is chained to the previous one, but its "
                        f"closing frame could not be read: {exc}. Nothing was "
                        f"submitted or charged. Re-generate the previous shot, "
                        f"or turn continuity off for this project.",
                        retryable=False)
                    return
                frame_mime = "image/png"
                frame_origin = f"tail of {plan.chain_from_clip_key.split('/')[-1]}"

            # The frame the model must land on, when it has such an input.
            last_frame = (await get_storage().get(plan.last_frame_key)
                          if plan.last_frame_key else None)

            log.info("SHOT %s -> KEYFRAME %s (%d bytes%s) continuity=%s%s",
                     str(shot.id)[:8], frame_origin, len(frame),
                     f", {still.width}x{still.height}"
                     if not plan.chain_from_clip_key else "",
                     plan.continuity.value,
                     f" last_frame={plan.last_frame_key.split('/')[-1]}"
                     if plan.last_frame_key else "")

            port = get_video_port(plan.caps.adapter)
            await jobs.progress(
                session, job, 15,
                f"submitting to {plan.caps.display_name} "
                f"({plan.resolved_duration_s:g}s, "
                f"~{plan.estimated_cost_cents / 100:.2f} USD)")
            try:
                sub = await port.submit(VideoRequest(
                    model_key=plan.caps.model_key, model_id=plan.caps.model_id,
                    first_frame=frame, first_frame_mime=frame_mime,
                    prompt=plan.prompt.positive,
                    negative_prompt=plan.prompt.negative or None,
                    reference_images=[],
                    duration_s=plan.resolved_duration_s,
                    resolution=plan.resolved_resolution,
                    aspect_ratio=plan.aspect_ratio, seed=plan.seed,
                    last_frame=last_frame,
                    last_frame_mime=plan.last_frame_mime or None))
            except AIError as exc:
                await jobs.fail(session, job, f"{exc.kind}:{exc.code}",
                                exc.detail, retryable=exc.retryable)
                return

            log.info("I2V SUBMIT       shot=%s model=%s duration=%gs "
                     "est=%.2f USD provider_job=%s",
                     str(shot.id)[:8], plan.caps.model_key,
                     plan.resolved_duration_s,
                     plan.estimated_cost_cents / 100, sub.provider_job_id)

            # Durable before the first poll. Everything after this point can
            # crash and be recovered; before it, nothing has been bought.
            job.payload = {**job.payload,
                           "provider_job_id": sub.provider_job_id,
                           "submission": sub.raw,
                           "endpoint": sub.endpoint,
                           "model_key": plan.caps.model_key,
                           "adapter": plan.caps.adapter,
                           "input_hash": plan.input_hash,
                           "resolved_duration_s": plan.resolved_duration_s,
                           "estimated_cost_cents": plan.estimated_cost_cents,
                           "continuity": plan.continuity.value,
                           "polls": 0}
            job.status = jobs.JobStatus.AWAITING_PROVIDER
            job.message = f"waiting on {plan.caps.display_name}"
            job.progress = 25
            await session.commit()
            async with jobs._publish_job(job):
                pass

            # First poll timed at 60% of the measured typical latency: sooner
            # is wasted calls, later is dead air the user watches.
            #
            # attempt=0 and then the poll count, NOT job.attempt: the broker
            # de-duplicates on (kind, job id, attempt), and a poll that
            # re-enqueues itself under a constant key is accepted here and
            # silently dropped there -- so the clip would be generated, paid
            # for, and then never collected. Each tick must be a new delivery.
            from .. import get_queue
            await get_queue().enqueue(
                "motion.poll", job.id, attempt=0,
                defer_s=max(5.0, plan.caps.typical_latency_s * 0.6)
                * _poll_scale())


# --------------------------------------------------------------------------- #
# 2. poll
# --------------------------------------------------------------------------- #
#: Backoff between ticks. Flat after the third: providers in this catalogue
#: finish in 79-180s, so a long tail of exponential waits only adds latency
#: after the clip is already sitting there.
POLL_BACKOFF_S = [10.0, 20.0, 30.0]


async def poll_motion_job(job_id: uuid.UUID) -> None:
    from ...ai.catalog import get as get_caps
    from .. import get_queue

    async with get_sessionmaker()() as session:
        job = await session.get(jobs.Job, job_id)
        if job is None or job.status in (jobs.JobStatus.SUCCEEDED,
                                         jobs.JobStatus.FAILED,
                                         jobs.JobStatus.CANCELLED):
            return
        caps = get_caps(job.payload["model_key"])
        port = get_video_port(job.payload["adapter"])
        sub = Submission(provider_job_id=job.payload["provider_job_id"],
                         endpoint=job.payload.get("endpoint", ""),
                         raw=job.payload.get("submission") or {})

        state = await port.poll(sub)
        polls = int(job.payload.get("polls", 0)) + 1
        job.payload = {**job.payload, "polls": polls}

        if state.error is not None:
            await jobs.fail(session, job, f"{state.error.kind}:{state.error.code}",
                            state.error.detail, retryable=state.error.retryable)
            return

        if not state.done:
            waited = sum(POLL_BACKOFF_S[:polls]) + POLL_BACKOFF_S[-1] * max(
                0, polls - len(POLL_BACKOFF_S))
            if waited > caps.max_wait_s:
                await jobs.fail(
                    session, job, "provider_timeout",
                    f"{caps.display_name} did not finish within "
                    f"{caps.max_wait_s}s. The generation may still complete on "
                    f"their side and be billed; check before resubmitting.")
                return
            await jobs.progress(
                session, job, min(70, 25 + polls * 5),
                f"{caps.display_name}: {state.progress_hint or 'rendering'} "
                f"({waited:.0f}s elapsed)")
            job.status = jobs.JobStatus.AWAITING_PROVIDER
            await session.commit()
            delay = POLL_BACKOFF_S[min(polls, len(POLL_BACKOFF_S)) - 1]
            # The tick number, so every re-enqueue is a distinct delivery to
            # the broker (see the note in submit_motion_job).
            await get_queue().enqueue("motion.poll", job.id,
                                      attempt=polls,
                                      defer_s=delay * _poll_scale())
            return

        log.info("I2V COMPLETE     job=%s after %d poll(s); downloading",
                 str(job.id)[:8], polls)
        job.payload = {**job.payload, "video_uri": state.video_uri,
                       "reported_cost_cents": state.reported_cost_cents,
                       "model_version": state.model_version}
        await jobs.progress(session, job, 75, "clip ready; downloading")
        job.status = jobs.JobStatus.AWAITING_PROVIDER
        await session.commit()
        # Immediately: several providers delete media on a retention clock,
        # and the download is the only thing standing between a paid
        # generation and losing it.
        await get_queue().enqueue("motion.download", job.id, attempt=job.attempt)


# --------------------------------------------------------------------------- #
# 3. download + validate
# --------------------------------------------------------------------------- #
class ClipInvalid(RuntimeError):
    pass


def validate_clip(data: bytes, expected_duration_s: float) -> dict:
    """Measure what actually arrived. Never trust the request.

    Raises ClipInvalid with a sentence naming what is wrong. The caller fails
    the shot on it -- it must never quietly substitute a still, because a
    silent degradation is exactly how a film turns back into a slideshow
    without anyone deciding that it should.
    """
    if len(data) < MIN_CLIP_BYTES:
        raise ClipInvalid(
            f"the download is {len(data)} bytes, far too small to be a clip. "
            f"Most likely a truncated transfer or an error page.")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "clip.mp4"
        path.write_bytes(data)
        info = probe(path)
        if not info.ok:
            raise ClipInvalid(f"ffprobe could not read the clip: {info.note}")
        if not info.has_video:
            raise ClipInvalid("the file contains no video stream")
        if not info.duration_ms:
            raise ClipInvalid("the clip reports no duration")
        if not (info.width and info.height):
            raise ClipInvalid("the clip reports no frame size")
        drift = info.duration_ms - int(expected_duration_s * 1000)
        if abs(drift) > DURATION_TOLERANCE_MS:
            raise ClipInvalid(
                f"the clip is {info.duration_ms}ms but {expected_duration_s:g}s "
                f"was requested ({drift:+d}ms). Beyond "
                f"{DURATION_TOLERANCE_MS}ms the narration under it would "
                f"visibly drift.")
        return {"duration_ms": info.duration_ms, "width": info.width,
                "height": info.height, "fps": float(info.fps or 0) or None,
                "has_audio": info.has_audio}


async def download_motion_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        async with jobs.running(session, job_id) as job:
            if job is None:
                return
            shot = await session.get(Shot, job.target_id)
            project = await session.get(Project, job.project_id)
            if shot is None or project is None:
                await jobs.fail(session, job, "not_found",
                                "the shot or project no longer exists")
                return

            from ...ai.catalog import get as get_caps
            from ...ai.ports import OperationState
            caps = get_caps(job.payload["model_key"])
            port = get_video_port(job.payload["adapter"])
            expected = float(job.payload["resolved_duration_s"])

            try:
                result = await port.fetch(OperationState(
                    done=True, video_uri=job.payload.get("video_uri"),
                    raw=job.payload.get("submission") or {}))
            except AIError as exc:
                await jobs.fail(session, job, f"{exc.kind}:{exc.code}",
                                exc.detail, retryable=exc.retryable)
                return

            await jobs.progress(session, job, 90, "validating the clip")
            try:
                measured = validate_clip(result.data, expected)
            except ClipInvalid as exc:
                # Deliberately terminal for this attempt. The retry machinery
                # may try again; what it must never do is fall back to a still
                # and call the shot finished.
                await jobs.fail(session, job, "clip_invalid", str(exc),
                                retryable=True)
                return

            aid = uuid7()
            key = asset_key(project.id, "clip", aid, "mp4")
            blob = await get_storage().put(key, result.data)
            est = float(job.payload.get("estimated_cost_cents") or 0)
            reported = job.payload.get("reported_cost_cents")
            cost = float(reported) if reported is not None else est

            session.add(Asset(
                id=aid, project_id=project.id, kind=AssetKind.CLIP,
                source=AssetSource.GENERATED, storage_key=key,
                mime=result.mime or "video/mp4", bytes=blob.bytes,
                checksum=blob.checksum,
                width=measured["width"], height=measured["height"],
                duration_ms=measured["duration_ms"], fps=measured["fps"],
                has_audio=measured["has_audio"],
                provider=caps.provider, model=caps.model_key,
                input_hash=job.payload["input_hash"],
                params={"prompt": job.payload.get("prompt", ""),
                        "model_id": caps.model_id,
                        "continuity": job.payload.get(
                            "continuity", MotionContinuity.NONE.value),
                        "requested_duration_s": expected,
                        "provider_job_id": job.payload.get("provider_job_id"),
                        "model_version": job.payload.get("model_version"),
                        "cost_source": ("reported" if reported is not None
                                        else "estimated")},
                cost_cents=cost))

            session.add(AICall(
                id=uuid7(), project_id=project.id, job_id=job.id,
                capability="video", provider=caps.provider,
                model=caps.model_key, units=1, cost_cents=cost,
                latency_ms=0, ok=True))
            project.spent_cents = int(project.spent_cents + round(cost))

            # Auto-select only when the shot has nothing: a human's choice of
            # clip must never be overwritten by a regeneration.
            if shot.selected_clip_id is None:
                shot.selected_clip_id = aid
            shot.motion_mode = MotionMode.GENERATED
            if project.stage in (ProjectStage.NARRATION, ProjectStage.PREVIEWED):
                project.stage = ProjectStage.MOTION

            log.info("CLIP PATH        shot=%s file=%s measured=%dms %sx%s "
                     "fps=%s cost=%.2f USD",
                     str(shot.id)[:8], key, measured["duration_ms"],
                     measured["width"], measured["height"], measured["fps"],
                     cost / 100)

            await jobs.succeed(session, job, {
                "asset_id": str(aid), "cached": False,
                "duration_ms": measured["duration_ms"],
                "width": measured["width"], "height": measured["height"],
                "model_key": caps.model_key,
                "cost_cents": round(cost, 2)})
            await jobs.notify_entity(project.id, "shot", shot.id, "clip_ready")
