"""Deterministic prompt composition.

The model fills structured slots; the application renders the prompt. That is
what makes prompts diffable, hashable and cacheable -- and it is the only way
a character can look the same in shot 12 as in shot 1, because the exact same
canon string is emitted every time rather than paraphrased anew.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

#: Bump when composition changes. Included in every input hash, so a change
#: here correctly marks every existing still stale -- deliberately, and with a
#: cost estimate in front of the user before anything is regenerated.
COMPOSER_VERSION = 1

BASE_NEGATIVE = [
    "text", "watermark", "signature", "caption", "letters",
    "extra limbs", "distorted hands", "deformed face", "duplicate subject",
]


@dataclass(frozen=True)
class ComposedPrompt:
    positive: str
    negative: str
    #: Where each fragment came from, so the UI can colour-code it. The single
    #: most useful debugging surface in the app.
    fragments: list[tuple[str, str]]

    def hash(self, *, seed: int | None, size: str, provider: str,
             model: str) -> str:
        return hashlib.sha256(json.dumps({
            "positive": self.positive, "negative": self.negative,
            "seed": seed, "size": size, "provider": provider, "model": model,
            "composer": COMPOSER_VERSION,
        }, sort_keys=True).encode()).hexdigest()


def compose_image_prompt(
    *,
    style_bible: dict,
    shot_type: str,
    action: str,
    composition_note: str = "",
    character_prompts: list[tuple[str, str]] | None = None,
    location_fragment: str = "",
    time_of_day: str = "unspecified",
    prompt_override: str | None = None,
) -> ComposedPrompt:
    """Render the still prompt for one shot.

    `character_prompts` are (name, frozen canon) pairs, embedded verbatim.
    Never paraphrase them: the whole point of freezing the canon at lock time
    is that the same words reach the model on every shot.
    """
    sb = style_bible or {}
    frags: list[tuple[str, str]] = []

    def add(origin: str, text: str) -> None:
        if text and text.strip():
            frags.append((origin, text.strip().rstrip(".") + "."))

    if prompt_override:
        add("override", prompt_override)
    else:
        add("style", sb.get("art_style", ""))
        add("shot", f"{shot_type.replace('_', ' ')} shot")
        add("action", action)
        for name, canon in (character_prompts or []):
            add(f"character:{name}", canon)
        add("location", location_fragment)
        if time_of_day and time_of_day != "unspecified":
            add("light", f"{time_of_day} light")
        add("lighting", sb.get("lighting", ""))
        if palette := sb.get("palette"):
            add("palette", "Palette: " + ", ".join(palette))
        add("texture", sb.get("line_and_texture", ""))
        add("composition", composition_note)

    negative = list(dict.fromkeys([*(sb.get("negative") or []), *BASE_NEGATIVE]))
    return ComposedPrompt(
        positive=" ".join(t for _, t in frags),
        negative=", ".join(negative),
        fragments=frags,
    )


# --------------------------------------------------------------------------- #
# Motion.
#
# A motion prompt answers a different question from an image prompt. The image
# prompt says what the frame IS; the motion prompt says what CHANGES over the
# next few seconds. Restating the composition here is actively harmful: the
# model already has the composition as its first frame, and describing it again
# invites reinterpretation -- a new coat, a different room, another face --
# which is precisely the character drift the frozen canon exists to prevent.
#
# So the composer deliberately omits the style bible, the character canon and
# the location. Those are already in the pixels.
# --------------------------------------------------------------------------- #

#: Bumped independently of COMPOSER_VERSION so that changing how motion is
#: phrased invalidates clips without marking every still stale as well.
MOTION_COMPOSER_VERSION = 1

#: Physical description of each camera move, in the language video models
#: actually respond to. The storyboard picks from a closed vocabulary; this
#: table is where that vocabulary becomes a sentence.
CAMERA_PHRASING: dict[str, str] = {
    "static": "The camera is locked off and does not move",
    "push_in": "The camera pushes slowly in toward the subject",
    "pull_out": "The camera pulls slowly back, widening the frame",
    "pan_left": "The camera pans steadily to the left",
    "pan_right": "The camera pans steadily to the right",
    "tilt_up": "The camera tilts slowly upward",
    "tilt_down": "The camera tilts slowly downward",
    "orbit": "The camera arcs slowly around the subject",
    "handheld": "The camera drifts with a slight handheld unsteadiness",
}

#: Pacing as an instruction about rate, not about content. "Cinematic" and
#: "dynamic" are not here on purpose: they are the generic filler that makes
#: every shot look like every other shot.
PACING_PHRASING: dict[str, str] = {
    "still": "Almost nothing moves; the shot is nearly a photograph, with only "
             "the faintest drift",
    "slow": "Everything moves slowly and deliberately, in real time",
    "steady": "The movement is continuous and even-paced",
    "brisk": "The movement is quick and urgent, but never blurred",
}

#: Sent to models that accept one. These are the failure modes of image-to-video
#: specifically -- a still that never animates, and a frame that mutates into a
#: different scene -- not the image artefacts BASE_NEGATIVE covers.
MOTION_NEGATIVE = [
    "static frozen frame", "still image", "no movement",
    "morphing faces", "changing clothing", "distorted hands",
    "sudden cut", "scene change", "new characters appearing",
    "text", "watermark", "subtitles",
]


@dataclass(frozen=True)
class ComposedMotion:
    positive: str
    negative: str
    fragments: list[tuple[str, str]]

    def hash(self, *, model_key: str, duration_s: float, resolution: str,
             seed: int | None, first_frame_checksum: str) -> str:
        """Identity of this generation's inputs.

        The keyframe's checksum is in here, so approving a different still for
        the shot correctly invalidates the clip that was animated from the old
        one -- the failure that would otherwise ship a film whose motion does
        not match its own frames.
        """
        return hashlib.sha256(json.dumps({
            "positive": self.positive, "negative": self.negative,
            "model_key": model_key, "duration_s": round(duration_s, 3),
            "resolution": resolution, "seed": seed,
            "first_frame": first_frame_checksum,
            "composer": MOTION_COMPOSER_VERSION,
        }, sort_keys=True).encode()).hexdigest()


def compose_motion_prompt(
    *,
    subject_motion: str = "",
    environment_motion: str = "",
    camera_move: str = "push_in",
    motion_pacing: str = "slow",
    motion_language: str = "",
    supports_negative_prompt: bool = True,
    motion_override: str | None = None,
) -> ComposedMotion:
    """Render the motion prompt for one shot.

    Order matters: subjects first, then environment, then camera, then rate.
    Video models weight the opening of the prompt most heavily, and what the
    characters do is what the audience watches.
    """
    frags: list[tuple[str, str]] = []

    def add(origin: str, text: str) -> None:
        if text and text.strip():
            frags.append((origin, text.strip().rstrip(".") + "."))

    if motion_override:
        add("override", motion_override)
    else:
        add("subject", subject_motion)
        add("environment", environment_motion)
        add("camera", CAMERA_PHRASING.get(camera_move, CAMERA_PHRASING["static"]))
        add("pacing", PACING_PHRASING.get(motion_pacing, PACING_PHRASING["slow"]))
        add("style", motion_language)
        # The anchor. Every model here is image-to-video, so the frame is
        # already correct; the one instruction that matters most is "do not
        # change it".
        add("anchor", "Keep the established composition, characters, clothing "
                      "and setting exactly as they appear in the first frame")

    positive = " ".join(t for _, t in frags)
    if supports_negative_prompt:
        negative = ", ".join(MOTION_NEGATIVE)
    else:
        # Folded in positively, the same accommodation the image composer
        # makes -- a capability difference, never a branch on a provider name.
        negative = ""
        positive += (" Continuous natural movement throughout, with no cuts "
                     "and no change of scene.")
        frags.append(("folded_negative", "no cuts, no scene change"))
    return ComposedMotion(positive=positive, negative=negative, fragments=frags)
