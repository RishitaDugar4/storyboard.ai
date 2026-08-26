"""Adapter registry: routes a catalogue entry to the port that serves it.

The only package in the system permitted to name a provider.
"""
from __future__ import annotations

import os

from adapters.base import ErrorKind, ProviderError, VideoPort
from catalog import get as get_caps


def build_registry(*, fake: bool, fake_speed: float = 10.0,
                   fake_failure_rate: float = 0.0) -> dict[str, VideoPort]:
    """Construct only the adapters whose credentials are present."""
    if fake:
        from adapters.fake import FakeVideoAdapter
        shared = FakeVideoAdapter(speed=fake_speed, failure_rate=fake_failure_rate)
        return {"fal": shared, "veo": shared, "task_api": shared}

    registry: dict[str, VideoPort] = {}
    if key := os.getenv("FAL_KEY"):
        from adapters.fal import FalVideoAdapter
        registry["fal"] = FalVideoAdapter(key)
    if key := os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        from adapters.veo import VeoVideoAdapter
        registry["veo"] = VeoVideoAdapter(
            key, person_generation=os.getenv("VEO_PERSON_GENERATION", "allow_adult"))
    task_keys = {p: os.getenv(f"{p.upper()}_API_KEY", "")
                 for p in ("runway", "luma")}
    if any(task_keys.values()):
        from adapters.task_api import TaskApiVideoAdapter
        registry["task_api"] = TaskApiVideoAdapter(task_keys)
    return registry


def port_for(registry: dict[str, VideoPort], model_key: str) -> VideoPort:
    caps = get_caps(model_key)
    port = registry.get(caps.adapter)
    if port is None:
        raise ProviderError(
            ErrorKind.AUTH, "adapter_unavailable",
            f"{caps.display_name} needs the '{caps.adapter}' adapter, which has "
            f"no credentials configured. See tools/bakeoff/README.md.")
    return port
