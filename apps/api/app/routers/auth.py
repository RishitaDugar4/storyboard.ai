"""Session endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Response, status

from ..auth import (AppSettings, CurrentUser, DbSession, clear_session,
                    ensure_owner, issue_session, verify_passphrase)
from ..errors import Unauthorized
from ..schemas.api import LoginRequest, MeResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/session", status_code=status.HTTP_204_NO_CONTENT)
async def login(body: LoginRequest, response: Response, session: DbSession,
                settings: AppSettings) -> Response:
    if not verify_passphrase(body.passphrase, settings):
        raise Unauthorized("incorrect passphrase")
    user = await ensure_owner(session, settings)
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
                      display_name=user.display_name)
