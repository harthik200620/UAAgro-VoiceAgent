"""``uaagro-db`` -- schema and seed management.

Wraps Alembic and the seed loader so the Makefile has a single, testable entry
point rather than a spread of alembic invocations with differing environments.

Output goes to stdout via ``sys.stdout.write`` rather than ``print``: §22 bans
``print`` in committed code, and this is an operator tool whose output is read
from a terminal, not a structured log stream.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from uaagro_domain.errors import UAAgroError
from uaagro_domain.settings import get_settings

from .engine import dispose_engines, get_migrator_engine, migrator_session
from .partitions import ensure_partitions
from .seeds import seed_all


def _out(message: str = "") -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _alembic_config() -> Config:
    """Alembic config rooted at the db package, wherever it is invoked from."""
    package_root = Path(__file__).resolve().parents[2]
    ini = package_root / "alembic.ini"
    if not ini.is_file():
        raise UAAgroError(
            f"alembic.ini was not found at {ini}.",
            remedy="Run from a checkout where packages/db/alembic.ini exists.",
        )
    config = Config(str(ini))
    config.set_main_option("script_location", str(package_root / "migrations"))
    return config


def _cmd_migrate(args: argparse.Namespace) -> int:
    config = _alembic_config()
    if args.sql:
        command.upgrade(config, args.revision, sql=True)
    else:
        command.upgrade(config, args.revision)
        _out(f"Migrated to {args.revision}.")
    return 0


def _cmd_downgrade(args: argparse.Namespace) -> int:
    command.downgrade(_alembic_config(), args.revision)
    _out(f"Downgraded to {args.revision}.")
    return 0


def _cmd_current(_: argparse.Namespace) -> int:
    command.current(_alembic_config(), verbose=True)
    return 0


def _cmd_revision(args: argparse.Namespace) -> int:
    command.revision(_alembic_config(), message=args.message, autogenerate=True)
    return 0


async def _seed() -> int:
    async with migrator_session() as session:
        report = await seed_all(session)
    _out("Seed complete.")
    _out(report.summary())
    _out(f"\n  total rows created: {report.total_created}")
    _out(
        "\n  NOTE: every crop_recommendations row is seeded as a DRAFT. The agent\n"
        "  cannot serve an unapproved dose (KB 5, spec 9). Approve them in the\n"
        "  admin panel before any advisory answer will work."
    )
    return 0


def _cmd_seed(_: argparse.Namespace) -> int:
    try:
        return asyncio.run(_seed())
    finally:
        asyncio.run(dispose_engines())


async def _partitions(behind: int, ahead: int) -> int:
    engine = get_migrator_engine()
    async with engine.begin() as connection:
        created = await ensure_partitions(
            connection, anchor=datetime.now(UTC).date(), behind=behind, ahead=ahead
        )
    if created:
        _out(f"Created {len(created)} partition(s):")
        for name in created:
            _out(f"  {name}")
    else:
        _out("All partitions already present.")
    return 0


def _cmd_partitions(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_partitions(args.behind, args.ahead))
    finally:
        asyncio.run(dispose_engines())


async def _verify_chain() -> int:
    """Walk the audit hash chain and report the first break (§17)."""
    from .audit import verify_chain

    async with migrator_session() as session:
        result = await verify_chain(session)
    if result.ok:
        _out(f"Audit chain intact across {result.rows_checked} row(s).")
        return 0
    _out(f"AUDIT CHAIN BROKEN at chain_index={result.broken_at}.")
    _out(
        "  Treat as a potential tampering incident: preserve the database, alert\n"
        "  the security owner, and follow the runbook before writing further rows."
    )
    return 1


def _cmd_verify_chain(_: argparse.Namespace) -> int:
    try:
        return asyncio.run(_verify_chain())
    finally:
        asyncio.run(dispose_engines())


async def _check() -> int:
    settings = get_settings()
    engine = get_migrator_engine()
    async with engine.connect() as connection:
        version = await connection.scalar(text("SHOW server_version"))
        extensions = await connection.execute(
            text("SELECT extname FROM pg_extension ORDER BY extname")
        )
        names = [row[0] for row in extensions]
    _out(f"  app_env      {settings.app_env}")
    _out(f"  postgres     {version}")
    _out(f"  extensions   {', '.join(names)}")
    missing = {"pgcrypto", "pg_trgm", "vector"} - set(names)
    if missing:
        _out(f"  MISSING      {', '.join(sorted(missing))} -- run `uaagro-db migrate`")
        return 1
    return 0


def _cmd_check(_: argparse.Namespace) -> int:
    try:
        return asyncio.run(_check())
    finally:
        asyncio.run(dispose_engines())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uaagro-db", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="apply migrations")
    migrate.add_argument("--revision", default="head")
    migrate.add_argument("--sql", action="store_true", help="emit SQL instead of applying")
    migrate.set_defaults(func=_cmd_migrate)

    downgrade = sub.add_parser("downgrade", help="revert migrations")
    downgrade.add_argument("revision")
    downgrade.set_defaults(func=_cmd_downgrade)

    sub.add_parser("current", help="show the applied revision").set_defaults(func=_cmd_current)

    revision = sub.add_parser("revision", help="autogenerate a migration")
    revision.add_argument("-m", "--message", required=True)
    revision.set_defaults(func=_cmd_revision)

    sub.add_parser("seed", help="load development seed data").set_defaults(func=_cmd_seed)

    partitions = sub.add_parser("partitions", help="create missing monthly partitions")
    partitions.add_argument("--behind", type=int, default=3)
    partitions.add_argument("--ahead", type=int, default=3)
    partitions.set_defaults(func=_cmd_partitions)

    sub.add_parser("verify-audit-chain", help="verify the audit hash chain").set_defaults(
        func=_cmd_verify_chain
    )
    sub.add_parser("check", help="report server version and extensions").set_defaults(
        func=_cmd_check
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except UAAgroError as exc:
        _out(f"error: {exc.message}")
        _out(f"  {exc.remedy}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
