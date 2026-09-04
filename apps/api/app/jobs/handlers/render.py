"""Rendering as a background job.

The renderer is CPU-bound and runs on its own worker at concurrency 1; this
handler is only the bridge between the database and a function that already
knows how to turn a Timeline into a film.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from sqlalchemy import select

from ...db.ids import uuid7
from ...db.models import (Asset, AssetKind, AssetSource, Project, ProjectStage,
                          Render)
from ...db.session import get_sessionmaker
from ...render import render as run_render
from ...render.timeline import Profile, Timeline
from ...storage import asset_key, get_storage
from .. import service as jobs


async def render_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        async with jobs.running(session, job_id) as job:
            if job is None:
                return

            render_row = await session.get(Render, job.target_id)
            project = await session.get(Project, job.project_id)
            if render_row is None or project is None:
                await jobs.fail(session, job, "not_found", "render not found")
                return

            timeline = Timeline.model_validate(render_row.timeline)
            render_row.status = "running"
            await session.commit()

            storage = get_storage()
            out_dir = Path(storage.root) / str(project.id) / "renders"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{render_row.id}.mp4"

            loop = asyncio.get_running_loop()
            progress_state = {"pct": 0}

            def on_status(message: str, frac: float) -> None:
                # ffmpeg runs in a thread; hop back to the loop to write
                # progress rather than touching the session from there.
                pct = 10 + int(frac * 85)
                if pct - progress_state["pct"] >= 5 or frac >= 1.0:
                    progress_state["pct"] = pct
                    asyncio.run_coroutine_threadsafe(
                        _progress(job_id, pct, message), loop)

            await jobs.progress(session, job, 5, "building the film")
            try:
                result = await asyncio.to_thread(
                    run_render, timeline, out_path,
                    cache_dir=out_dir.parent / ".render-cache",
                    on_status=on_status)
            except Exception as exc:                       # noqa: BLE001
                render_row.status = "failed"
                render_row.error = str(exc)[:4000]
                await jobs.fail(session, job, "render_failed", str(exc)[:2000])
                return

            await jobs.progress(session, job, 96, "storing the film")
            assets: dict[str, uuid.UUID] = {}
            for label, path, kind, mime in (
                ("video", result.video, AssetKind.VIDEO, "video/mp4"),
                ("poster", result.poster, AssetKind.POSTER, "image/jpeg"),
                ("subtitle", result.srt, AssetKind.SUBTITLE, "application/x-subrip"),
            ):
                if not path or not Path(path).exists():
                    continue
                aid = uuid7()
                key = asset_key(project.id, "render", aid, Path(path).suffix)
                blob = await storage.put(key, Path(path).read_bytes())
                session.add(Asset(
                    id=aid, project_id=project.id, kind=kind,
                    source=AssetSource.DERIVED, storage_key=key, mime=mime,
                    bytes=blob.bytes, checksum=blob.checksum,
                    width=result.width if label == "video" else None,
                    height=result.height if label == "video" else None,
                    duration_ms=result.duration_ms if label == "video" else None,
                    params={"profile": str(timeline.profile)}))
                assets[label] = aid

            render_row.status = "succeeded"
            render_row.video_asset_id = assets.get("video")
            render_row.poster_asset_id = assets.get("poster")
            render_row.subtitle_asset_id = assets.get("subtitle")
            render_row.duration_ms = result.duration_ms
            if project.stage in (ProjectStage.NARRATION, ProjectStage.STILLS):
                project.stage = ProjectStage.PREVIEWED

            await jobs.succeed(session, job, {
                "render_id": str(render_row.id),
                "duration_ms": result.duration_ms,
                "width": result.width, "height": result.height,
                "cache_hits": result.cache_hits,
                "cache_misses": result.cache_misses,
                "seconds": round(result.elapsed_s, 1)})
            await jobs.notify_entity(project.id, "render", render_row.id, "ready")


async def _progress(job_id: uuid.UUID, pct: int, message: str) -> None:
    async with get_sessionmaker()() as s:
        from ...db.models import Job
        job = await s.get(Job, job_id)
        if job:
            await jobs.progress(s, job, pct, message)
