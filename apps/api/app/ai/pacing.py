"""Narration pacing constants.

These are OURS -- an authorial model of how fast a narrator speaks -- not any
provider's. Provider duration grids are resolved separately at motion-plan
time; a storyboard must never encode one vendor's clip lengths.

Recalibrate WORDS_PER_SECOND against real TTS output at M5: it is currently an
estimate, and every word budget in the app derives from it.
"""
from __future__ import annotations

WORDS_PER_SECOND = 2.5      # measured narration pace; recalibrate at M5
PAD_S = 0.9                 # 0.3s lead-in + 0.6s tail
MIN_SHOT_S = 2.5
MAX_SHOT_S = 12.0


def word_budget(duration_s: float) -> int:
    """How many narrated words comfortably fit a shot of this length."""
    return max(0, int((duration_s - PAD_S) * WORDS_PER_SECOND))


def speech_seconds(word_count: int) -> float:
    return word_count / WORDS_PER_SECOND


def required_seconds(word_count: int) -> float:
    """Speech plus the silence that makes it land."""
    return speech_seconds(word_count) + PAD_S if word_count else 0.0
