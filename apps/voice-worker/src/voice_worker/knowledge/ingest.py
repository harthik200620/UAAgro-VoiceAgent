"""Ingest documents into ``kb_documents`` and ``kb_chunks`` (§9).

Chunk, embed, store. Three properties matter more than the pipeline itself:

**Re-embedding is skipped when nothing changed.** §9 says so explicitly, and it
is the difference between a document edit costing one chunk's inference and
costing the whole corpus. Both the document and each chunk carry a
``content_hash``; an unchanged chunk keeps its vector even when its neighbours
move.

**Ingestion never publishes.** A new document lands with ``is_published=False``
and retrieval filters on that flag, so uploading a file cannot put text in front
of a caller. §9 is explicit that the seed corpus contains `[VERIFY]` sections the
agronomy team must confirm; the review step is the point, and an ingest that
published would remove it.

**A document with no embeddings is still useful.** BM25 works without vectors,
so ingestion completes and reports how many chunks went un-embedded rather than
failing when the model is absent. Retrieval says it is degraded (§9's dense half
is missing) instead of pretending the corpus is fully indexed.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import KbChunk, KbDocument, Organization

from .chunking import CHUNKER_VERSION, Chunk, chunk_markdown
from .embeddings import E5Embedder

log = structlog.get_logger(__name__)

#: Embedding batch size. Large enough to amortise the ONNX call, small enough
#: that one batch does not hold a hundred 512-token sequences in memory.
EMBED_BATCH = 16


@dataclass
class IngestReport:
    """What ingestion did, in terms an operator can check."""

    document_title: str = ""
    document_id: str | None = None
    chunks_written: int = 0
    chunks_reused: int = 0
    chunks_embedded: int = 0
    chunks_unembedded: int = 0
    skipped_unchanged: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_unchanged:
            return f"{self.document_title}: unchanged, nothing to do."
        parts = [
            f"{self.document_title}: {self.chunks_written} chunks",
            f"{self.chunks_reused} reused",
            f"{self.chunks_embedded} embedded",
        ]
        if self.chunks_unembedded:
            parts.append(f"{self.chunks_unembedded} without vectors")
        return ", ".join(parts) + "."


async def ingest_markdown(
    session: AsyncSession,
    *,
    title: str,
    markdown: str,
    doc_type: str,
    source: str | None = None,
    language: str = "hi",
    organization_id: uuid.UUID | None = None,
    embedder: E5Embedder | None = None,
    crop_vocabulary: Sequence[str] = (),
    product_vocabulary: Sequence[str] = (),
    publish: bool = False,
) -> IngestReport:
    """Chunk, embed and store one document.

    Args:
        publish: Whether the document is servable. Defaults to False -- §9's
            seed corpus carries `[VERIFY]` sections that an agronomist must
            confirm, and publishing on ingest would skip that review.
    """
    report = IngestReport(document_title=title)
    # The chunker version is part of the hash, not just the text. Hashing the
    # input alone means a chunker improvement never reaches a corpus that is
    # already loaded -- silently, since "unchanged" is the expected result of a
    # re-ingest and looks identical either way.
    digest = hashlib.sha256(
        f"chunker-v{CHUNKER_VERSION}|".encode() + markdown.encode("utf-8")
    ).hexdigest()

    if organization_id is None:
        found: uuid.UUID | None = await session.scalar(select(Organization.id).limit(1))
        if found is None:
            raise ValueError("no organisation exists to attach the document to")
        organization_id = found

    document: KbDocument | None = await session.scalar(
        select(KbDocument).where(
            KbDocument.title == title, KbDocument.organization_id == organization_id
        )
    )

    if document is not None and document.content_hash == digest:
        report.skipped_unchanged = True
        report.document_id = str(document.id)
        log.info("knowledge.ingest_skipped", title=title, reason="unchanged")
        return report

    if document is None:
        document = KbDocument(
            organization_id=organization_id,
            title=title,
            doc_type=doc_type,
            language=language,
            source=source,
            version=1,
            content_hash=digest,
            is_published=publish,
        )
        session.add(document)
        await session.flush()
    else:
        document.version += 1
        document.content_hash = digest
        document.source = source or document.source
        # A changed document loses its approval. The reviewer approved the text
        # that was there, not the text that replaced it.
        document.approved_by_user_id = None
        document.approved_at = None
        document.is_published = publish
        await session.flush()

    report.document_id = str(document.id)

    existing = {
        row.content_hash: row
        for row in (
            await session.scalars(select(KbChunk).where(KbChunk.document_id == document.id))
        ).all()
    }

    chunks = chunk_markdown(
        markdown,
        crop_vocabulary=crop_vocabulary,
        product_vocabulary=product_vocabulary,
    )
    reusable_vectors = {
        h: row.embedding for h, row in existing.items() if row.embedding is not None
    }

    # Replaced wholesale rather than diffed in place: chunk_index shifts when a
    # paragraph is inserted, and the unique (document_id, chunk_index)
    # constraint would collide part-way through an update. The vectors are
    # carried across by hash, which is the expensive part.
    await session.execute(delete(KbChunk).where(KbChunk.document_id == document.id))

    to_embed: list[tuple[int, Chunk]] = []
    rows: list[KbChunk] = []
    for chunk in chunks:
        digest_chunk = chunk.content_hash
        vector = reusable_vectors.get(digest_chunk)
        row = KbChunk(
            document_id=document.id,
            chunk_index=chunk.index,
            content=chunk.content,
            content_hash=digest_chunk,
            language=chunk.language,
            embedding=vector,
            crop_tags=list(chunk.crop_tags),
            product_tags=list(chunk.product_tags),
            section_path=chunk.section_path or None,
        )
        rows.append(row)
        if vector is None:
            to_embed.append((len(rows) - 1, chunk))
        else:
            report.chunks_reused += 1

    session.add_all(rows)
    await session.flush()
    report.chunks_written = len(rows)

    if to_embed:
        if embedder is not None and embedder.ready:
            for start in range(0, len(to_embed), EMBED_BATCH):
                batch = to_embed[start : start + EMBED_BATCH]
                # Heading plus body -- see `Chunk.embedding_text`.
                vectors = await embedder.embed_passages(
                    [c.embedding_text for _, c in batch]
                )
                for (position, _), vector in zip(batch, vectors, strict=True):
                    rows[position].embedding = vector
                report.chunks_embedded += len(batch)
            await session.flush()
        else:
            report.chunks_unembedded = len(to_embed)
            # Said out loud rather than logged at debug: a corpus that is
            # BM25-only answers Hindi questions about English documents far
            # worse, and that is invisible from the outside.
            report.warnings.append(
                f"{len(to_embed)} chunks stored without embeddings; retrieval will "
                "run BM25-only until the embedding model is available."
            )

    log.info(
        "knowledge.ingested",
        title=title,
        document_id=str(document.id),
        version=document.version,
        chunks=report.chunks_written,
        embedded=report.chunks_embedded,
        reused=report.chunks_reused,
        published=publish,
    )
    return report


async def ingest_file(
    session: AsyncSession,
    path: Path,
    *,
    doc_type: str = "knowledge_base",
    **kwargs: object,
) -> IngestReport:
    """Ingest a markdown file, titled from its first heading or filename."""
    # Off the event loop: ingestion runs from the CLI and the admin upload
    # path, and a multi-megabyte read would block the loop serving live calls.
    markdown = await asyncio.to_thread(path.read_text, encoding="utf-8")
    title = _title_of(markdown) or path.stem
    return await ingest_markdown(
        session,
        title=title,
        markdown=markdown,
        doc_type=doc_type,
        source=path.name,
        **kwargs,  # type: ignore[arg-type]
    )


def _title_of(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


async def publish_document(
    session: AsyncSession, document_id: uuid.UUID, *, approved_by: uuid.UUID
) -> None:
    """Make a document servable. Requires a named approver (§9, §16.2)."""
    document: KbDocument | None = await session.get(KbDocument, document_id)
    if document is None:
        raise ValueError(f"no document {document_id}")
    document.approved_by_user_id = approved_by
    document.approved_at = datetime.now(UTC)
    document.is_published = True
    await session.flush()


__all__ = ("IngestReport", "ingest_file", "ingest_markdown", "publish_document")
