"""Grant background jobs org-wide read through a ``system`` application role.

Revision ID: 0006
Revises: 0005
Created: Phase 8

RLS policies read ``current_setting('app.role', true)``. When nothing has set
it, that returns NULL, the ``IN`` test is NULL, the centre-list test is false
against an empty array, and the whole predicate is false: the session sees an
empty database.

That is the right default. It is also exactly wrong for a background job. The
post-call pipeline is handed a call id and has to enrich that row; without a
bound role it reports "call not found" for every call it is given, and the
campaign dialer loads a contact list of zero and reports a clean run. Both fail
*silently and successfully*, which is the worst shape a security default can
take -- the queue drains, the metrics look calm, and nothing is written.

The API and the media path already bind roles (``scoped_session`` binds the
staff user's, ``incall_session`` binds ``voice_agent``). Only the background
worker had nothing to bind, because it acts for no user.

A distinct role rather than reusing ``ops_manager``: audit and log lines then
distinguish "a background job read this" from "a person did", and a future
policy that wants to narrow what jobs may see has something to name.

This recreates every policy, because the role list is baked into the predicate
at creation time.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from uaagro_db.rls import CENTRE_COLUMN, enable_rls_statements
from uaagro_db.roles import AGENT_ROLE, APP_ROLE, ORG_WIDE_ROLES

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> tuple[str, ...]:
    from uaagro_db.models import RLS_TABLES

    return tuple(RLS_TABLES)


def upgrade() -> None:
    # Generated rather than written out, so this migration cannot drift from
    # the predicate the application tests against. The generator already drops
    # each policy before recreating it.
    for statement in enable_rls_statements(_tables()):
        op.execute(statement)


def downgrade() -> None:
    """Recreate the policies without the system role.

    Written out rather than generated, because generating it would produce
    whatever the current source says -- which after this migration includes the
    role we are trying to remove.
    """
    roles = ", ".join(f"'{role}'" for role in (*ORG_WIDE_ROLES, AGENT_ROLE))

    for table in _tables():
        column = CENTRE_COLUMN[table]
        predicate = (
            f"(current_setting('app.role', true) IN ({roles})"
            f" OR ({column} IS NOT NULL AND {column}::text = ANY ("
            "  string_to_array(coalesce(nullif(current_setting('app.centre_ids', true), ''), ''),"
            " ','))))"
        )
        op.execute(f"DROP POLICY IF EXISTS {table}_centre_scope ON {table}")
        op.execute(
            f"CREATE POLICY {table}_centre_scope ON {table} "
            f"FOR ALL TO {APP_ROLE} "
            f"USING {predicate} WITH CHECK {predicate}"
        )
