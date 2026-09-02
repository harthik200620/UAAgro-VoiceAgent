"""The restore rehearsal (§21 Phase 8, §18).

§21's Phase 8 gate requires a restore to have been *performed and documented*,
not merely configured, and the runbook says why: a backup nobody has restored
is a hypothesis.

This is that rehearsal, automated. It dumps the seeded database, restores it
into a second one, and then checks the four things a restore is actually for --
none of which is "the command exited zero":

1. **The data came back.** Row counts match, table for table.
2. **The RLS policies came with it.** A dump taken with the wrong flags
   restores the rows and drops the policies, and the result looks perfect: same
   tables, same counts, and a centre manager who can now read every centre's
   calls. That is the failure worth catching, because it is invisible in every
   check except this one.
3. **The audit chain still verifies across the restore boundary.** §17 makes
   the chain tamper-evident; a restore that reordered or renumbered rows would
   break it, and an auditor would discover that months later.
4. **The partitions came back as partitions.** `calls` is range-partitioned
   monthly (§10); restoring it as a plain table works fine until the next
   insert lands outside any partition -- which happens on the first of the
   month, in the audio path.

Running against the embedded Postgres means this runs in CI on every change,
rather than being a quarterly exercise somebody remembers.
"""

from __future__ import annotations

import pathlib
import subprocess
import time
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

#: Tables whose row counts are compared. Not every table -- these are the ones
#: whose loss would be noticed by a farmer or a regulator.
CHECKED_TABLES = (
    "organizations",
    "centres",
    "users",
    "farmers",
    "products",
    "product_variants",
    "inventory",
    "crop_recommendations",
    "kb_documents",
    "audit_log",
)


def _binaries() -> pathlib.Path:
    import pgserver

    return pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin"


def _run(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    import os

    # Every element of `command` is either a path to a binary shipped inside
    # the installed `pgserver` package or a field parsed out of the test
    # harness's own DSN. Nothing here comes from a request, a fixture file or
    # the network, and the list form never reaches a shell.
    return subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


@pytest.fixture
def dsn(embedded_pg) -> str:  # type: ignore[no-untyped-def]
    """The owner DSN.

    A restore needs CREATE DATABASE and it recreates policies and ownership,
    so it runs as the owner -- which is also who runs it during a real
    incident. The application role deliberately cannot do any of that, and
    a rehearsal performed as the wrong role would prove the wrong thing.
    """
    return str(embedded_pg.migrator_dsn)


async def test_a_dump_restores_into_a_working_database(  # type: ignore[no-untyped-def]
    app_engine, embedded_pg, dsn: str, record_property
) -> None:
    """The rehearsal §21 Phase 8 asks for.

    Recorded rather than merely asserted: the wall-clock restore time is the
    number that matters at 3am, and a test that checked correctness without
    reporting duration would leave the operator guessing during an incident.
    """
    binaries = _binaries()
    pg_dump = binaries / "pg_dump.exe"
    pg_restore = binaries / "pg_restore.exe"
    if not pg_dump.exists():  # pragma: no cover -- non-Windows toolchain
        pg_dump = binaries / "pg_dump"
        pg_restore = binaries / "pg_restore"
    if not pg_dump.exists():
        pytest.skip("no pg_dump in the embedded Postgres distribution")

    parts = urlsplit(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    source_db = (parts.path or "/postgres").lstrip("/")
    restored_db = f"{source_db}_restore_rehearsal"
    env = {"PGPASSWORD": parts.password or ""}
    conn_args = [
        "--host",
        parts.hostname or "localhost",
        "--port",
        str(parts.port or 5432),
        "--username",
        parts.username or "postgres",
        "--no-password",
    ]

    admin_url = dsn.rsplit("/", 1)[0] + "/postgres"
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{restored_db}"'))
            await connection.execute(text(f'CREATE DATABASE "{restored_db}"'))
    finally:
        await admin.dispose()

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dump_path = pathlib.Path(tmp) / "uaagro.dump"

        # Custom format, not plain SQL. It is what `pg_restore` needs for
        # selective restore during a real incident, and it is what the runbook
        # documents -- rehearsing a different format than the one on the shelf
        # rehearses nothing.
        dumped = _run(
            [str(pg_dump), *conn_args, "--format=custom", "--file", str(dump_path), source_db],
            env=env,
        )
        assert dumped.returncode == 0, dumped.stderr[-2000:]
        assert dump_path.stat().st_size > 0

        started = time.perf_counter()
        restored = _run(
            [
                str(pg_restore),
                *conn_args,
                "--dbname",
                restored_db,
                "--no-owner",
                str(dump_path),
            ],
            env=env,
        )
        elapsed = time.perf_counter() - started

    # pg_restore exits non-zero on warnings it recovered from. What matters is
    # whether the database below is usable, so the return code is recorded
    # rather than asserted -- and the checks that follow are the real gate.
    #
    # Recorded as a property rather than printed: it lands in the CI report, so
    # the restore time becomes a series somebody can watch rather than a line
    # that scrolled past. §21's gate wants this documented, and the runbook is
    # blunt that the restore *time* is the number nobody measures until they
    # need it.
    record_property("restore_seconds", round(elapsed, 2))
    record_property("pg_restore_exit", restored.returncode)

    source = async_sessionmaker(app_engine, expire_on_commit=False)
    target_engine = create_async_engine(dsn.rsplit("/", 1)[0] + f"/{restored_db}")
    try:
        target = async_sessionmaker(target_engine, expire_on_commit=False)

        async with source() as a, target() as b:
            await a.execute(text("SELECT set_config('app.role','ops_manager',true)"))
            await b.execute(text("SELECT set_config('app.role','ops_manager',true)"))

            # 1. The data came back.
            for table in CHECKED_TABLES:
                before = await a.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                after = await b.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                assert after == before, f"{table}: {before} rows became {after}"

            # 2. The policies came with it. This is the check that catches a
            # restore which looks perfect and leaks every centre's calls.
            policies_before = await a.scalar(
                text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
            )
            policies_after = await b.scalar(
                text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
            )
            assert policies_after == policies_before, (
                f"{policies_before} RLS policies became {policies_after}; "
                "the restored database would leak across centres"
            )

            forced = await b.scalar(
                text(
                    "SELECT count(*) FROM pg_class WHERE relrowsecurity "
                    "AND relforcerowsecurity"
                )
            )
            assert forced and forced > 0, "RLS restored without FORCE is RLS the owner bypasses"

            # 3. The partitions came back as partitions.
            partitioned = await b.scalar(
                text(
                    "SELECT count(*) FROM pg_class c "
                    "JOIN pg_partitioned_table p ON p.partrelid = c.oid"
                )
            )
            assert partitioned and partitioned > 0, (
                "calls restored as a plain table; the next insert past the "
                "month boundary would fail in the audio path"
            )

            # 4. The audit chain still verifies across the restore boundary.
            from uaagro_db.audit import verify_chain

            verification = await verify_chain(b)
            assert verification.ok, (
                f"the audit chain broke at {verification.broken_at} during "
                f"restore: {verification.reason}"
            )
    finally:
        await target_engine.dispose()
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{restored_db}"'))
        finally:
            await admin.dispose()
