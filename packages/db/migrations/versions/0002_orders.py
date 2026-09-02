"""Orders and order lines.

Revision ID: 0002
Revises: 0001
Created: Phase 3

**Not part of §10.** §6.3 requires ``get_order_status(farmer_id, order_ref?)``
and §6.2 puts the farmer's last three orders in every turn's context block, but
the schema in §10 has nowhere to read either from. §1 N1 forbids answering
"मेरा ऑर्डर कहाँ है?" from anything but stored data, so the choice was between
adding these tables and shipping a tool that always says it does not know.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Table, text

from uaagro_db.models import Base
from uaagro_db.rls import disable_rls_statements, enable_rls_statements
from uaagro_db.roles import APP_ROLE

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("orders", "order_items")
#: Only ``orders`` carries a centre and a farmer, so only it is policy-scoped.
#: ``order_items`` is reachable solely by joining through it.
RLS_TABLES = ("orders",)


def _tables() -> list[Table]:
    """This revision's tables, resolved against the full metadata.

    Copying them into a fresh ``MetaData`` would be tidier but does not work:
    their foreign keys point at ``farmers``, ``centres``, ``organizations`` and
    ``product_variants``, all created by 0001, and SQLAlchemy cannot resolve a
    foreign key to a table that is not in the same collection. Selecting a
    subset of the shared metadata keeps the references intact and still creates
    only what this revision owns.
    """
    return [Base.metadata.tables[name] for name in TABLES]


def upgrade() -> None:
    connection = op.get_bind()

    Base.metadata.create_all(bind=connection, tables=_tables(), checkfirst=False)

    for table in TABLES:
        connection.execute(text(f"DROP TRIGGER IF EXISTS trg_{table}_touch ON {table}"))
        connection.execute(
            text(
                f"CREATE TRIGGER trg_{table}_touch BEFORE UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
            )
        )

    # ALTER DEFAULT PRIVILEGES in 0001 covers tables created by the migrator
    # afterwards, but granting explicitly costs nothing and does not depend on
    # both revisions having run as the same role.
    for table in TABLES:
        connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        )

    for statement in enable_rls_statements(RLS_TABLES):
        connection.execute(text(statement))


def downgrade() -> None:
    connection = op.get_bind()

    for statement in disable_rls_statements(RLS_TABLES):
        connection.execute(text(statement))
    for table in TABLES:
        connection.execute(text(f"DROP TRIGGER IF EXISTS trg_{table}_touch ON {table}"))
    Base.metadata.drop_all(bind=connection, tables=_tables())
