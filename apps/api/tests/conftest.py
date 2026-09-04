"""Point every test at a throwaway database, and refuse to run anywhere else.

The suite truncates tables between tests. Pointed at the development database
-- which is exactly where DATABASE_URL points during normal work -- that
deletes real projects: the stories, stills and renders this application exists
to keep. A dropped fixture is cheap; a deleted film is not.

The test database is derived from the configured one by appending `_test`, and
created on demand. The name is then asserted, so a misconfiguration stops the
run instead of quietly clearing live data.
"""
from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

DEFAULT = "postgresql+asyncpg://localhost/hbday_zee_dev"


def _test_url(url: str) -> str:
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    if name.endswith("_test"):
        return url
    return urlunsplit(parts._replace(path=f"/{name}_test"))


def _psql_url(url: str) -> str:
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql://")


url = _test_url(os.environ.get("TEST_DATABASE_URL")
                or os.environ.get("DATABASE_URL")
                or DEFAULT)
name = urlsplit(url).path.lstrip("/")
assert name.endswith("_test"), (
    f"refusing to run destructive tests against {name!r}")

os.environ["DATABASE_URL"] = url

# Create and migrate on demand so a fresh checkout needs no setup step.
_created = subprocess.run(["createdb", name], capture_output=True, text=True)
if _created.returncode != 0 and "already exists" not in _created.stderr:
    print(_created.stderr, file=sys.stderr)

_migrated = subprocess.run(
    [sys.executable, "-m", "alembic", "upgrade", "head"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    capture_output=True, text=True,
    env={**os.environ, "DATABASE_URL": url})
if _migrated.returncode != 0:
    raise RuntimeError(f"could not migrate {name}:\n{_migrated.stderr}")
