"""Job handlers, registered by kind.

A handler takes only a job id: everything it needs it loads itself, so the
queue never carries state that could go stale between enqueue and execution.
"""
from __future__ import annotations

from .motion import (download_motion_job, poll_motion_job,
                     submit_motion_job)
from .narration import synthesize_narration_job
from .render import render_job
from .stills import generate_still_job
from .story import analyze_story_job, generate_storyboard_job

HANDLERS = {
    "story.analyze": analyze_story_job,
    "storyboard.generate": generate_storyboard_job,
    "asset.image": generate_still_job,
    "narration.tts": synthesize_narration_job,
    # Three kinds, not one: the provider's job id is durable in Postgres
    # before polling starts, so a killed worker never loses a paid clip.
    "motion.submit": submit_motion_job,
    "motion.poll": poll_motion_job,
    "motion.download": download_motion_job,
    "render.preview": render_job,
    "render.final": render_job,
}

__all__ = ["HANDLERS", "analyze_story_job", "generate_storyboard_job",
           "generate_still_job", "synthesize_narration_job",
           "submit_motion_job", "poll_motion_job", "download_motion_job"]
