"""Index kb_chunks under both the simple and english text-search configs.

Revision ID: 0003
Revises: 0002
Created: Phase 3

Fixes a mismatch that made Tier-2 lexical retrieval return nothing for most
English queries.

0001 indexed an English chunk with the ``english`` configuration, which stems:
"services" is stored as ``servic``. Retrieval queries with ``simple``, which
does not stem, so it looks for ``services`` and never finds it. Both halves were
individually defensible and together they retrieved almost nothing -- the kind of
failure that shows up as "the knowledge base does not seem to have much in it"
rather than as an error.

Picking one configuration would have traded one silent loss for another.
``english`` alone mangles romanised Hindi, which is most of what a farmer types
and a large part of what an ASR transcript contains: "khaad" and "khad" stem
apart while "urea" and "urease" stem together. ``simple`` alone gives up English
morphology entirely, so "services" no longer matches a document about a service.

So both, concatenated into one vector. A tsvector is a set of lexemes and
concatenation is a union, so a chunk carries its raw tokens *and* its English
stems; the query is the OR of the same two configurations. The cost is a larger
index on a corpus measured in thousands of chunks, which is not a cost.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DUAL_CONFIG = """
CREATE OR REPLACE FUNCTION kb_chunks_search_vector() RETURNS trigger AS $$
BEGIN
    -- Both configurations, unioned. Applied unconditionally rather than
    -- branched on NEW.language: running 'english' over Devanagari is a no-op
    -- that costs a few duplicate lexemes, while getting the branch wrong on a
    -- mixed-script chunk costs the chunk.
    NEW.search_vector :=
        to_tsvector('simple'::regconfig, coalesce(NEW.content, '')) ||
        to_tsvector('english'::regconfig, coalesce(NEW.content, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

SINGLE_CONFIG = """
CREATE OR REPLACE FUNCTION kb_chunks_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector(
        CASE WHEN NEW.language LIKE 'en%' THEN 'english'::regconfig
             ELSE 'simple'::regconfig END,
        coalesce(NEW.content, '')
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def _reindex(connection: object) -> None:
    """Rebuild every stored vector by firing the trigger."""
    from sqlalchemy.engine import Connection

    assert isinstance(connection, Connection)
    # A no-op UPDATE is enough: the BEFORE UPDATE trigger recomputes the vector.
    connection.execute(text("UPDATE kb_chunks SET content = content"))


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text(DUAL_CONFIG))
    _reindex(connection)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text(SINGLE_CONFIG))
    _reindex(connection)
