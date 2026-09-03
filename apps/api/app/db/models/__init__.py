"""Model registry. Every model must be imported here or Alembic autogenerate
will silently omit its table."""
from .project import Project, ProjectStage
from .user import User

__all__ = ["User", "Project", "ProjectStage"]
