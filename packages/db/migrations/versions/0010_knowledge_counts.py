"""Size, word count and indexed-at on knowledge documents.

Revision ID: 0010
Revises: 0009
Created: the knowledge page says how big a document is and when it was read

The panel lists documents with their chunk and vector counts, which say how
retrieval sees a document and nothing about what the operator uploaded. A
scanned PDF that produced forty chunks of twelve words looks fine by chunk
count and is useless on a call; a note typed in the panel has no page count
at all. Three columns close that gap: the stored object's size, the number of
words that were actually chunked, and when indexing last finished -- which
is also what "re-index" resets.

All three are nullable. Rows the CLI ingested before the panel existed have
no upload to measure, and a document still pending has not been read yet.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("kb_documents", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("kb_documents", sa.Column("word_count", sa.Integer(), nullable=True))
    op.add_column(
        "kb_documents", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Rows indexed before these columns existed keep an honest "indexed at":
    # the last time the row was written is the closest thing to it, and the
    # panel would otherwise show a blank beside every row the CLI loaded.
    op.execute(
        "UPDATE kb_documents SET indexed_at = updated_at "
        "WHERE ingest_status = 'indexed' AND indexed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("kb_documents", "indexed_at")
    op.drop_column("kb_documents", "word_count")
    op.drop_column("kb_documents", "size_bytes")
