"""Model registry.

Every model must be imported here or Alembic autogenerate silently omits its
table -- a failure mode that only shows up in production.
"""
from .assets import (Asset, AssetKind, AssetSource, Render,
                     RenderProfile)
from .content import (CameraMove, Character, Location, MotionMode,
                      NarrationLine, Scene, Shot, ShotType, StoryAnalysisDoc,
                      StoryboardDoc, StoryInput)
from .jobs import ACTIVE, TERMINAL, AICall, Job, JobEvent, JobStatus
from .project import Project, ProjectStage
from .user import User

__all__ = [
    "User", "Project", "ProjectStage",
    "Asset", "AssetKind", "AssetSource", "Render", "RenderProfile",
    "StoryInput", "StoryAnalysisDoc", "StoryboardDoc",
    "Character", "Location", "Scene", "Shot", "NarrationLine",
    "ShotType", "CameraMove", "MotionMode",
    "Job", "JobEvent", "JobStatus", "AICall", "ACTIVE", "TERMINAL",
]
