"""Job handlers, registered by kind.

A handler takes only a job id: everything it needs it loads itself, so the
queue never carries state that could go stale between enqueue and execution.
"""
from __future__ import annotations

from .narration import synthesize_narration_job
from .render import render_job
from .stills import generate_still_job
from .story import analyze_story_job, generate_storyboard_job

HANDLERS = {
    "story.analyze": analyze_story_job,
    "storyboard.generate": generate_storyboard_job,
    "asset.image": generate_still_job,
    "narration.tts": synthesize_narration_job,
    "render.preview": render_job,
    "render.final": render_job,
}

__all__ = ["HANDLERS", "analyze_story_job", "generate_storyboard_job",
           "generate_still_job",
           "synthesize_narration_job"]
