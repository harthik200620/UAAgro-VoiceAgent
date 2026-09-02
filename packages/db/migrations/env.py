"""Alembic environment.

Runs migrations as the **schema owner** (``DATABASE_URL_MIGRATOR``), never as
the application role. The application role owns nothing, which is what makes
the RLS policies in :mod:`uaagro_db.rls` meaningful.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from uaagro_db.models import Base
from uaagro_domain.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Migrator URL from the environment, with the Alembic override honoured.

    ``-x url=...`` lets the test harness point a migration run at a throwaway
    database without mutating the process environment.
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return str(override)
    return get_settings().database_url_migrator


def _include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Keep autogenerate away from objects it does not own.

    Monthly partitions are created by :mod:`uaagro_db.partitions`, not by a
    migration. Without this filter, every autogenerate run would try to drop
    the partitions it found and recreate them as tables.
    """
    if type_ == "table" and name is not None:
        parents = ("calls_p", "call_turns_p", "call_events_p", "dtmf_events_p")
        if name.startswith(parents):
            return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection (``--sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        # Deterministic constraint names come from the metadata naming
        # convention; without this, autogenerate cannot match existing ones.
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
