"""Project CRUD -- the one end-to-end path M0 proves."""
from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, select

from ..auth import CurrentUser, DbSession
from ..db.models import Project
from ..errors import NotFound
from ..schemas.api import (ProjectCreate, ProjectList, ProjectRead,
                           ProjectUpdate)

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


async def _owned(session: DbSession, user, pid: uuid.UUID) -> Project:
    project = await session.get(Project, pid)
    # Same response for "absent" and "someone else's": ownership is not a fact
    # worth leaking, even in a single-tenant app.
    if project is None or project.owner_id != user.id:
        raise NotFound(f"project {pid} not found")
    return project


@router.get("", response_model=ProjectList)
async def list_projects(session: DbSession, user: CurrentUser) -> ProjectList:
    rows = (await session.execute(
        select(Project).where(Project.owner_id == user.id)
        .order_by(Project.updated_at.desc()))).scalars().all()
    total = (await session.execute(
        select(func.count()).select_from(Project)
        .where(Project.owner_id == user.id))).scalar_one()
    return ProjectList(items=[ProjectRead.model_validate(r) for r in rows],
                       total=total)


@router.post("", response_model=ProjectRead,
             status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, session: DbSession,
                         user: CurrentUser) -> ProjectRead:
    project = Project(
        owner_id=user.id, title=body.title, aspect_ratio=body.aspect_ratio,
        style_preset=body.style_preset, budget_cents=body.budget_cents,
        image_size="1920x1080" if body.aspect_ratio == "16:9" else "1080x1920",
    )
    session.add(project)
    await session.flush()
    await session.refresh(project)
    return ProjectRead.model_validate(project)


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: uuid.UUID, session: DbSession,
                      user: CurrentUser) -> ProjectRead:
    return ProjectRead.model_validate(await _owned(session, user, project_id))


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(project_id: uuid.UUID, body: ProjectUpdate,
                         session: DbSession, user: CurrentUser) -> ProjectRead:
    project = await _owned(session, user, project_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    await session.flush()
    await session.refresh(project)
    return ProjectRead.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, session: DbSession,
                         user: CurrentUser) -> None:
    await session.delete(await _owned(session, user, project_id))


@router.post("/{project_id}/share", response_model=ProjectRead)
async def create_share_link(project_id: uuid.UUID, session: DbSession,
                            user: CurrentUser) -> ProjectRead:
    project = await _owned(session, user, project_id)
    if not project.share_token:
        project.share_token = secrets.token_urlsafe(24)
        await session.flush()
        await session.refresh(project)
    return ProjectRead.model_validate(project)
