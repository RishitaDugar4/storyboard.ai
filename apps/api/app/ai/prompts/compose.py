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
