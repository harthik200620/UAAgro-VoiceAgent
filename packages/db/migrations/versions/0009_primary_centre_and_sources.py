"""The primary centre, and the client's database as a data source.

Revision ID: 0009
Revises: 0008
Created: the head office, and where the catalogue comes from

Two things the first live calls showed were missing.

**A primary centre.** A caller the helpline does not know has no centre, and
a stock question with no centre had no answer -- the agent was left to say
"not in stock" about a product it had never looked up. The helpline now
answers stock and price for the head office when the caller's own centre is
unknown, and says so. Exactly one active centre per organisation is primary;
the partial unique index enforces it in the database rather than in a form.

**Data sources.** UA Agro keeps stores, products and stock in a MySQL database
of its own. The platform stays on PostgreSQL (row-level security, vector
search and partitions depend on it) and *pulls* from the client's database:
``data_sources`` names the server and the table mapping, ``data_source_runs``
records each pull. The password column is ciphertext under the platform data
key, never plaintext.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "centres",
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ux_centres_primary",
        "centres",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )

    op.create_table(
        "data_sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default=sa.text("'mysql'")),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default=sa.text("3306")),
        sa.Column("database", sa.String(length=128), nullable=False),
        sa.Column("user", sa.String(length=128), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=True),
        sa.Column("tls", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "mapping",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "schedule", sa.String(length=12), nullable=False, server_default=sa.text("'manual'")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("kind IN ('mysql')", name="data_source_kind_valid"),
        sa.CheckConstraint(
            "schedule IN ('manual', 'hourly', 'daily')", name="data_source_schedule_valid"
        ),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="data_source_port_range"),
    )
    op.create_index("ix_data_sources_org", "data_sources", ["organization_id", "is_active"])

    op.create_table(
        "data_source_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(length=12), nullable=False, server_default=sa.text("'running'")
        ),
        sa.Column("stores_written", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("products_written", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stock_written", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "report",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'failed')", name="data_source_run_status_valid"
        ),
    )
    op.create_index(
        "ix_data_source_runs_source_started", "data_source_runs", ["source_id", "started_at"]
    )

    # The application role reads and writes these like every other table it
    # owns nothing of. Not RLS-protected: a data source is organisation-wide
    # configuration, and only ops_manager+ reaches the routes.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON data_sources TO uaagro_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON data_source_runs TO uaagro_app")
    # `updated_at` is maintained by the trigger revision 0001 installs on every
    # table it creates; tables added later install their own.
    for table in ("data_sources", "data_source_runs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_touch ON {table}")
        op.execute(
            f"CREATE TRIGGER trg_{table}_touch BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
        )


def downgrade() -> None:
    op.drop_index("ix_data_source_runs_source_started", table_name="data_source_runs")
    op.drop_table("data_source_runs")
    op.drop_index("ix_data_sources_org", table_name="data_sources")
    op.drop_table("data_sources")
    op.drop_index("ux_centres_primary", table_name="centres")
    op.drop_column("centres", "is_primary")
