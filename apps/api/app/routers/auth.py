"""Session endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Response, status

from ..auth import (AppSettings, CurrentUser, DbSession, authenticate,
                    clear_session, issue_session)
from ..schemas.api import LoginRequest, MeResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/session", status_code=status.HTTP_204_NO_CONTENT)
async def login(body: LoginRequest, response: Response, session: DbSession,
                settings: AppSettings) -> Response:
    user = await authenticate(session, body.email, body.passphrase)
    issue_session(response, user, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, settings: AppSettings) -> Response:
    clear_session(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


me_router = APIRouter(prefix="/api/v1", tags=["auth"])


@me_router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    return MeResponse(id=user.id, email=user.email,
                      display_name=user.display_name,
                      last_login_at=user.last_login_at)
