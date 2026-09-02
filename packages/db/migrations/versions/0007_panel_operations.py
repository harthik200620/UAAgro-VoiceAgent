"""Columns the operations panel needs: editable scripts, centre managers,
document indexing state and the contact-to-call link.

Revision ID: 0007
Revises: 0006
Created: admin panel rebuild

Four additions, each small, each closing a gap the panel exposed:

**``agent_configs.script``.** The outbound conversation was scripted in code
(§13.2) -- the disclosure, the permission question, the interest check -- and
the only editable part, the offer, lived on a separate ``offers`` row. An
operator who wants to invite farmers to a meeting instead of pitching an offer
had no field to type into. The script is now a structured JSON document on the
config version, so it is versioned, published and rolled back with everything
else the agent says. The legally mandated lines keep their defaults in code and
the panel shows them locked.

**``centres.manager_name``.** A centre's manager was modelled only as a panel
user (``manager_user_id``). Most centre managers will never log in; the agent
still needs a name to say when it hands a call over ("मैं आपको सुनीता जी से
जोड़ रहा हूँ"). A plain name column, next to the existing transfer number.

**``kb_documents.ingest_status`` / ``ingest_error`` / ``page_count``.** Uploads
from the panel are extracted, chunked and embedded in the background. The
panel needs to show "indexing" against the row while that runs and the reason
when it fails; before this the only states were "exists" and "published".
Existing rows were ingested synchronously by the CLI and are marked indexed.

**``campaign_contacts.call_id``.** The panel's contact card opens the call it
produced. The call knows its campaign (``calls.campaign_id``) but a contact
could not find its call without a join on the phone hash and a time window,
which is the kind of query that is right until two attempts land in one day.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_configs",
        sa.Column(
            "script",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.add_column("centres", sa.Column("manager_name", sa.String(length=200), nullable=True))

    op.add_column(
        "kb_documents",
        sa.Column(
            "ingest_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'indexed'"),
        ),
    )
    op.add_column("kb_documents", sa.Column("ingest_error", sa.Text(), nullable=True))
    op.add_column("kb_documents", sa.Column("page_count", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "kb_ingest_status_valid",
        "kb_documents",
        "ingest_status IN ('pending', 'indexing', 'indexed', 'failed')",
    )

    op.add_column(
        "campaign_contacts",
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_campaign_contacts_call_id",
        "campaign_contacts",
        ["call_id"],
        unique=False,
        postgresql_where=sa.text("call_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_contacts_call_id", table_name="campaign_contacts")
    op.drop_column("campaign_contacts", "call_id")

    op.drop_constraint("kb_ingest_status_valid", "kb_documents", type_="check")
    op.drop_column("kb_documents", "page_count")
    op.drop_column("kb_documents", "ingest_error")
    op.drop_column("kb_documents", "ingest_status")

    op.drop_column("centres", "manager_name")

    op.drop_column("agent_configs", "script")
