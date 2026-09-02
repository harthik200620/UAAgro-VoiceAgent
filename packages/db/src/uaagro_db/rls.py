"""Row-Level Security policies (§10, §17).

§10 is explicit that centre scoping must be enforced *in the database*, not only
in the API: application-layer-only scoping is one forgotten ``WHERE`` clause
from a breach.

Two details decide whether these policies actually hold, and both are easy to
get wrong:

1. **A table's owner bypasses RLS** unless the table is also ``FORCE``d. Every
   protected table below is FORCEd, so even the migrator role is subject to
   policy when it reads through a normal session.
2. **The application must not connect as the owner.** ``uaagro_app`` owns
   nothing; it is granted DML only. If the app connected as the schema owner,
   FORCE would be the only thing standing between a bug and a cross-centre
   read, and a single ``ALTER TABLE ... NO FORCE`` would remove it.

The policies read transaction-local GUCs bound by
:func:`uaagro_db.engine.bind_rls_context`.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import RLS_TABLES
from .roles import APP_ROLE, ORG_WIDE_ALL

#: Which column carries the centre a row belongs to, per protected table.
CENTRE_COLUMN: dict[str, str] = {
    "calls": "centre_id",
    "call_turns": "centre_id",
    "farmers": "assigned_centre_id",
    "tickets": "centre_id",
    "inventory": "centre_id",
    "orders": "centre_id",
}


def _org_wide_predicate() -> str:
    roles = ", ".join(f"'{role}'" for role in ORG_WIDE_ALL)
    return f"current_setting('app.role', true) IN ({roles})"


def _centre_predicate(column: str) -> str:
    """True when the row's centre is in the caller's granted centre list.

    ``app.centre_ids`` is a comma-separated list of UUIDs. It is parsed with
    ``string_to_array`` rather than compared as text so that an empty setting
    yields an empty array -- and therefore no rows -- instead of matching.

    A NULL centre (an unassigned farmer, a call before identification) is
    visible only to org-wide roles. Making it visible to everyone would be a
    quiet hole: an unassigned row would leak across every centre.
    """
    return (
        f"({column} IS NOT NULL AND {column}::text = ANY ("
        "  string_to_array(coalesce(nullif(current_setting('app.centre_ids', true), ''), ''), ',')"
        "))"
    )


def policy_predicate(table: str) -> str:
    """The USING/WITH CHECK expression for one protected table."""
    column = CENTRE_COLUMN[table]
    return f"({_org_wide_predicate()} OR {_centre_predicate(column)})"


def enable_rls_statements(tables: Sequence[str] = RLS_TABLES) -> list[str]:
    """DDL enabling and forcing RLS, plus one policy per protected table.

    ``tables`` defaults to every protected table, but a migration passes only
    the ones that revision creates: a policy on a table that does not exist yet
    is an error, and applying one to a table a later revision adds would make
    this revision non-reproducible.
    """
    statements: list[str] = []
    for table in tables:
        predicate = policy_predicate(table)
        statements.extend(
            [
                f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
                # Without FORCE, the owner reads everything and the tests would
                # pass against a system that leaks.
                f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
                f"DROP POLICY IF EXISTS {table}_centre_scope ON {table}",
                # Every interpolated value is a module constant or a key of
                # CENTRE_COLUMN -- no request data reaches here, and Postgres
                # accepts no bind parameter inside a policy body.
                f"CREATE POLICY {table}_centre_scope ON {table} "
                f"FOR ALL TO {APP_ROLE} "
                f"USING {predicate} WITH CHECK {predicate}",
            ]
        )
    return statements


def disable_rls_statements(tables: Sequence[str] = RLS_TABLES) -> list[str]:
    statements: list[str] = []
    for table in tables:
        statements.extend(
            [
                f"DROP POLICY IF EXISTS {table}_centre_scope ON {table}",
                f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
                f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
            ]
        )
    return statements


def create_app_role_statements(password: str) -> list[str]:
    """Create ``uaagro_app`` and grant it DML but no ownership.

    ``password`` is interpolated because Postgres does not accept a bind
    parameter in ``CREATE ROLE``. The caller must supply a value it trusts --
    this runs only from a migration, never from request-handling code.
    """
    escaped = password.replace("'", "''")
    return [
        # S608: CREATE ROLE accepts no bind parameter, so the password must be
        # interpolated. It is single-quote escaped above, the role name is a
        # module constant, and this runs only from a migration -- never from
        # request-handling code.
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{escaped}';
            END IF;
        END
        $$
        """,  # noqa: S608
        # The database name is not knowable at authoring time and GRANT does
        # not accept an expression, so it is resolved dynamically.
        f"""
        DO $$
        BEGIN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {APP_ROLE}', current_database());
        END
        $$
        """,
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
        # Tables created by later migrations inherit the same grants.
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}",
        # audit_log is append-only even for the application (§17).
        f"REVOKE UPDATE, DELETE ON audit_log FROM {APP_ROLE}",
    ]
