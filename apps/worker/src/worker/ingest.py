"""Turn a panel upload into searchable knowledge (§9, §15.1).

The control plane accepts the file, stores it, writes the ``kb_documents`` row
with ``ingest_status='pending'`` and hands the id to this job. Everything slow
happens here: fetching the object or crawling the site, extracting text,
chunking, embedding on a model that takes a gigabyte of memory and seconds per
passage, and publishing.

The row is the progress report. It moves to ``indexing`` before the first byte
is read and to ``indexed`` or ``failed`` at the end, with the failure written
in the operator's terms ("The PDF contains no readable text") rather than a
retry count. A scan is not made readable by trying three times.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import Crop, KbDocument, Product
from uaagro_db.storage import object_store
from uaagro_domain.errors import UAAgroError
from voice_worker.knowledge.embeddings import E5Embedder
from voice_worker.knowledge.extract import DEFAULT_MAX_PAGES, extract_file, fetch_site
from voice_worker.knowledge.ingest import ingest_markdown

log = structlog.get_logger(__name__)

#: One embedder per worker process. Ingestion is the batch job §9 says should
#: use the machine, so it takes more threads than the media path allows.
_embedder: E5Embedder | None = None
_embedder_lock = asyncio.Lock()


async def embedder() -> E5Embedder | None:
    """The process's embedding model, or None when it cannot be loaded.

    None is a supported state, not an error: the document is stored BM25-only
    and the row says so, and the nightly ``refresh_embeddings`` job reports it
    until the model is available.
    """
    global _embedder
    async with _embedder_lock:
        if _embedder is None:
            candidate = E5Embedder(threads=4)
            unavailable = await candidate.warm()
            if unavailable is not None:
                log.warning("ingest.embedder_unavailable", reason=unavailable.reason)
                return None
            _embedder = candidate
        return _embedder


async def vocabularies(session: AsyncSession) -> tuple[list[str], list[str]]:
    """Crop and product names, for tagging chunks at ingest time."""
    crops = list((await session.scalars(select(Crop.name_en))).all())
    crops += list((await session.scalars(select(Crop.name_hi))).all())
    products = list((await session.scalars(select(Product.name_hi))).all())
    return [c for c in crops if c], [p for p in products if p]


async def ingest_pending_document(session: AsyncSession, document_id: uuid.UUID) -> str:
    """Fill in a document the panel created. Returns a one-line summary."""
    document = await session.scalar(
        select(KbDocument).where(KbDocument.id == document_id, KbDocument.deleted_at.is_(None))
    )
    if document is None:
        return "document not found"
    if document.ingest_status == "indexed":
        return "already indexed"

    document.ingest_status = "indexing"
    document.ingest_error = None
    await session.commit()

    try:
        markdown, page_count = await _content_of(document)
        crops, products = await vocabularies(session)
        model = await embedder()
        report = await ingest_markdown(
            session,
            title=document.title,
            markdown=markdown,
            doc_type=document.doc_type,
            source=document.source,
            language=document.language,
            organization_id=document.organization_id,
            embedder=model,
            crop_vocabulary=crops,
            product_vocabulary=products,
            # The operator who uploaded it is the approver: the panel's
            # upload is the review step for this corpus, and the row records
            # who did it.
            publish=True,
        )
    except UAAgroError as exc:
        return await _failed(session, document, exc.message)
    except Exception as exc:
        log.error("ingest.failed", document_id=str(document_id), error=type(exc).__name__)
        return await _failed(session, document, f"Indexing failed ({type(exc).__name__}).")

    await session.refresh(document)
    document.ingest_status = "indexed"
    document.ingest_error = None
    document.page_count = page_count
    # What was actually read, for the panel: a scan that produced forty
    # chunks of twelve words is visible as such. A crawl has no upload to
    # measure, so its size is the text it yielded.
    document.word_count = len(markdown.split())
    if document.size_bytes is None:
        document.size_bytes = len(markdown.encode("utf-8"))
    document.indexed_at = datetime.now(UTC)
    document.approved_by_user_id = document.uploaded_by_user_id
    document.approved_at = datetime.now(UTC)
    document.is_published = True
    await session.commit()
    log.info("ingest.indexed", document_id=str(document_id), summary=report.summary())
    return report.summary()


async def _content_of(document: KbDocument) -> tuple[str, int | None]:
    """The markdown for this document, from the crawl or the stored file."""
    if document.doc_type == "web":
        if not document.source:
            raise ValueError("a web document needs a source address")
        extracted = await fetch_site(document.source, max_pages=DEFAULT_MAX_PAGES)
        return extracted.markdown, extracted.page_count
    if not document.file_key:
        raise ValueError("the uploaded file is missing")
    data = await object_store().get(document.file_key)
    extracted = await asyncio.to_thread(
        extract_file, data, filename=document.source or document.file_key
    )
    return extracted.markdown, extracted.page_count


async def _failed(session: AsyncSession, document: KbDocument, reason: str) -> str:
    await session.rollback()
    document = await session.merge(document)
    document.ingest_status = "failed"
    document.ingest_error = reason[:500]
    await session.commit()
    return f"failed: {reason}"


__all__ = ("embedder", "ingest_pending_document", "vocabularies")
