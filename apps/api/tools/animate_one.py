"""Drive ONE shot through the real motion path and render the film.

    python tools/animate_one.py --project <uuid>              # plan only
    python tools/animate_one.py --project <uuid> --yes        # spends money

Calls the same handlers the worker calls -- submit, poll, download -- then
rebuilds the timeline and renders. No HTTP and no broker, so the trace is
readable, but every stage that matters is the production one.

Exists because the end-to-end path never invoked motion: `make smoke` runs
stills -> narration -> render and nothing in between ever asked for a clip.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(name)-14s %(message)s",
    datefmt="%H:%M:%S")
for noisy in ("httpx", "httpcore", "sqlalchemy.engine", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("hbz.animate")

import app.jobs as jobs_pkg                                    # noqa: E402
from sqlalchemy import select                                  # noqa: E402


class _ManualQueue:
    """Swallow the handlers' self-scheduling; this script is the scheduler.

    The default queue is `inline`, which runs an enqueued job immediately as
    an asyncio task. `poll_motion_job` re-enqueues itself, so under the inline
    queue it spawns a self-driving chain -- and a driver that ALSO polls ends
    up with several chains racing, each firing `motion.download`. One claims
    the job; the rest find it unclaimable; and whichever is still in flight
    when the process exits is cancelled, leaving the row stuck in `running`
    with the clip paid for and never collected.

    Under arq (the deployed path) that race does not exist, because each
    delivery is handled by a worker that owns it to completion.
    """

    def __init__(self):
        self.dropped: list[tuple[str, str]] = []

    async def enqueue(self, kind, job_id, defer_s=0.0, attempt=0):
        self.dropped.append((kind, str(job_id)))

    async def close(self):
        return None

from app.db.models import (Asset, AssetKind, MotionMode, Project,  # noqa: E402
                           Scene, Shot)
from app.db.session import get_sessionmaker                    # noqa: E402
from app.jobs import service as jobs                           # noqa: E402
from app.jobs.handlers.motion import (download_motion_job,     # noqa: E402
                                      poll_motion_job,
                                      submit_motion_job)
from app.render.pipeline import render                         # noqa: E402
from app.render.timeline import Profile, SourceKind            # noqa: E402
from app.services.motion_service import plan_motion            # noqa: E402
from app.services.timeline_builder import build_timeline       # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out" / "animated"


async def _pick_shot(session, project, index: int):
    rows = (await session.execute(
        select(Shot, Scene).join(Scene, Scene.id == Shot.scene_id)
        .where(Shot.project_id == project.id)
        .order_by(Scene.sort_order, Shot.sort_order))).all()
    ready = [(s, sc) for s, sc in rows if s.selected_image_id]
    if not ready:
        log.error("no shot in this project has an approved still; "
                  "the keyframe is the clip's first frame")
        return None, None, rows
    return (*ready[min(index, len(ready) - 1)], rows)


async def _animate_all(args) -> int:
    """Every ready shot, priced together and checked against the budget once.

    Checking per shot would let a fan-out spend most of a budget before the
    shot that exceeds it is reached, and clips already bought are not
    refundable.
    """
    async with get_sessionmaker()() as session:
        project = await session.get(Project, uuid.UUID(args.project))
        rows = (await session.execute(
            select(Shot, Scene).join(Scene, Scene.id == Shot.scene_id)
            .where(Shot.project_id == project.id)
            .order_by(Scene.sort_order, Shot.sort_order))).all()
        ready, total = [], 0
        for shot, scene in rows:
            plan = await plan_motion(session, shot, project,
                                     model_key=args.model)
            if plan.ok:
                ready.append((shot, scene, plan))
                total += plan.estimated_cost_cents
            else:
                log.warning("SKIP %-22s %s", scene.title[:22],
                            plan.blocking[0].message)
        remaining = int(project.budget_cents) - int(project.spent_cents)
        log.info("FILM PLAN  %d shot(s), %.2f USD total; budget has "
                 "%.2f USD left", len(ready), total / 100, remaining / 100)
        if total > remaining:
            log.error("REFUSED: this would spend %.2f USD against %.2f USD of "
                      "remaining budget. Raise the project budget deliberately "
                      "or animate fewer shots.", total / 100, remaining / 100)
            return 2
        if not args.yes:
            log.info("planning only -- nothing sent, nothing charged. "
                     "Re-run with --yes.")
            return 0

    for i, (shot, scene, _) in enumerate(ready, 1):
        log.info("=== [%d/%d] %s ===", i, len(ready), scene.title)
        rc = await _animate_shot(args, shot.id)
        if rc:
            log.error("stopping: shot %d failed", i)
            break
    return await _finish(args)


async def _animate_shot(args, shot_id) -> int:
    """Submit, poll to completion, download. One shot, real handlers."""
    async with get_sessionmaker()() as session:
        shot = await session.get(Shot, shot_id)
        project = await session.get(Project, uuid.UUID(args.project))
        plan = await plan_motion(session, shot, project, model_key=args.model)
        job, _ = await jobs.enqueue(
            session, project_id=project.id, kind="motion.submit",
            input_hash=plan.input_hash, target_type="shot", target_id=shot.id,
            payload={"model_key": plan.model_key,
                     "duration_s": plan.requested_duration_s})
        await session.commit()
        job_id = job.id

    await submit_motion_job(job_id)
    for _ in range(200):
        async with get_sessionmaker()() as s:
            j = await s.get(jobs.Job, job_id)
            status, payload = j.status, dict(j.payload)
            code, detail = j.error_code, j.error_detail
        if status in (jobs.JobStatus.FAILED, jobs.JobStatus.CANCELLED):
            log.error("motion failed: %s -- %s", code, (detail or "")[:200])
            return 1
        if status is jobs.JobStatus.SUCCEEDED:
            return 0                      # cache hit; nothing to download
        if payload.get("video_uri"):
            break
        await poll_motion_job(job_id)
        await asyncio.sleep(args.poll_s)
    await download_motion_job(job_id)
    async with get_sessionmaker()() as s:
        j = await s.get(jobs.Job, job_id)
        if j.status is not jobs.JobStatus.SUCCEEDED:
            log.error("download failed: %s -- %s", j.error_code,
                      (j.error_detail or "")[:200])
            return 1
    return 0


async def run(args) -> int:
    jobs_pkg.get_queue.cache_clear()
    jobs_pkg.get_queue = lambda _q=_ManualQueue(): _q

    if args.all:
        return await _animate_all(args)

    async with get_sessionmaker()() as session:
        project = await session.get(Project, uuid.UUID(args.project))
        if project is None:
            log.error("no project %s", args.project)
            return 2
        log.info("PROJECT %r  budget=%dc spent=%dc",
                 project.title, project.budget_cents, project.spent_cents)

        shot, scene, all_rows = await _pick_shot(session, project, args.shot)
        if shot is None:
            return 2

        # Recover a generation that was paid for but never collected: the
        # provider still has it and the uri is durable in the payload, so
        # this costs nothing and re-generating would cost again.
        stuck = (await session.execute(
            select(jobs.Job).where(
                jobs.Job.target_id == shot.id,
                jobs.Job.kind == "motion.submit",
                jobs.Job.payload["video_uri"].astext.isnot(None))
            .order_by(jobs.Job.queued_at.desc()).limit(1))).scalar_one_or_none()
        if stuck is not None and stuck.status is not jobs.JobStatus.SUCCEEDED:
            log.warning("RESUMING paid generation %s -- provider still has the "
                        "clip, so this costs nothing", str(stuck.id)[:8])
            stuck.status = jobs.JobStatus.AWAITING_PROVIDER
            await session.commit()
            await download_motion_job(stuck.id)
            return await _finish(args, log_prefix="RESUMED")

        plan = await plan_motion(session, shot, project,
                                 model_key=args.model)
        log.info("PLAN  shot=%s scene=%r", str(shot.id)[:8], scene.title)
        log.info("      model=%s duration=%gs cost=%.2f USD",
                 plan.model_key, plan.resolved_duration_s,
                 plan.estimated_cost_cents / 100)
        log.info("      prompt=%s", plan.prompt.positive[:150])
        for n in plan.warnings:
            log.warning("      ! %s", n.message)
        for n in plan.blocking:
            log.error("      BLOCKED %s", n.message)
        if plan.blocking:
            return 1

        if not args.yes:
            log.info("planning only -- nothing sent, nothing charged. "
                     "Re-run with --yes.")
            return 0

        # ---- the real handlers, in the real order -------------------------
        job, _ = await jobs.enqueue(
            session, project_id=project.id, kind="motion.submit",
            input_hash=plan.input_hash, target_type="shot", target_id=shot.id,
            payload={"model_key": plan.model_key,
                     "duration_s": plan.requested_duration_s})
        await session.commit()
        job_id = job.id

    await submit_motion_job(job_id)

    for tick in range(120):
        async with get_sessionmaker()() as s:
            j = await s.get(jobs.Job, job_id)
            status, payload = j.status, dict(j.payload)
        if status in (jobs.JobStatus.FAILED, jobs.JobStatus.CANCELLED):
            async with get_sessionmaker()() as s:
                j = await s.get(jobs.Job, job_id)
                log.error("motion job failed: %s -- %s",
                          j.error_code, (j.error_detail or "")[:300])
            return 1
        if payload.get("video_uri"):
            break
        await poll_motion_job(job_id)
        await asyncio.sleep(args.poll_s)
    await download_motion_job(job_id)
    return await _finish(args)


async def _finish(args, log_prefix: str = "GENERATED") -> int:
    """Report DB state, rebuild the timeline, render, and say what happened."""
    async with get_sessionmaker()() as session:
        project = await session.get(Project, uuid.UUID(args.project))

        rows = (await session.execute(
            select(Shot, Scene).join(Scene, Scene.id == Shot.scene_id)
            .where(Shot.project_id == project.id)
            .order_by(Scene.sort_order, Shot.sort_order))).all()
        from app.storage import get_storage
        storage = get_storage()
        log.info("DB STATE  %d shots:", len(rows))
        for shot, scene in rows:
            clip = (await session.get(Asset, shot.selected_clip_id)
                    if shot.selected_clip_id else None)
            on_disk = None
            if clip is not None:
                p = storage.local_path(clip.storage_key)
                on_disk = bool(p and p.exists())
            log.info("          %-22s motion_mode=%-9s clip=%-8s "
                     "hash=%-10s asset=%s disk=%s",
                     scene.title[:22], shot.motion_mode.value,
                     str(shot.selected_clip_id)[:8] if shot.selected_clip_id
                     else "-",
                     (shot.motion_input_hash or "-")[:10],
                     "yes" if clip else "no",
                     on_disk if clip else "-")

        result = await build_timeline(session, project, profile=Profile.PREVIEW)
        if result.timeline is None:
            for p in result.blocking:
                log.error("timeline blocked: %s", p.message)
            return 1
        timeline = result.timeline

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{args.project[:8]}-animated.mp4"
    log.info("RENDERING to %s", dest)
    res = render(timeline, dest, cache_dir=OUT / ".cache")
    log.info("RENDERED  %s  %.1f MB  %dms",
             dest.name, dest.stat().st_size / 1e6, res.duration_ms or 0)

    n_clip = sum(1 for c in timeline.clips if c.source.kind is SourceKind.CLIP)
    log.info("RESULT    %d/%d shots are real generated video",
             n_clip, len(timeline.clips))
    log.info("          open %s", dest)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--shot", type=int, default=0,
                    help="index among shots that have an approved still")
    ap.add_argument("--all", action="store_true",
                    help="animate every ready shot, not just one. Prints the "
                         "whole film's cost and checks it against the budget "
                         "before spending anything.")
    ap.add_argument("--model", default="kling-2.5-turbo-i2v")
    ap.add_argument("--poll-s", type=float, default=10.0)
    ap.add_argument("--yes", action="store_true",
                    help="actually spend money. Without it this only plans.")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
