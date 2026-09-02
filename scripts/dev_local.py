"""Bring up a local stack without Docker.

`make dev` runs the §20 compose stack and is the supported path. This is the
fallback for a machine with no Docker daemon: it starts the same embedded
PostgreSQL 16 + pgvector the test suite uses, migrates it, seeds it, and writes
the environment the API and admin panel need.

It is a **development convenience and nothing else.** The database lives under
`.localdev/`, the credentials below are fixed and public, and `APP_ENV=local`
keeps `verify_production_readiness()` from demanding the real vendor keys. None
of that is safe anywhere but a laptop, which is why the compose stack -- with
real secrets from the environment -- remains the deployment path.

Run it once; it leaves Postgres running and exits:

    uv run python scripts/dev_local.py

Then start the API and the panel against `.localdev/env`.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from urllib.parse import quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / ".localdev"
DB_NAME = "uaagro_local"
APP_ROLE = "uaagro_app"
APP_PASSWORD = "uaagro_local_pw"


def as_asyncpg(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+asyncpg://", 1)


def with_credentials(uri: str, user: str, password: str) -> str:
    scheme, _, rest = uri.partition("://")
    _, _, hostpart = rest.rpartition("@") if "@" in rest else ("", "", rest)
    return f"{scheme}://{quote(user)}:{quote(password)}@{hostpart}"


def run_cli(command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "uaagro_db.cli", command],
        env={**os.environ, **env, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def main() -> int:
    import pgserver

    STATE.mkdir(exist_ok=True)
    print("starting embedded PostgreSQL 16 + pgvector ...", flush=True)
    # `cleanup_mode=None` leaves the server running after this process exits,
    # which is the whole point -- the API needs it afterwards.
    server = pgserver.get_server(STATE / "pgdata", cleanup_mode=None)

    existing = server.psql(
        f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'"
    ).strip()
    if "1" not in existing:
        server.psql(f"CREATE DATABASE {DB_NAME}")

    base = server.get_uri(database=DB_NAME)
    migrator_dsn = as_asyncpg(base)
    app_dsn = as_asyncpg(with_credentials(base, APP_ROLE, APP_PASSWORD))

    env = {
        "APP_ENV": "development",
        "DATABASE_URL": app_dsn,
        "DATABASE_URL_MIGRATOR": migrator_dsn,
    }

    print("applying migrations ...", flush=True)
    migrated = run_cli("migrate", env)
    if migrated.returncode != 0:
        sys.stderr.write(migrated.stdout + migrated.stderr)
        return 1

    print("seeding ...", flush=True)
    seeded = run_cli("seed", env)
    if seeded.returncode != 0:
        sys.stderr.write(seeded.stdout + seeded.stderr)
        return 1

    # Written as a file rather than exported: this process exits, and the API
    # and the panel are started separately.
    lines = [
        "APP_ENV=development",
        f"DATABASE_URL={app_dsn}",
        f"DATABASE_URL_MIGRATOR={migrator_dsn}",
        "LOG_JSON=false",
        "LOG_LEVEL=INFO",
    ]
    (STATE / "env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (STATE / "dsn.json").write_text(
        json.dumps({"app": app_dsn, "migrator": migrator_dsn}, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"  database  {DB_NAME} (data in .localdev/pgdata)")
    print(f"  env       {STATE / 'env'}")
    print()
    print("  next: start the API, then the admin panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
