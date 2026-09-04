"""Domain 2: storyboard generation.

The story text and its analysis form a stable prefix reused by every later
per-scene regeneration, so they are passed as a cache prefix rather than
inlined in the volatile instruction.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..ai.pacing import word_budget
from ..ai.ports import StructuredResult, TextPort
from ..schemas.ai import StoryAnalysis, Storyboard

PROMPTS = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "system"

#: Mirrors the Storyboard schema's own bounds; kept in sync by a test.
SCENE_MIN, SCENE_MAX = 4, 20

#: Observed average shot length the director actually chooses (~6-7s). The
#: suggested scene range is derived from this so the count and the runtime
#: target cannot contradict each other -- an earlier version suggested up to 18
#: scenes for a 90s film, which is 108s before anyone writes a word.
TYPICAL_SHOT_S = 6.5


@dataclass(frozen=True)
class StoryboardRequest:
    story_text: str
    analysis: StoryAnalysis
    target_length_s: int = 90
    aspect_ratio: str = "16:9"
    style_preset: str = "storybook_gouache"
    notes: str = ""

    @property
    def suggested_scene_count(self) -> tuple[int, int]:
        """Scenes run roughly 5-8 seconds each.

        Both ends are clamped to the Storyboard schema's own limits (4-20
        scenes), so a long target cannot suggest a range the schema will then
        reject -- and the range can never come back inverted.
        """
        centre = self.target_length_s / TYPICAL_SHOT_S
        low = min(max(round(centre * 0.85), SCENE_MIN), SCENE_MAX)
        high = min(max(round(centre * 1.1), low), SCENE_MAX)
        return low, high


def _system() -> str:
    return (PROMPTS / "storyboard.md").read_text()


def build_cache_prefix(req: StoryboardRequest) -> str:
    """Stable across every regeneration for this project."""
    return (
        "<story>\n" + req.story_text.strip() + "\n</story>\n\n"
        "<analysis>\n" + req.analysis.model_dump_json(indent=2) + "\n</analysis>"
    )


def build_user_prompt(req: StoryboardRequest) -> str:
    low, high = req.suggested_scene_count
    lines = [
        "Turn the story and analysis above into a storyboard.",
        "",
        f"- Target runtime: {req.target_length_s} seconds. The sum of every "
        f"shot's target_duration_s must land between "
        f"{int(req.target_length_s * 0.9)} and {int(req.target_length_s * 1.1)} "
        f"seconds. Add them up before you finish.",
        f"- Aim for {low}-{high} scenes, one shot each. At {low}-{high} shots "
        f"that is about {req.target_length_s / high:.1f}-"
        f"{req.target_length_s / low:.1f}s per shot.",
        f"- Aspect ratio: {req.aspect_ratio}.",
        f"- Art direction starting point: {req.style_preset.replace('_', ' ')}.",
        "",
        "Word budgets you must respect (narration per shot):",
        *[f"  {d:g}s shot -> at most {word_budget(d)} words"
          for d in (4, 5, 6, 8, 10)],
    ]
    if req.notes:
        lines += ["", f"Additional direction: {req.notes}"]
    return "\n".join(lines)


async def generate_storyboard(req: StoryboardRequest, port: TextPort, *,
                              effort: str = "high"
                              ) -> StructuredResult[Storyboard]:
    return await port.generate_structured(
        schema=Storyboard, system=_system(),
        user=build_user_prompt(req), cache_prefix=build_cache_prefix(req),
        max_tokens=32000, effort=effort,
    )
