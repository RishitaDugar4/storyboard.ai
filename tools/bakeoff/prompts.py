"""Motion prompt composition and the standardized bake-off case set.

The application composes prompts from structured storyboard fields; the harness
composes them from the same function with fixed inputs, so the bake-off tests
*our* prompting rather than ad-hoc strings.

Graduates to ``apps/api/app/ai/prompts/compose.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from catalog import AudioBehavior, VideoModelCaps

# Bump whenever composition changes. Marks every existing clip stale, on purpose.
COMPOSER_VERSION = 1

CAMERA_PHRASE = {
    "static": "The camera is locked off and does not move.",
    "push_in": "The camera pushes in slowly toward the subject.",
    "pull_out": "The camera pulls back slowly, revealing more of the scene.",
    "pan_left": "The camera pans smoothly to the left.",
    "pan_right": "The camera pans smoothly to the right.",
    "tilt_up": "The camera tilts slowly upward.",
    "tilt_down": "The camera tilts slowly downward.",
    "orbit": "The camera orbits slowly around the subject.",
    "handheld": "Subtle handheld movement, as if hand-held.",
}

DEFAULT_EXCLUSIONS = [
    "text", "captions", "watermark", "morphing faces", "extra limbs",
    "warped hands", "flickering",
]

STYLE_HOLD = ("The art style, character design and colour palette stay "
              "identical to the source image throughout.")


@dataclass(frozen=True)
class MotionPrompt:
    positive: str
    negative: str | None


def compose_motion_prompt(
    *,
    caps: VideoModelCaps,
    subject_motion: str,
    camera_move: str,
    motion_language: str = "Gentle, unhurried camera movement.",
    ambient_sound: str = "",
    exclusions: list[str] | None = None,
) -> MotionPrompt:
    """Render the provider-appropriate prompt from structured fields.

    Provider quirks are handled here, driven by capability flags -- never by a
    branch on the provider's name.
    """
    exclusions = list(exclusions if exclusions is not None else DEFAULT_EXCLUSIONS)
    parts = [
        "Animate this image.",
        subject_motion,
        CAMERA_PHRASE.get(camera_move, CAMERA_PHRASE["static"]),
        motion_language,
        STYLE_HOLD,
    ]

    if caps.audio is AudioBehavior.ALWAYS_ON:
        # Audio cannot be disabled on this model; steer it away from speech so
        # the track we discard never fights the narrator we keep.
        parts.append("No spoken dialogue, no voices, no on-screen text.")
        if ambient_sound:
            parts.append(f"Ambient sound: {ambient_sound}.")

    if caps.supports_negative_prompt:
        return MotionPrompt(" ".join(p for p in parts if p), ", ".join(exclusions))

    # No negative-prompt input: fold exclusions in, phrased positively.
    parts.append("Clean frame with no text or captions; faces and hands stay "
                 "stable and well-formed.")
    return MotionPrompt(" ".join(p for p in parts if p), None)


@dataclass(frozen=True)
class Case:
    """One standardized motion test, applied identically to every model."""

    case_id: str
    camera_move: str
    subject_motion: str
    ambient_sound: str = ""
    #: Sample narration used only to exercise the fit evaluation at plan time.
    narration: str = ""
    rationale: str = ""

    @property
    def narration_word_count(self) -> int:
        return len(self.narration.split())


CASES: list[Case] = [
    Case(
        case_id="static-subtle",
        camera_move="static",
        subject_motion=("Small ambient movement only: hair and fabric shift "
                        "slightly, light flickers."),
        ambient_sound="quiet room tone",
        narration="It was the last winter the light would burn.",
        rationale="Does the model hold still when told to? The most common "
                  "failure is inventing motion nobody asked for.",
    ),
    Case(
        case_id="push-in",
        camera_move="push_in",
        subject_motion="The subject breathes and blinks slowly.",
        ambient_sound="distant wind",
        narration="She had kept the lamp lit every night for forty years, "
                  "without once being asked to.",
        rationale="Tests the workhorse move, and whether faces survive a "
                  "scale change. Long narration deliberately overflows short "
                  "clips so the fit evaluation is exercised.",
    ),
    Case(
        case_id="pan",
        camera_move="pan_right",
        subject_motion="Light moves gently across the scene.",
        ambient_sound="soft ambience",
        narration="Beyond the window, the sea went on.",
        rationale="Tests parallax and whether the model invents geometry as "
                  "it reveals new frame.",
    ),
    Case(
        case_id="action",
        camera_move="handheld",
        subject_motion=("The character turns their head and looks off-screen "
                        "to the right."),
        ambient_sound="footsteps on wood",
        narration="Then she heard it.",
        rationale="Deliberate identity stress: a head turn is where character "
                  "consistency breaks first.",
    ),
]

CASES_BY_ID = {c.case_id: c for c in CASES}
