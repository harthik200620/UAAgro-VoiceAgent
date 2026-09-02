"""Initial schema: tables, partitions, triggers, roles and RLS.

Revision ID: 0001
Revises:
Created: Phase 1

Builds the whole of §10 in one migration. The tables themselves come from the
SQLAlchemy metadata so the schema and the models cannot drift on day one;
everything the ORM cannot express -- extensions, partitions, triggers, the
application role, grants and the RLS policies -- is explicit SQL below.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

from alembic import op
from sqlalchemy import MetaData, text

from uaagro_db.models import RLS_TABLES, Base
from uaagro_db.partitions import partition_ddl
from uaagro_db.rls import (
    create_app_role_statements,
    disable_rls_statements,
    enable_rls_statements,
)
from uaagro_db.roles import APP_ROLE
from uaagro_domain.settings import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------- #
# Extensions
# --------------------------------------------------------------------------- #

#: Required. Dense retrieval over kb_chunks (§9 Tier 2) is not optional, and a
#: server without it cannot serve the knowledge layer at all.
REQUIRED_EXTENSIONS = ("vector",)

#: Wanted, but their absence degrades rather than breaks.
#:
#: ``pg_trgm`` backs the trigram indexes that let a misheard brand name still
#: match (§5.5). Without it product search falls back to full-text and exact
#: matching -- slower and less forgiving, but correct. The readiness check
#: reports the gap rather than letting it pass unnoticed.
#:
#: ``pgcrypto`` is deliberately **not** required. Its only use here was
#: ``gen_random_uuid()``, which has been in the Postgres core since 13. Keeping
#: it as a hard dependency would exclude otherwise-capable servers for nothing.
OPTIONAL_EXTENSIONS = ("pg_trgm", "pgcrypto")

#: Indexes that need ``pg_trgm``. Skipped, with a warning, when it is absent.
TRIGRAM_INDEXES = ("ix_products_name_hi_trgm", "ix_products_name_en_trgm")

#: Columns added by later migrations to tables this revision *does* create.
#:
#: Same reasoning as POST_0001_TABLES below, one level down. Adding a field to
#: an existing model would otherwise make revision 0001 create it -- so a fresh
#: database would get the column from 0001 and then fail 0005 with a duplicate,
#: while a database migrated earlier would take the 0005 path. Two databases,
#: same revision history, different schemas.
POST_0001_COLUMNS: dict[str, tuple[str, ...]] = {
    "campaigns": ("is_promotional", "dlt_entity_id"),
    # Revision 0007: the operations panel.
    "agent_configs": ("script",),
    "centres": ("manager_name",),
    "kb_documents": ("ingest_status", "ingest_error", "page_count"),
    "campaign_contacts": ("call_id",),
}

#: Tables added by later migrations, excluded here.
#:
#: This migration builds from live ORM metadata, which keeps schema and models
#: from drifting -- but it also means that adding a model would silently change
#: what *this* revision creates. A database migrated before the addition would
#: then never receive the table, while a fresh one would get it from revision
#: 0001, and the two would disagree about what "0001" means. Pinning the set
#: here keeps every revision reproducible.
POST_0001_TABLES = ("orders", "order_items")


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #

#: updated_at is maintained server-side. Rows are written by the ORM, by the
#: seed loader, by background jobs and by migrations; a Python-side onupdate
#: would leave three of those four stale.
TOUCH_UPDATED_AT = """
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

#: Product search vector.
#:
#: Built with the 'simple' configuration, not 'english'. The English stemmer
#: mangles romanised Hindi -- "davai", "dawai", "bori" -- which is most of what
#: a farmer-facing catalogue actually contains. 'simple' does no stemming, and
#: recall is recovered by the trigram indexes and the ASR lexicon instead.
PRODUCT_SEARCH_VECTOR = """
CREATE OR REPLACE FUNCTION products_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', coalesce(NEW.name_hi, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(NEW.name_en, '')), 'A') ||
        setweight(to_tsvector('simple',
            coalesce(array_to_string(NEW.lexicon_variants, ' '), '')), 'B') ||
        setweight(to_tsvector('simple',
            coalesce(array_to_string(NEW.active_ingredients, ' '), '')), 'C') ||
        setweight(to_tsvector('simple',
            coalesce(array_to_string(NEW.crop_targets, ' '), '')), 'D') ||
        setweight(to_tsvector('simple',
            coalesce(array_to_string(NEW.pest_targets, ' '), '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

#: Knowledge-chunk search vector.
#:
#: English chunks get the English stemmer, which genuinely helps; everything
#: else gets 'simple'. §9 requires retrieval in both the caller's language and
#: English, then fusion -- so both configurations must exist and be correct.
CHUNK_SEARCH_VECTOR = """
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

#: Audit chain sequence. §17 orders the hash chain by a monotonic counter
#: rather than by timestamp, because two rows can share a millisecond and the
#: chain needs a total order.
AUDIT_CHAIN_SEQUENCE = "CREATE SEQUENCE IF NOT EXISTS audit_log_chain_seq AS bigint START 1"


def _tables_with_updated_at() -> list[str]:
    return sorted(
        name
        for name, table in Base.metadata.tables.items()
        if "updated_at" in table.c and name not in POST_0001_TABLES
    )


def _rls_tables() -> tuple[str, ...]:
    """Protected tables this revision creates. See ``POST_0001_TABLES``."""
    return tuple(t for t in RLS_TABLES if t not in POST_0001_TABLES)


def _app_role_password() -> str:
    """Password for ``uaagro_app``, taken from the application's own DSN.

    Deriving it from ``DATABASE_URL`` guarantees the role the migration creates
    is the one the application will actually authenticate as -- a mismatch here
    surfaces as an opaque auth failure at boot.
    """
    settings = get_settings()
    password = urlsplit(settings.database_url).password
    if not password:
        raise RuntimeError(
            "DATABASE_URL has no password, so the uaagro_app role cannot be created. "
            "Set DATABASE_URL to the full application DSN before migrating."
        )
    return password


def _warn(message: str) -> None:
    """Emit a migration warning to stderr.

    Migrations run as a one-shot command whose output an operator reads from a
    terminal or a deploy log, so stderr is the right channel -- structured
    logging is not configured at this point in the process.
    """
    sys.stderr.write(f"warning: {message}\n")


def _subset(
    metadata: MetaData,
    *,
    without_indexes: tuple[str, ...] = (),
    without_tables: tuple[str, ...] = (),
) -> MetaData:
    """A copy of ``metadata`` with the named indexes and tables removed.

    Mutating the shared metadata would leak into the ORM for the rest of the
    process, so everything is detached from a copy used only for this
    ``create_all``.
    """
    skip_tables = set(without_tables)
    unwanted = set(without_indexes)
    copy = MetaData(naming_convention=metadata.naming_convention)
    for name, table in metadata.tables.items():
        if name not in skip_tables:
            table.to_metadata(copy)
    for name, table in copy.tables.items():
        for index in {i for i in table.indexes if i.name in unwanted}:
            table.indexes.discard(index)
        for column_name in POST_0001_COLUMNS.get(name, ()):
            if column_name in table.c:
                table._columns.remove(table.c[column_name])
    return copy


def upgrade() -> None:
    connection = op.get_bind()

    # Availability is checked before creating, not caught afterwards: a failed
    # CREATE EXTENSION aborts the surrounding transaction, and rolling back to
    # recover would discard the rest of the migration with it.
    installable = {
        row[0] for row in connection.execute(text("SELECT name FROM pg_available_extensions"))
    }

    missing_required = [e for e in REQUIRED_EXTENSIONS if e not in installable]
    if missing_required:
        raise RuntimeError(
            f"This Postgres server does not provide {', '.join(missing_required)}. "
            f"pgvector is required for the knowledge layer (spec 9). Use the "
            f"pgvector/pgvector:pg16 image, or install the extension package."
        )
    for extension in REQUIRED_EXTENSIONS:
        connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))

    available: set[str] = set()
    for extension in OPTIONAL_EXTENSIONS:
        if extension in installable:
            connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))
            available.add(extension)
        else:
            _warn(f"extension {extension!r} is unavailable on this server. Continuing without it.")

    connection.execute(text(AUDIT_CHAIN_SEQUENCE))

    skip_indexes: tuple[str, ...] = ()
    if "pg_trgm" not in available:
        # Drop the trigram indexes from this run only. They are a search-quality
        # optimisation, not a correctness requirement, so a server without
        # pg_trgm gets a correct schema and a warning rather than a failure.
        skip_indexes = TRIGRAM_INDEXES
        _warn(
            "pg_trgm is unavailable, so the product trigram indexes were skipped. "
            "Fuzzy product-name matching will fall back to full-text search. "
            "Install pg_trgm before production."
        )

    # Tables straight from the models, so schema and ORM agree by construction.
    # checkfirst=False: this is the initial migration, nothing exists yet, and
    # it keeps `alembic upgrade head --sql` (offline mode) working.
    metadata = _subset(
        Base.metadata, without_indexes=skip_indexes, without_tables=POST_0001_TABLES
    )
    metadata.create_all(bind=connection, checkfirst=False)

    # -- triggers --------------------------------------------------------- #
    connection.execute(text(TOUCH_UPDATED_AT))
    for table in _tables_with_updated_at():
        connection.execute(text(f"DROP TRIGGER IF EXISTS trg_{table}_touch ON {table}"))
        connection.execute(
            text(
                f"CREATE TRIGGER trg_{table}_touch BEFORE UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
            )
        )

    connection.execute(text(PRODUCT_SEARCH_VECTOR))
    connection.execute(text("DROP TRIGGER IF EXISTS trg_products_search ON products"))
    connection.execute(
        text(
            "CREATE TRIGGER trg_products_search BEFORE INSERT OR UPDATE ON products "
            "FOR EACH ROW EXECUTE FUNCTION products_search_vector()"
        )
    )

    connection.execute(text(CHUNK_SEARCH_VECTOR))
    connection.execute(text("DROP TRIGGER IF EXISTS trg_kb_chunks_search ON kb_chunks"))
    connection.execute(
        text(
            "CREATE TRIGGER trg_kb_chunks_search BEFORE INSERT OR UPDATE ON kb_chunks "
            "FOR EACH ROW EXECUTE FUNCTION kb_chunks_search_vector()"
        )
    )

    # -- partitions ------------------------------------------------------- #
    # An insert with no matching partition fails, and that failure would land in
    # the audio path. A window is created now; `make db-partitions` rolls it on.
    for statement in partition_ddl(datetime.now(UTC).date()):
        connection.execute(text(statement))

    # -- role, grants, RLS ------------------------------------------------ #
    for statement in create_app_role_statements(_app_role_password()):
        connection.execute(text(statement))
    connection.execute(text(f"GRANT USAGE, SELECT ON SEQUENCE audit_log_chain_seq TO {APP_ROLE}"))

    for statement in enable_rls_statements(_rls_tables()):
        connection.execute(text(statement))


def downgrade() -> None:
    connection = op.get_bind()

    for statement in disable_rls_statements(_rls_tables()):
        connection.execute(text(statement))

    for table in _tables_with_updated_at():
        connection.execute(text(f"DROP TRIGGER IF EXISTS trg_{table}_touch ON {table}"))
    connection.execute(text("DROP TRIGGER IF EXISTS trg_products_search ON products"))
    connection.execute(text("DROP TRIGGER IF EXISTS trg_kb_chunks_search ON kb_chunks"))
    connection.execute(text("DROP FUNCTION IF EXISTS touch_updated_at()"))
    connection.execute(text("DROP FUNCTION IF EXISTS products_search_vector()"))
    connection.execute(text("DROP FUNCTION IF EXISTS kb_chunks_search_vector()"))

    # Dropping a partitioned parent drops its partitions with it.
    _subset(Base.metadata, without_tables=POST_0001_TABLES).drop_all(bind=connection)
    connection.execute(text("DROP SEQUENCE IF EXISTS audit_log_chain_seq"))
    # The role is intentionally left in place: it may own grants in other
    # databases on the same cluster, and dropping it is an operator decision.
