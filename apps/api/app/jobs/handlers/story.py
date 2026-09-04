"""Domain 1 and 2 as background jobs."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from ...ai.ports import AIError
from ...ai.registry import get_text_port
from ...db.ids import uuid7
from ...db.models import (AICall, Job, Project, ProjectStage, StoryAnalysisDoc,
                          StoryboardDoc, StoryInput)
from ...db.session import get_sessionmaker
from ...schemas.ai import StoryAnalysis
from ...services.story_service import analyze_story
from ...services.storyboard_service import (StoryboardRequest,
                                             generate_storyboard)
from .. import service as jobs


async def _record_call(session, job: Job, capability: str, usage) -> None:
    session.add(AICall(
        id=uuid7(), project_id=job.project_id, job_id=job.id,
        capability=capability, provider=usage.model.split("-")[0],
        model=usage.model, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens, cost_cents=usage.cost_cents,
        latency_ms=usage.latency_ms, ok=True))
    project = await session.get(Project, job.project_id)
    if project:
        # Spend is tracked on the project so the budget gate has one number to
        # read, rather than aggregating ai_calls on every enqueue.
        project.spent_cents = int(project.spent_cents + round(usage.cost_cents))


async def analyze_story_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        async with jobs.running(session, job_id) as job:
            if job is None:
                return
            await jobs.progress(session, job, 5, "reading the story")

            story = (await session.execute(
                select(StoryInput)
                .where(StoryInput.project_id == job.project_id)
                .order_by(StoryInput.version.desc()).limit(1))).scalar_one()

            try:
                res = await analyze_story(story.raw_text, get_text_port())
            except AIError as exc:
                await jobs.fail(session, job, f"{exc.kind}:{exc.code}",
                                exc.detail, retryable=exc.retryable)
                return

            await jobs.progress(session, job, 80, "saving analysis")
            doc = StoryAnalysisDoc(
                id=uuid7(), project_id=job.project_id, story_input_id=story.id,
                schema_version=res.value.schema_version,
                document=res.value.model_dump(mode="json"),
                provider=res.usage.model.split("-")[0], model=res.usage.model,
                cost_cents=res.usage.cost_cents, repaired=res.repaired)
            session.add(doc)
            await _record_call(session, job, "text", res.usage)

            project = await session.get(Project, job.project_id)
            if project and project.stage == ProjectStage.DRAFT:
                project.stage = ProjectStage.ANALYZED

            await jobs.succeed(session, job, {
                "analysis_id": str(doc.id), "title": res.value.title,
                "characters": len(res.value.characters),
                "beats": len(res.value.beats),
                "cost_cents": round(res.usage.cost_cents, 2)})
            await jobs.notify_entity(job.project_id, "analysis", doc.id, "ready")


async def generate_storyboard_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        async with jobs.running(session, job_id) as job:
            if job is None:
                return
            await jobs.progress(session, job, 5, "loading story and analysis")

            story = (await session.execute(
                select(StoryInput)
                .where(StoryInput.project_id == job.project_id)
                .order_by(StoryInput.version.desc()).limit(1))).scalar_one()
            analysis_doc = (await session.execute(
                select(StoryAnalysisDoc)
                .where(StoryAnalysisDoc.project_id == job.project_id)
                .order_by(StoryAnalysisDoc.created_at.desc())
                .limit(1))).scalar_one_or_none()
            if analysis_doc is None:
                await jobs.fail(session, job, "stage_precondition_failed",
                                "analyse the story before generating a storyboard")
                return

            project = await session.get(Project, job.project_id)
            await jobs.progress(session, job, 20, "directing the storyboard")

            try:
                res = await generate_storyboard(
                    StoryboardRequest(
                        story_text=story.raw_text,
                        analysis=StoryAnalysis.model_validate(analysis_doc.document),
                        target_length_s=int(job.payload.get("target_length_s", 90)),
                        aspect_ratio=project.aspect_ratio,
                        style_preset=project.style_preset,
                        notes=job.payload.get("notes", "")),
                    get_text_port())
            except AIError as exc:
                await jobs.fail(session, job, f"{exc.kind}:{exc.code}",
                                exc.detail, retryable=exc.retryable)
                return

            await jobs.progress(session, job, 85, "saving storyboard")
            version = ((await session.execute(
                select(func.coalesce(func.max(StoryboardDoc.version), 0))
                .where(StoryboardDoc.project_id == job.project_id))).scalar_one()) + 1

            doc = StoryboardDoc(
                id=uuid7(), project_id=job.project_id,
                story_analysis_id=analysis_doc.id, version=version,
                schema_version=res.value.schema_version,
                document=res.value.model_dump(mode="json"),
                provider=res.usage.model.split("-")[0], model=res.usage.model,
                cost_cents=res.usage.cost_cents, repaired=res.repaired)
            session.add(doc)
            await _record_call(session, job, "text", res.usage)

            if project and project.stage in (ProjectStage.DRAFT,
                                             ProjectStage.ANALYZED):
                project.stage = ProjectStage.STORYBOARDED

            sb = res.value
            await jobs.succeed(session, job, {
                "storyboard_id": str(doc.id), "version": version,
                "scenes": len(sb.scenes), "shots": sb.shot_count,
                "runtime_s": round(sb.total_target_duration_s),
                "repaired": res.repaired,
                "cost_cents": round(res.usage.cost_cents, 2)})
            await jobs.notify_entity(job.project_id, "storyboard", doc.id, "ready")
