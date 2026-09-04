"""Narration synthesis as a background job."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from ...ai.ports import AIError
from ...ai.registry import get_speech_port
from ...db.ids import uuid7
from ...db.models import (AICall, Asset, AssetKind, AssetSource, NarrationLine,
                          Project, ProjectStage)
from ...db.session import get_sessionmaker
from ...services.narration_service import line_input_hash, voice_for_line
from ...storage import asset_key, get_storage
from .. import service as jobs


async def synthesize_narration_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        async with jobs.running(session, job_id) as job:
            if job is None:
                return

            line = await session.get(NarrationLine, job.target_id)
            project = await session.get(Project, job.project_id)
            if line is None or project is None:
                await jobs.fail(session, job, "not_found",
                                "the narration line no longer exists")
                return

            port = get_speech_port()
            default_voice = project.narrator_voice_id or "Kore"
            voice = await voice_for_line(session, line, default_voice)
            input_hash = line_input_hash(line.text, voice, line.delivery,
                                         getattr(port, "model", "unknown"))
            line.input_hash = input_hash

            existing = (await session.execute(
                select(Asset).where(Asset.project_id == project.id,
                                    Asset.kind == AssetKind.AUDIO,
                                    Asset.input_hash == input_hash)
                .limit(1))).scalar_one_or_none()
            if existing:
                line.audio_asset_id = existing.id
                line.duration_ms = existing.duration_ms
                await jobs.succeed(session, job, {
                    "asset_id": str(existing.id), "cached": True,
                    "duration_ms": existing.duration_ms, "cost_cents": 0.0})
                await jobs.notify_entity(project.id, "narration", line.id,
                                         "audio_ready")
                return

            await jobs.progress(session, job, 30, f"speaking as {voice}")
            try:
                # Delivery steers tone; the API has no style parameter, so it
                # goes in as a plain instruction.
                speech, usage = await port.synthesize(
                    text=line.text, voice=voice,
                    style=(line.delivery if line.delivery != "neutral" else None))
            except AIError as exc:
                await jobs.fail(session, job, f"{exc.kind}:{exc.code}",
                                exc.detail, retryable=exc.retryable)
                return

            await jobs.progress(session, job, 80, "storing audio")
            aid = uuid7()
            key = asset_key(project.id, "audio", aid, "wav")
            blob = await get_storage().put(key, speech.data)
            session.add(Asset(
                id=aid, project_id=project.id, kind=AssetKind.AUDIO,
                source=AssetSource.GENERATED, storage_key=key,
                mime=speech.mime, bytes=blob.bytes, checksum=blob.checksum,
                duration_ms=speech.duration_ms,
                provider=getattr(port, "provider", "fake"),
                model=getattr(port, "model", "unknown"),
                input_hash=input_hash,
                params={"text": line.text, "voice": voice,
                        "delivery": line.delivery,
                        "sample_rate": speech.sample_rate},
                cost_cents=usage.cost_cents))

            line.audio_asset_id = aid
            # Measured, never estimated: every later duration is built on this.
            line.duration_ms = speech.duration_ms

            session.add(AICall(
                id=uuid7(), project_id=project.id, job_id=job.id,
                capability="speech", provider=getattr(port, "provider", "fake"),
                model=getattr(port, "model", "unknown"),
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                units=len(line.text.split()), cost_cents=usage.cost_cents,
                latency_ms=usage.latency_ms, ok=True))
            project.spent_cents = int(project.spent_cents + round(usage.cost_cents))
            if project.stage == ProjectStage.STILLS:
                project.stage = ProjectStage.NARRATION

            await jobs.succeed(session, job, {
                "asset_id": str(aid), "cached": False,
                "duration_ms": speech.duration_ms, "voice": voice,
                "cost_cents": round(usage.cost_cents, 3)})
            await jobs.notify_entity(project.id, "narration", line.id,
                                     "audio_ready")
