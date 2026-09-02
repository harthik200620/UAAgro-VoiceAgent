"""Which calls a knowledge document may be quoted on.

Revision ID: 0008
Revises: 0007
Created: knowledge base for outbound calls

The outbound agent has always been able to answer questions -- press 2 on an
offer call hands the farmer to the same agent, with the same tools, as the
helpline (§13.2). What it had no way to express was *which* documents belong
on which kind of call, because the panel offered the knowledge base under
Inbound alone and every document was implicitly for everything.

That is fine until the two diverge, and they do: a campaign's offer sheet is
this week's prices for the farmers being called and should not be read out to
whoever rings the helpline next month, while the crop and pesticide material
is for both. So a document now says where it is used.

``both`` is the default and the existing rows get it, because that is what
they have been doing since they were ingested. The column is checked rather
than free text -- retrieval filters on it, and a typo would silently make a
document unreachable rather than loudly wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kb_documents",
        sa.Column(
            "scope",
            sa.String(length=12),
            nullable=False,
            server_default=sa.text("'both'"),
        ),
    )
    op.create_check_constraint(
        "kb_document_scope_valid",
        "kb_documents",
        "scope IN ('inbound', 'outbound', 'both')",
    )
    # Retrieval asks for "documents this call may quote", which is always the
    # published ones for one direction plus the shared ones. The existing
    # published index leads on `is_published`; this carries the scope with it
    # so the filter is answered from the index rather than by re-reading rows.
    op.create_index(
        "ix_kb_documents_scope",
        "kb_documents",
        ["is_published", "scope", "language"],
    )


def downgrade() -> None:
    op.drop_index("ix_kb_documents_scope", table_name="kb_documents")
    op.drop_constraint("kb_document_scope_valid", "kb_documents", type_="check")
    op.drop_column("kb_documents", "scope")
