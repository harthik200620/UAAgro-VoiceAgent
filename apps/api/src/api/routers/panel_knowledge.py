"""What the agent knows, as the panel manages it (§9, §15.1).

An upload is accepted fast and indexed slowly. The API stores the file,
writes the document row with ``ingest_status='pending'`` and hands the id to
the background worker; the row is the progress report the panel polls.
Nothing the agent could say changes until the worker has extracted, chunked,
embedded and published -- which is also why "switch off" is instant: it flips
the flag retrieval filters on.

"Try a question" asks the voice worker, not this process. The worker has the
retriever and its model warm; the answer the panel shows is the answer a
caller would get, because it is produced by the same code.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.models import KbChunk, KbDocument
from uaagro_db.storage import ObjectStore, knowledge_key
from uaagro_domain.enums import AuditAction, Role
from uaagro_domain.errors import NotFoundError, ValidationError
from uaagro_domain.settings import get_defaults

from ..security.deps import DbDep, Principal, require_role
from ..services import jobs
from ..services.worker_client import WorkerClient

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/knowledge", tags=["panel"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_SUPPORTED = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".csv": "csv",
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
}


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
    updatedAt: str


class CrawlBody(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    title: str | None = Field(default=None, max_length=300)
    maxPages: int = Field(default=100, ge=1, le=500)
    language: str | None = None


class PublishBody(BaseModel):
    isPublished: bool


class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    answer: bool = False
    language: str | None = None


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
        updatedAt=doc.updated_at.isoformat(),
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


@router.post("/documents", response_model=KbDocumentRow, status_code=202)
async def add_document(
    request: Request,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
    file: Annotated[UploadFile | None, File()] = None,
    title: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
) -> KbDocumentRow:
    """A file (multipart) or a website (JSON). Either way, indexing is queued."""
    content_type = request.headers.get("content-type", "")
    default_language = _base_language(get_defaults().default_language)

    if "application/json" in content_type:
        body = CrawlBody.model_validate(await request.json())
        if not body.url.lower().startswith(("http://", "https://")):
            raise ValidationError("That is not a web address.", remedy="Start it with https://")
        document = KbDocument(
            organization_id=principal.organization_id,
            title=(body.title or body.url).strip()[:300],
            doc_type="web",
            language=(body.language or default_language)[:12],
            source=body.url.strip(),
            version=0,
            content_hash=hashlib.sha256(body.url.encode()).hexdigest(),
            uploaded_by_user_id=principal.user_id,
            ingest_status="pending",
            created_by=principal.user_id,
        )
        db.add(document)
        await db.flush()
        await _queue(db, document, principal, note=f"crawl up to {body.maxPages} pages")
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
        content_hash=hashlib.sha256(data).hexdigest(),
        uploaded_by_user_id=principal.user_id,
        ingest_status="pending",
        created_by=principal.user_id,
    )
    db.add(document)
    await db.flush()
    key = knowledge_key(str(document.id), file.filename)
    await ObjectStore().put(key, data, content_type=file.content_type or "application/octet-stream")
    document.file_key = key
    await _queue(db, document, principal, note=f"{len(data)} bytes")
    return await _one(db, document.id)


async def _queue(
    db: AsyncSession, document: KbDocument, principal: Principal, *, note: str
) -> None:
    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="kb_document",
        resource_id=str(document.id),
        actor_user_id=principal.user_id,
        after={"title": document.title, "doc_type": document.doc_type, "note": note},
    )
    # Committed before the job is enqueued: the worker must find the row.
    await db.commit()
    await jobs.enqueue("ingest_document", str(document.id))


@router.get("/documents/{document_id}", response_model=KbDocumentRow)
async def document(
    document_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.AGRONOMIST)],
) -> KbDocumentRow:
    return await _one(db, document_id)


@router.patch("/documents/{document_id}", response_model=KbDocumentRow)
async def set_published(
    document_id: uuid.UUID,
    body: PublishBody,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> KbDocumentRow:
    doc = await db.scalar(
        select(KbDocument).where(KbDocument.id == document_id, KbDocument.deleted_at.is_(None))
    )
    if doc is None:
        raise NotFoundError(resource="document", identifier=str(document_id))
    if body.isPublished and doc.ingest_status != "indexed":
        raise ValidationError(
            "This document has not been indexed yet.",
            remedy="Wait for indexing to finish, or fix what it reported.",
        )
    before = doc.is_published
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
        before={"is_published": before},
        after={"is_published": doc.is_published},
    )
    return await _one(db, doc.id)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> None:
    doc = await db.scalar(
        select(KbDocument).where(KbDocument.id == document_id, KbDocument.deleted_at.is_(None))
    )
    if doc is None:
        raise NotFoundError(resource="document", identifier=str(document_id))
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
        await ObjectStore().delete(doc.file_key)


@router.post("/ask", response_model=AskResult)
async def ask(body: Ask, _: Annotated[Principal, require_role(Role.AGRONOMIST)]) -> AskResult:
    language = _base_language(body.language or get_defaults().default_language)
    result: dict[str, Any] = await WorkerClient().search(
        body.question, language=language, answer=body.answer
    )
    answer = result.get("answer")
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
        retrievalMs=int(result.get("retrievalMs") or 0),
        degraded=bool(result.get("degraded")),
        answer=Answer(
            text=answer.get("text"), totalMs=answer.get("totalMs"), note=answer.get("note")
        )
        if isinstance(answer, dict)
        else None,
    )


def _title_from(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return " ".join(stem.replace("_", " ").replace("-", " ").split()) or filename


def _base_language(code: str) -> str:
    return code.split("-")[0].lower() if code else "hi"


__all__ = ("KbDocumentRow", "list_documents", "router")
