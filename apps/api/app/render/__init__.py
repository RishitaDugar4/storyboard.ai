"""Standalone video renderer.

Imports nothing from db/, services/ or ai/ -- it consumes a Timeline and file
paths. That constraint is what keeps it testable from fixtures, runnable from
the CLI, and unchanged when a shot's source switches between a still and a
generated clip.
"""
from .pipeline import RenderResult, render
from .preflight import PreflightReport, preflight
from .timeline import Clip, Profile, Source, SourceKind, Timeline

__all__ = ["render", "RenderResult", "preflight", "PreflightReport",
           "Timeline", "Clip", "Source", "SourceKind", "Profile"]
