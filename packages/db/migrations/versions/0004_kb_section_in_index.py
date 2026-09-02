"""Index a chunk's section heading alongside its text.

Revision ID: 0004
Revises: 0003
Created: Phase 3

A chunk's section path is some of the most discriminating text it carries.
"CONVERSATION SNIPPETS -- THE TARGET REGISTER" says more about what a passage
answers than most of its sentences do, because a heading names the topic while
the body underneath tends to demonstrate it. Indexing only the body throws that
away, and it is exactly the signal a question is phrased against: a caller asks
"how do I handle an interruption", and the heading says "Handling an
interruption gracefully" while the body is a dialogue transcript that never uses
either word.

Weighted ``A`` against the body's ``B`` so a heading match ranks above an
incidental mention. The dense half gets the same treatment in
``Chunk.embedding_text``; keeping the two halves of §9's hybrid retrieval
looking at the same text is the point, since fusing lists built from different
inputs makes the ranks incomparable.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WITH_SECTION = """
CREATE OR REPLACE FUNCTION kb_chunks_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple'::regconfig, coalesce(NEW.section_path, '')), 'A') ||
        setweight(to_tsvector('english'::regconfig, coalesce(NEW.section_path, '')), 'A') ||
        setweight(to_tsvector('simple'::regconfig, coalesce(NEW.content, '')), 'B') ||
        setweight(to_tsvector('english'::regconfig, coalesce(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

WITHOUT_SECTION = """
CREATE OR REPLACE FUNCTION kb_chunks_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        to_tsvector('simple'::regconfig, coalesce(NEW.content, '')) ||
        to_tsvector('english'::regconfig, coalesce(NEW.content, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def _reindex() -> None:
    """Rebuild stored vectors by firing the BEFORE UPDATE trigger."""
    op.get_bind().execute(text("UPDATE kb_chunks SET content = content"))


def upgrade() -> None:
    op.get_bind().execute(text(WITH_SECTION))
    _reindex()


def downgrade() -> None:
    op.get_bind().execute(text(WITHOUT_SECTION))
    _reindex()
