"""Which adapter serves each capability, from configuration."""
from __future__ import annotations

import os
from functools import lru_cache

from .ports import TextPort


@lru_cache(maxsize=1)
def get_text_port() -> TextPort:
    provider = os.getenv("AI_TEXT_PROVIDER", "gemini").lower()
    if provider in ("fake", "none"):
        from .adapters.fakes import FakeTextAdapter
        return FakeTextAdapter()
    if provider in ("gemini", "google"):
        from .adapters.gemini_text import DEFAULT_MODEL, GeminiTextAdapter
        return GeminiTextAdapter(
            model=os.getenv("AI_TEXT_MODEL", DEFAULT_MODEL),
            api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    if provider == "anthropic":
        from .adapters.anthropic_text import DEFAULT_MODEL, AnthropicTextAdapter
        return AnthropicTextAdapter(model=os.getenv("AI_TEXT_MODEL", DEFAULT_MODEL))
    raise ValueError(
        f"unknown AI_TEXT_PROVIDER {provider!r}; expected one of: "
        "gemini, anthropic, fake")


@lru_cache(maxsize=1)
def get_image_port():
    provider = os.getenv("AI_IMAGE_PROVIDER", "fal").lower()
    if provider in ("fake", "none"):
        from .adapters.fakes import FakeImageAdapter
        return FakeImageAdapter()
    if provider == "fal":
        from .adapters.fal_image import DEFAULT_MODEL, FalImageAdapter
        return FalImageAdapter(os.getenv("FAL_KEY", ""),
                               model=os.getenv("AI_IMAGE_MODEL", DEFAULT_MODEL))
    raise ValueError(
        f"unknown AI_IMAGE_PROVIDER {provider!r}; expected one of: fal, fake")


#: Requests per minute we allow ourselves for speech, below the provider's own
#: ceiling. Gemini's Tier 1 TTS limit is 10/min and `narration:generate_all`
#: fans out one job per line, so the default leaves headroom for a retry or a
#: manual regenerate landing in the same window. 0 disables pacing.
DEFAULT_SPEECH_RPM = 8


@lru_cache(maxsize=1)
def get_speech_port():
    provider = os.getenv("AI_SPEECH_PROVIDER", "gemini").lower()
    if provider in ("fake", "none"):
        from .adapters.fakes import FakeSpeechAdapter
        return FakeSpeechAdapter()
    if provider in ("gemini", "google"):
        from .adapters.gemini_speech import DEFAULT_MODEL, GeminiSpeechAdapter
        port = GeminiSpeechAdapter(
            os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
            model=os.getenv("AI_SPEECH_MODEL", DEFAULT_MODEL))
        return _paced(port, "speech", os.getenv("AI_SPEECH_RPM"),
                      DEFAULT_SPEECH_RPM)
    raise ValueError(
        f"unknown AI_SPEECH_PROVIDER {provider!r}; expected: gemini, fake")


def _paced(port, capability: str, rpm_env: str | None, default_rpm: int):
    """Wrap a port in the shared rate limiter, unless it is switched off.

    Keyed by provider and model because quotas are per model: pacing on a
    shared counter would throttle the text model on the speech model's traffic.
    """
    rpm = default_rpm if rpm_env is None else int(rpm_env)
    if rpm <= 0:
        return port
    from .ratelimit import RateLimitedSpeech, RateLimiter
    return RateLimitedSpeech(
        port, RateLimiter(f"{capability}:{port.provider}:{port.model}", rpm))


def reset() -> None:
    get_text_port.cache_clear()
    get_image_port.cache_clear()
    get_speech_port.cache_clear()
