"""Domain 1: story parsing.

Builds the prompt, calls the text port, returns a validated StoryAnalysis.
No database here -- persistence arrives at M3.
"""
from __future__ import annotations

from pathlib import Path

from ..ai.ports import StructuredResult, TextPort
from ..schemas.ai import StoryAnalysis

PROMPTS = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "system"
MAX_STORY_WORDS = 8000


def _system() -> str:
    return (PROMPTS / "story_analysis.md").read_text()


def build_user_prompt(story_text: str) -> str:
    words = len(story_text.split())
    return (
        f"Here is the story ({words} words). Read it and return your analysis.\n\n"
        f"<story>\n{story_text.strip()}\n</story>"
    )


async def analyze_story(text: str, port: TextPort, *, effort: str = "high"
                        ) -> StructuredResult[StoryAnalysis]:
    text = text.strip()
    if not text:
        raise ValueError("story is empty")
    if (n := len(text.split())) > MAX_STORY_WORDS:
        raise ValueError(
            f"story is {n} words; the limit is {MAX_STORY_WORDS}. "
            "Split it or trim it -- beyond this the analysis loses focus.")
    return await port.generate_structured(
        schema=StoryAnalysis, system=_system(),
        user=build_user_prompt(text), max_tokens=16000, effort=effort,
    )
