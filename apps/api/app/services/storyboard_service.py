"""Domain 2: storyboard generation.

The story text and its analysis form a stable prefix reused by every later
per-scene regeneration, so they are passed as a cache prefix rather than
inlined in the volatile instruction.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..ai.catalog import VideoModelCaps
from ..ai.catalog import get as get_caps
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
    #: The image-to-video model the film will be animated with, when one is
    #: known. Its duration grid is a hard constraint on the storyboard, not a
    #: detail to reconcile afterwards: every provider rounds a request UP, so
    #: a 13-word line written for a 6s shot silently buys a 10s clip on a
    #: 5s/10s grid. Told the grid up front, the director writes to it.
    motion_model_key: str | None = None

    @property
    def motion_caps(self) -> VideoModelCaps | None:
        return get_caps(self.motion_model_key) if self.motion_model_key else None

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
    if (caps := req.motion_caps) is not None:
        grid = sorted(caps.durations.values) or [caps.durations.min_s]
        lines += [
            "",
            f"This film will be animated with {caps.display_name}, which can "
            f"only produce clips of {caps.durations.describe()}. A duration "
            f"between those values is rounded UP to the next one, and you pay "
            f"for -- and watch -- the longer clip.",
            "So set every target_duration_s to one of these values exactly, "
            "and keep each shot's narration inside that value's budget:",
            *[f"  {d:g}s -> at most {word_budget(d)} words" for d in grid],
            f"Prefer {min(grid):g}s. Spend a longer clip only where the beat "
            f"genuinely needs the time; every one you use costs runtime you "
            f"then cannot give to another shot.",
        ]
    lines += [
        "",
        "Motion. Every shot will become a real animated clip, so each one "
        "needs a motion plan as well as a composition:",
        "  - `action` and `composition_note` describe a SINGLE FRAME: what "
        "the keyframe looks like. No movement verbs.",
        "  - `subject_motion` describes what the people physically DO across "
        "the shot -- specific, observable, one idea. \"She turns her head "
        "toward the door and takes a step back, blinking\", never \"she is "
        "afraid\" and never \"it is cinematic\".",
        "  - `environment_motion` describes what moves that nobody is doing: "
        "curtains, rain, dust in a beam, firelight, a passing car. This is "
        "often what separates an animated shot from a photograph.",
        "  - `motion_pacing` is how much happens per second: `still` `slow` "
        "`steady` `brisk`. Match the beat. A quiet scene paced `brisk` reads "
        "as wrong, and most shots should be `slow`.",
        "  - `camera_move` is the camera only, never the subject.",
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
