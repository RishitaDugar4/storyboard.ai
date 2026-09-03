"""Passphrase gate + signed session cookie.

A gate, not a product surface: one shared passphrase mints a signed cookie
naming the single owner row. No password hashing, no registration, no reset
flow -- all of which would be real work for zero user-facing value here
(ARCHITECTURE section 15).
"""
from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Cookie, Depends, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db.models import User
from .db.session import get_session
from .errors import Unauthorized

_SALT = "hbz-session-v1"


def _serializer(s: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(s.session_secret, salt=_SALT)


def verify_passphrase(candidate: str, s: Settings) -> bool:
    # Constant-time: a timing oracle on a shared secret is cheap to avoid.
    return hmac.compare_digest(candidate.encode(), s.app_passphrase.encode())


def issue_session(response: Response, user: User, s: Settings) -> None:
    token = _serializer(s).dumps({"uid": str(user.id)})
    response.set_cookie(
        s.session_cookie_name, token, max_age=s.session_max_age_s,
        httponly=True, secure=s.session_cookie_secure, samesite="lax", path="/",
    )


def clear_session(response: Response, s: Settings) -> None:
    response.delete_cookie(s.session_cookie_name, path="/")


async def ensure_owner(session: AsyncSession, s: Settings) -> User:
    """Return the single owner row, creating it on first use."""
    user = (await session.execute(
        select(User).where(User.email == s.owner_email))).scalar_one_or_none()
    if user is None:
        user = User(email=s.owner_email, display_name=s.owner_name)
        session.add(user)
        await session.flush()
    return user


async def current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    hbz_session: Annotated[str | None, Cookie(alias="hbz_session")] = None,
) -> User:
    if not hbz_session:
        raise Unauthorized("no session cookie")
    try:
        data = _serializer(settings).loads(
            hbz_session, max_age=settings.session_max_age_s)
    except SignatureExpired:
        raise Unauthorized("session expired") from None
    except BadSignature:
        raise Unauthorized("invalid session") from None

    user = await session.get(User, __import__("uuid").UUID(data["uid"]))
    if user is None:
        raise Unauthorized("session refers to a missing user")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
