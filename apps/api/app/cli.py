#!/usr/bin/env python3
"""Account administration.

    python -m app.cli user add zee@example.com "Zee"
    python -m app.cli user list
    python -m app.cli user passwd zee@example.com
    python -m app.cli user disable zee@example.com

Accounts are created here rather than through a signup page, because this app
has exactly the users it was built for.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from .auth import create_user
from .db.models import User
from .db.session import dispose_engine, get_sessionmaker
from .security import generate_passphrase, hash_passphrase


async def _add(email: str, name: str, passphrase: str | None) -> int:
    generated = passphrase is None
    passphrase = passphrase or generate_passphrase()
    async with get_sessionmaker()() as s:
        exists = (await s.execute(select(User).where(
            func.lower(User.email) == email.lower()))).scalar_one_or_none()
        if exists:
            print(f"error: {email} already has an account", file=sys.stderr)
            return 1
        user = await create_user(s, email=email, display_name=name,
                                 passphrase=passphrase)
        await s.commit()
    print(f"  created {user.email}  ({user.display_name})")
    if generated:
        print(f"  passphrase: {passphrase}")
        print("  This is shown once. Write it down or send it to them now.")
    return 0


async def _list() -> int:
    async with get_sessionmaker()() as s:
        users = (await s.execute(select(User).order_by(User.created_at))).scalars()
        rows = list(users)
    if not rows:
        print("  no accounts yet -- create one with: python -m app.cli user add ...")
        return 0
    print(f"  {'email':32} {'name':18} {'active':7} last login")
    for u in rows:
        last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never"
        print(f"  {u.email:32} {u.display_name:18} "
              f"{'yes' if u.is_active else 'no':7} {last}")
    return 0


async def _passwd(email: str, passphrase: str | None) -> int:
    generated = passphrase is None
    passphrase = passphrase or generate_passphrase()
    async with get_sessionmaker()() as s:
        user = (await s.execute(select(User).where(
            func.lower(User.email) == email.lower()))).scalar_one_or_none()
        if not user:
            print(f"error: no account for {email}", file=sys.stderr)
            return 1
        user.passphrase_hash = hash_passphrase(passphrase)
        await s.commit()
    print(f"  passphrase reset for {email}")
    if generated:
        print(f"  passphrase: {passphrase}")
    return 0


async def _set_active(email: str, active: bool) -> int:
    async with get_sessionmaker()() as s:
        user = (await s.execute(select(User).where(
            func.lower(User.email) == email.lower()))).scalar_one_or_none()
        if not user:
            print(f"error: no account for {email}", file=sys.stderr)
            return 1
        user.is_active = active
        await s.commit()
    print(f"  {email} {'enabled' if active else 'disabled'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="group", required=True)
    user = sub.add_parser("user").add_subparsers(dest="action", required=True)

    add = user.add_parser("add")
    add.add_argument("email"); add.add_argument("name")
    add.add_argument("--passphrase", help="omit to generate one")
    user.add_parser("list")
    pw = user.add_parser("passwd")
    pw.add_argument("email"); pw.add_argument("--passphrase")
    for verb in ("disable", "enable"):
        user.add_parser(verb).add_argument("email")

    args = ap.parse_args(argv)

    async def run() -> int:
        try:
            match args.action:
                case "add":
                    return await _add(args.email, args.name, args.passphrase)
                case "list":
                    return await _list()
                case "passwd":
                    return await _passwd(args.email, args.passphrase)
                case "disable":
                    return await _set_active(args.email, False)
                case "enable":
                    return await _set_active(args.email, True)
            return 2
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
