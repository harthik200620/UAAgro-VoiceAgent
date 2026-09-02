"""An embedded Postgres for the integration tests.

Docker is the documented way to run the stack (§20), but requiring it to run the
test suite would mean the RLS, seed and authz-matrix tests -- the ones that
actually prove §10 and §17 hold -- get skipped on any machine without it. Those
are the tests most worth running.

``pgserver`` ships PostgreSQL **16.2** with **pgvector** as a pip wheel, needs no
root and no daemon, and matches the production major version. It does not ship
contrib, so ``pg_trgm`` is absent; the migration handles that by skipping the two
trigram indexes with a warning, and those indexes are a search-quality
optimisation rather than a correctness requirement.

**This is test tooling only.** Production remains Postgres 16 on RDS, and the
compose stack is unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

APP_ROLE = "uaagro_app"
APP_PASSWORD = "test_app_pw"
DB_NAME = "uaagro_test"


@dataclass(frozen=True, slots=True)
class EmbeddedPostgres:
    """A running embedded server plus the two DSNs the platform needs."""

    #: Superuser DSN. Alembic and the seed loader use this.
    migrator_dsn: str
    #: Application-role DSN. RLS applies to this connection, not the migrator's.
    app_dsn: str
    #: True when pg_trgm was available, so tests can assert accordingly.
    has_trgm: bool


def start(tmp_path: Path) -> tuple[object, EmbeddedPostgres]:
    """Start a server, create the database and the app role, and migrate.

    Returns the server handle (so the caller can stop it) and the DSNs.
    """
    import pgserver

    server = pgserver.get_server(tmp_path / "pgdata", cleanup_mode=None)

    # The default database is `postgres`; create a dedicated one so a dropped
    # test database never takes the cluster's own catalogue with it.
    _psql(server, f"DROP DATABASE IF EXISTS {DB_NAME}")
    _psql(server, f"CREATE DATABASE {DB_NAME}")

    base = server.get_uri(database=DB_NAME)
    migrator_dsn = _as_asyncpg(base)
    app_dsn = _as_asyncpg(_with_credentials(base, APP_ROLE, APP_PASSWORD))

    has_trgm = _migrate(migrator_dsn=migrator_dsn, app_dsn=app_dsn)
    return server, EmbeddedPostgres(migrator_dsn=migrator_dsn, app_dsn=app_dsn, has_trgm=has_trgm)


def _psql(server: object, statement: str) -> str:
    result: str = server.psql(statement)  # type: ignore[attr-defined]
    return result


def _as_asyncpg(uri: str) -> str:
    """SQLAlchemy needs the driver named in the scheme."""
    return uri.replace("postgresql://", "postgresql+asyncpg://", 1)


def _with_credentials(uri: str, user: str, password: str) -> str:
    """Rewrite the userinfo portion of a DSN."""
    scheme, _, rest = uri.partition("://")
    _, _, hostpart = rest.rpartition("@") if "@" in rest else ("", "", rest)
    return f"{scheme}://{quote(user)}:{quote(password)}@{hostpart}"


def _migrate(*, migrator_dsn: str, app_dsn: str) -> bool:
    """Run ``uaagro-db migrate`` against the embedded server.

    Runs as a subprocess with an explicit environment rather than in-process:
    Alembic reads settings at import time and caches engines, and a test that
    mutated this process's environment would leak into every later test.

    Returns whether pg_trgm was available.
    """
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": app_dsn,
        "DATABASE_URL_MIGRATOR": migrator_dsn,
        "PYTHONUTF8": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "uaagro_db.cli", "migrate"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Migration failed against the embedded Postgres.\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return "pg_trgm is unavailable" not in completed.stderr


def seed(*, migrator_dsn: str, app_dsn: str) -> str:
    """Run ``uaagro-db seed``. Returns combined output for assertions."""
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": app_dsn,
        "DATABASE_URL_MIGRATOR": migrator_dsn,
        "PYTHONUTF8": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "uaagro_db.cli", "seed"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Seed failed.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout + completed.stderr
