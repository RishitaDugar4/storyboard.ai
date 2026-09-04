"""Per-user authentication.

Accounts are created from the CLI, not by signup: this app has exactly the
users it was built for, and a registration flow would be a surface with no
purpose. Each user owns their own projects, and ownership is checked on every
read -- two people sharing the instance never see each other's work.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Cookie, Depends, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db.models import User
from .db.session import get_session
from .errors import Unauthorized
from .security import hash_passphrase, verify_passphrase

_SALT = "hbz-session-v1"


def _serializer(s: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(s.session_secret, salt=_SALT)


def issue_session(response: Response, user: User, s: Settings) -> None:
    token = _serializer(s).dumps({"uid": str(user.id)})
    response.set_cookie(
        s.session_cookie_name, token, max_age=s.session_max_age_s,
        httponly=True, secure=s.session_cookie_secure, samesite="lax", path="/",
    )


def clear_session(response: Response, s: Settings) -> None:
    response.delete_cookie(s.session_cookie_name, path="/")


async def authenticate(session: AsyncSession, email: str,
                       passphrase: str) -> User:
    """Verify credentials, or raise the same error either way.

    Email lookup is case-insensitive; the failure message never distinguishes
    "no such account" from "wrong passphrase", so it cannot be used to
    enumerate who has an account here.
    """
    user = (await session.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )).scalar_one_or_none()

    if user is None or not user.is_active:
        # Hash anyway so a missing account is not measurably faster than a
        # wrong passphrase.
        verify_passphrase(passphrase, "scrypt$32768$8$1$00$00")
        raise Unauthorized("incorrect email or passphrase")
    if not verify_passphrase(passphrase, user.passphrase_hash):
        raise Unauthorized("incorrect email or passphrase")

    user.last_login_at = datetime.now(timezone.utc)
    return user


async def create_user(session: AsyncSession, *, email: str, display_name: str,
                      passphrase: str) -> User:
    user = User(email=email.strip().lower(), display_name=display_name.strip(),
                passphrase_hash=hash_passphrase(passphrase))
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

    user = await session.get(User, uuid.UUID(data["uid"]))
    if user is None or not user.is_active:
        raise Unauthorized("account is no longer active")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
