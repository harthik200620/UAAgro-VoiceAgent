"""What the agent knows, as the panel manages it (§9, §15.1).

An upload is accepted fast and indexed slowly. The API stores the file,
writes the document row with ``ingest_status='pending'`` and hands the id to
the background worker; the row is the progress report the panel polls.
Nothing the agent could say changes until the worker has extracted, chunked,
embedded and published -- which is also why "switch off" is instant: it flips
the flag retrieval filters on.

A note typed in the panel is a document like any other. Its text is stored
as an object and goes through the same queue, so an operator's two-line
correction to a price list is indexed, versioned and retrievable exactly as
a PDF would be, and shows the same counts.

When the queue cannot be reached the API indexes the document itself, in the
background, rather than leave it pending: a document nobody will ever pick up
is a document the operator believes the agent knows. The row says "indexed
inline" while that runs, and the status route says whether the worker has
been heard from at all.

"Try a question" asks the voice worker, not this process. The worker has the
retriever and its model warm; the answer the panel shows is the answer a
caller would get, because it is produced by the same code.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import pydantic
import structlog
from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.engine import system_session
from uaagro_db.models import KbChunk, KbDocument
from uaagro_db.storage import knowledge_key, object_store
from uaagro_domain.enums import AuditAction, Role
from uaagro_domain.errors import NotFoundError, ValidationError, VendorError
from uaagro_domain.settings import get_defaults, get_settings

from ..security.deps import DbDep, Principal, require_role
from ..services import jobs
from ..services.worker_client import WorkerClient
from ._panel import iso

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/knowledge", tags=["panel"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
#: A note is typed, not uploaded; past this it is a document and wants a file.
MAX_NOTE_CHARS = 100_000
_SUPPORTED = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".csv": "csv",
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
}

#: What ``ingestError`` reads while this process indexes a document itself.
INLINE_MARKER = "indexed inline"

#: Where a document may be quoted. Mirrors the check constraint on the column.
Scope = Literal["inbound", "outbound", "both"]

#: Which kind of call is asking.
Direction = Literal["inbound", "outbound"]

#: The last measured search latency, from the most recent "try a question".
#: Process memory, not a row: it is a reading, and the next question replaces it.
_last_retrieval_ms: int | None = None


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class KbDocumentRow(BaseModel):
    id: str
    title: str
    docType: str
    language: str
    source: str | None
    version: int
    chunks: int
    embedded: int
    pageCount: int | None
    ingestStatus: str
    ingestError: str | None
    isPublished: bool
    #: "inbound", "outbound" or "both" -- which calls may quote this.
    scope: str
    updatedAt: str
    sizeBytes: int | None
    indexedAt: str | None
    wordCount: int | None


class KnowledgeWorker(BaseModel):
    alive: bool
    lastSeenAt: str | None


class DocumentCounts(BaseModel):
    pending: int
    indexing: int
    indexed: int
    failed: int


class KnowledgeStatus(BaseModel):
    worker: KnowledgeWorker
    documents: DocumentCounts
    chunks: int
    embedded: int
    embeddingModel: str
    retrievalMs: int | None


class CrawlBody(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    title: str | None = Field(default=None, max_length=300)
    maxPages: int = Field(default=100, ge=1, le=500)
    language: str | None = None
    scope: Scope = "both"


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)
    title: str = Field(min_length=1, max_length=300)
    language: str | None = None
    scope: Scope = "both"


class PublishBody(BaseModel):
    isPublished: bool | None = None
    scope: Scope | None = None


class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    answer: bool = False
    language: str | None = None
    #: Which kind of call to answer as. An offer call and the helpline can
    #: reach different documents, so the panel says which it is testing.
    direction: Direction = "inbound"


class Passage(BaseModel):
    documentTitle: str
    section: str | None
    snippet: str
    score: float


class Answer(BaseModel):
    text: str | None
    totalMs: int | None
    note: str | None


class AskResult(BaseModel):
    passages: list[Passage]
    retrievalMs: int
    degraded: bool
    answer: Answer | None


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _row(doc: KbDocument, chunks: int, embedded: int) -> KbDocumentRow:
    return KbDocumentRow(
        id=str(doc.id),
        title=doc.title,
        docType=doc.doc_type,
        language=doc.language,
        source=doc.source,
        version=doc.version,
        chunks=chunks,
        embedded=embedded,
        pageCount=doc.page_count,
        ingestStatus=doc.ingest_status,
        ingestError=doc.ingest_error,
        isPublished=doc.is_published,
        scope=doc.scope,
        updatedAt=doc.updated_at.isoformat(),
        sizeBytes=doc.size_bytes,
        indexedAt=iso(doc.indexed_at),
        wordCount=doc.word_count,
    )


async def list_documents(db: AsyncSession) -> list[KbDocumentRow]:
    embedded = func.count(KbChunk.embedding).label("embedded")
    rows = (
        await db.execute(
            select(KbDocument, func.count(KbChunk.id), embedded)
            .outerjoin(KbChunk, KbChunk.document_id == KbDocument.id)
            .where(KbDocument.deleted_at.is_(None))
            .group_by(KbDocument.id)
            .order_by(KbDocument.updated_at.desc())
        )
    ).all()
    return [_row(doc, int(chunks or 0), int(vectors or 0)) for doc, chunks, vectors in rows]


async def _one(db: AsyncSession, document_id: uuid.UUID) -> KbDocumentRow:
    embedded = func.count(KbChunk.embedding).label("embedded")
    row = (
        await db.execute(
            select(KbDocument, func.count(KbChunk.id), embedded)
            .outerjoin(KbChunk, KbChunk.document_id == KbDocument.id)
            .where(KbDocument.id == document_id, KbDocument.deleted_at.is_(None))
            .group_by(KbDocument.id)
        )
    ).first()
    if row is None:
        raise NotFoundError(resource="document", identifier=str(document_id))
    doc, chunks, vectors = row
    return _row(doc, int(chunks or 0), int(vectors or 0))


async def _document(db: AsyncSession, document_id: uuid.UUID) -> KbDocument:
    doc = await db.scalar(
        select(KbDocument).where(KbDocument.id == document_id, KbDocument.deleted_at.is_(None))
    )
    if doc is None:
        raise NotFoundError(resource="document", identifier=str(document_id))
    return doc


@router.get("/documents", response_model=list[KbDocumentRow])
async def documents(
    db: DbDep, _: Annotated[Principal, require_role(Role.AGRONOMIST)]
) -> list[KbDocumentRow]:
    """Documents with their indexing and embedding progress.

    ``embedded`` separate from ``chunks`` because they diverge in a way that
    matters: ingestion completes BM25-only when the embedding model is
    unavailable, and those chunks retrieve far worse without ever looking
    broken. A document showing 40 chunks and 0 embedded is the explanation for
    "the agent cannot find this".
    """
    return await list_documents(db)


@router.get("/status", response_model=KnowledgeStatus)
async def status(
    db: DbDep, _: Annotated[Principal, require_role(Role.AGRONOMIST)]
) -> KnowledgeStatus:
    """Whether anything is indexing, and how much has been.

    The worker's heartbeat is the first line: a queue of pending documents
    with no worker behind it is the thing this page exists to make visible.
    """
    last_seen = await jobs.worker_last_seen()
    alive = last_seen is not None and datetime.now(UTC) - last_seen < timedelta(
        seconds=jobs.HEARTBEAT_TTL_S
    )
    by_status = {
        str(state): int(count)
        for state, count in (
            await db.execute(
                select(KbDocument.ingest_status, func.count())
                .where(KbDocument.deleted_at.is_(None))
                .group_by(KbDocument.ingest_status)
            )
        ).all()
    }
    chunks, embedded = (
        await db.execute(
            select(func.count(KbChunk.id), func.count(KbChunk.embedding))
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(KbDocument.deleted_at.is_(None))
        )
    ).one()
    return KnowledgeStatus(
        worker=KnowledgeWorker(alive=alive, lastSeenAt=iso(last_seen)),
        documents=DocumentCounts(
            pending=by_status.get("pending", 0),
            indexing=by_status.get("indexing", 0),
            indexed=by_status.get("indexed", 0),
            failed=by_status.get("failed", 0),
        ),
        chunks=int(chunks or 0),
        embedded=int(embedded or 0),
        embeddingModel=get_settings().embedding_model,
        retrievalMs=_last_retrieval_ms,
    )


# --------------------------------------------------------------------------- #
# Adding
# --------------------------------------------------------------------------- #


def _parse[T: BaseModel](model: type[T], payload: Any) -> T:
    """A JSON body checked the way a typed body would be, answered as a 422."""
    try:
        return model.model_validate(payload)
    except pydantic.ValidationError as exc:
        errors = exc.errors()
        field = ".".join(str(part) for part in errors[0]["loc"]) if errors else "body"
        message = errors[0]["msg"] if errors else "is not valid"
        raise ValidationError(
            f"{field}: {message}.", remedy="Check the field and send it again."
        ) from None


@router.post("/documents", response_model=KbDocumentRow, status_code=202)
async def add_document(
    request: Request,
    background: BackgroundTasks,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
    file: Annotated[UploadFile | None, File()] = None,
    title: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
) -> KbDocumentRow:
    """A file (multipart), a website or a typed note (JSON). Indexing is queued."""
    content_type = request.headers.get("content-type", "")
    default_language = _base_language(get_defaults().default_language)

    if "application/json" in content_type:
        payload = await request.json()
        if isinstance(payload, dict) and "text" in payload:
            note = _parse(NoteBody, payload)
            document = await _note(db, principal, note, default_language)
            await _queue(db, document, principal, background, note=f"{document.size_bytes} bytes")
            return await _one(db, document.id)

        body = _parse(CrawlBody, payload)
        if not body.url.lower().startswith(("http://", "https://")):
            raise ValidationError("That is not a web address.", remedy="Start it with https://")
        document = KbDocument(
            organization_id=principal.organization_id,
            title=(body.title or body.url).strip()[:300],
            doc_type="web",
            language=(body.language or default_language)[:12],
            source=body.url.strip(),
            version=0,
            scope=body.scope,
            content_hash=hashlib.sha256(body.url.encode()).hexdigest(),
            uploaded_by_user_id=principal.user_id,
            ingest_status="pending",
            created_by=principal.user_id,
        )
        db.add(document)
        await db.flush()
        await _queue(db, document, principal, background, note=f"crawl up to {body.maxPages} pages")
        return await _one(db, document.id)

    if file is None or not file.filename:
        raise ValidationError("Nothing to add.", remedy="Choose a file, or give a website address.")
    suffix = ("." + file.filename.rsplit(".", 1)[-1].lower()) if "." in file.filename else ""
    doc_type = _SUPPORTED.get(suffix)
    if doc_type is None:
        raise ValidationError(
            f"Cannot read {file.filename!r}.",
            remedy="Upload a PDF, a Word document (.docx), a spreadsheet saved as CSV, "
            "or a text or Markdown file.",
        )
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"{file.filename!r} is {len(data) // (1024 * 1024)} MB.",
            remedy=f"Upload files under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    if not data:
        raise ValidationError("The file is empty.", remedy="Choose a file with content.")

    document = KbDocument(
        organization_id=principal.organization_id,
        title=(title or _title_from(file.filename)).strip()[:300],
        doc_type=doc_type,
        language=(language or default_language)[:12],
        source=file.filename[:300],
        version=0,
        scope=_scope_or_both(scope),
        content_hash=hashlib.sha256(data).hexdigest(),
        uploaded_by_user_id=principal.user_id,
        ingest_status="pending",
        size_bytes=len(data),
        created_by=principal.user_id,
    )
    db.add(document)
    await db.flush()
    key = knowledge_key(str(document.id), file.filename)
    content_type = file.content_type or "application/octet-stream"
    await object_store().put(key, data, content_type=content_type)
    document.file_key = key
    await _queue(db, document, principal, background, note=f"{len(data)} bytes")
    return await _one(db, document.id)


async def _note(
    db: AsyncSession, principal: Principal, body: NoteBody, default_language: str
) -> KbDocument:
    """A typed note, stored as the text file it would have been uploaded as."""
    text = body.text.strip()
    if not text:
        raise ValidationError("The note is empty.", remedy="Type what the agent should know.")
    data = text.encode("utf-8")
    document = KbDocument(
        organization_id=principal.organization_id,
        title=body.title.strip()[:300],
        doc_type="text",
        language=(body.language or default_language)[:12],
        source=None,
        version=0,
        scope=body.scope,
        content_hash=hashlib.sha256(data).hexdigest(),
        uploaded_by_user_id=principal.user_id,
        ingest_status="pending",
        size_bytes=len(data),
        # Known now rather than at indexing: a note is its own text, and the
        # row should say so while it waits.
        word_count=len(text.split()),
        created_by=principal.user_id,
    )
    db.add(document)
    await db.flush()
    key = knowledge_key(str(document.id), "note.txt")
    await object_store().put(key, data, content_type="text/plain; charset=utf-8")
    document.file_key = key
    return document


async def _queue(
    db: AsyncSession,
    document: KbDocument,
    principal: Principal,
    background: BackgroundTasks,
    *,
    note: str,
    action: AuditAction = AuditAction.CREATE,
) -> None:
    await append_audit(
        db,
        action=action,
        resource_type="kb_document",
        resource_id=str(document.id),
        actor_user_id=principal.user_id,
        after={"title": document.title, "doc_type": document.doc_type, "note": note},
    )
    # Committed before the job is enqueued: the worker must find the row.
    await db.commit()
    try:
        await jobs.enqueue("ingest_document", str(document.id))
    except VendorError as exc:
        # The queue is down. Indexed here instead, after the response is
        # written, so the document is never left silently pending; the row
        # says which path it took.
        log.warning(
            "knowledge.queue_unreachable",
            document_id=str(document.id),
            error=type(exc).__name__,
        )
        document.ingest_error = INLINE_MARKER
        await db.commit()
        background.add_task(_ingest_inline, document.id)


async def _ingest_inline(document_id: uuid.UUID) -> None:
    """Index one document in this process, because nothing else will.

    The worker's own code, when it is installed beside the API; the row is
    marked failed with the fix when it is not. This loads the embedding model
    into the control plane, which is why it is the fallback and not the path.
    """
    try:
        from worker.ingest import ingest_pending_document
    except ImportError as exc:
        log.warning("knowledge.inline_unavailable", error=type(exc).__name__)
        await _mark_failed(
            document_id,
            "The job queue is unreachable and the indexing code is not installed beside "
            "the API. Start Redis and the background worker "
            "(arq worker.tasks.WorkerSettings), then re-index.",
        )
        return
    try:
        async with system_session() as session:
            outcome = await ingest_pending_document(session, document_id)
    except Exception as exc:
        log.exception(
            "knowledge.inline_failed", document_id=str(document_id), error=type(exc).__name__
        )
        await _mark_failed(
            document_id,
            f"Indexing failed here ({type(exc).__name__}). Start the background worker "
            "and re-index.",
        )
        return
    log.info("knowledge.indexed_inline", document_id=str(document_id), outcome=outcome)


async def _mark_failed(document_id: uuid.UUID, reason: str) -> None:
    async with system_session() as session:
        document = await session.scalar(select(KbDocument).where(KbDocument.id == document_id))
        if document is None:
            return
        document.ingest_status = "failed"
        document.ingest_error = reason[:500]


@router.get("/documents/{document_id}", response_model=KbDocumentRow)
async def document(
    document_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.AGRONOMIST)],
) -> KbDocumentRow:
    return await _one(db, document_id)


@router.post("/documents/{document_id}/reindex", response_model=KbDocumentRow, status_code=202)
async def reindex(
    document_id: uuid.UUID,
    background: BackgroundTasks,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> KbDocumentRow:
    """Read the stored file or the site again, from the start."""
    doc = await _document(db, document_id)
    if doc.doc_type != "web" and not doc.file_key:
        raise ValidationError(
            "This document keeps no copy of its file to read again.",
            remedy="Upload it again. Rows loaded from the command line hold only their chunks.",
        )
    doc.ingest_status = "pending"
    doc.ingest_error = None
    doc.indexed_at = None
    await _queue(db, doc, principal, background, note="re-index", action=AuditAction.UPDATE)
    return await _one(db, doc.id)


# --------------------------------------------------------------------------- #
# Changing and removing
# --------------------------------------------------------------------------- #


@router.patch("/documents/{document_id}", response_model=KbDocumentRow)
async def set_published(
    document_id: uuid.UUID,
    body: PublishBody,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> KbDocumentRow:
    doc = await _document(db, document_id)
    if body.isPublished is None and body.scope is None:
        raise ValidationError(
            "Nothing to change.",
            remedy="Send whether it is live, which calls may use it, or both.",
        )
    if body.isPublished and doc.ingest_status != "indexed":
        raise ValidationError(
            "This document has not been indexed yet.",
            remedy="Wait for indexing to finish, or fix what it reported.",
        )
    before = {"is_published": doc.is_published, "scope": doc.scope}
    if body.scope is not None:
        doc.scope = body.scope
    if body.isPublished is not None:
        doc.is_published = body.isPublished
        if body.isPublished:
            doc.approved_by_user_id = principal.user_id
            doc.approved_at = datetime.now(UTC)
    await append_audit(
        db,
        action=AuditAction.PUBLISH if body.isPublished else AuditAction.UPDATE,
        resource_type="kb_document",
        resource_id=str(doc.id),
        actor_user_id=principal.user_id,
        before=before,
        after={"is_published": doc.is_published, "scope": doc.scope},
    )
    return await _one(db, doc.id)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> None:
    doc = await _document(db, document_id)
    # Unpublished first so retrieval stops serving it in the same transaction
    # the panel stops showing it; the chunks go with the row's soft delete.
    doc.is_published = False
    doc.deleted_at = datetime.now(UTC)
    await append_audit(
        db,
        action=AuditAction.DELETE,
        resource_type="kb_document",
        resource_id=str(doc.id),
        actor_user_id=principal.user_id,
        before={"title": doc.title},
    )
    if doc.file_key:
        await object_store().delete(doc.file_key)


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #


@router.post("/ask", response_model=AskResult)
async def ask(body: Ask, _: Annotated[Principal, require_role(Role.AGRONOMIST)]) -> AskResult:
    global _last_retrieval_ms
    language = _base_language(body.language or get_defaults().default_language)
    result: dict[str, Any] = await WorkerClient().search(
        body.question, language=language, answer=body.answer, direction=body.direction
    )
    answer = result.get("answer")
    retrieval_ms = int(result.get("retrievalMs") or 0)
    _last_retrieval_ms = retrieval_ms
    return AskResult(
        passages=[
            Passage(
                documentTitle=str(p.get("documentTitle") or ""),
                section=p.get("section"),
                snippet=str(p.get("snippet") or ""),
                score=float(p.get("score") or 0.0),
            )
            for p in result.get("passages") or []
            if isinstance(p, dict)
        ],
        retrievalMs=retrieval_ms,
        degraded=bool(result.get("degraded")),
        answer=Answer(
            text=answer.get("text"), totalMs=answer.get("totalMs"), note=answer.get("note")
        )
        if isinstance(answer, dict)
        else None,
    )


def _scope_or_both(value: str | None) -> str:
    """A scope from a form field, or the safe default.

    Multipart carries strings, not enums, so this is the one place the value
    is checked. Anything unrecognised becomes ``both`` rather than an error:
    the operator asked to add a document, and refusing the upload over a
    malformed hidden field would lose the file they just chose.
    """
    return value if value in ("inbound", "outbound", "both") else "both"


def _title_from(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return " ".join(stem.replace("_", " ").replace("-", " ").split()) or filename


def _base_language(code: str) -> str:
    return code.split("-")[0].lower() if code else "hi"


__all__ = ("INLINE_MARKER", "KbDocumentRow", "KnowledgeStatus", "list_documents", "router")
