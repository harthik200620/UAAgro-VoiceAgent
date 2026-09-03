"""Crops, agronomist-approved recommendations and the knowledge layer (§9).

``crop_recommendations`` is the single most important safety control in the
platform. An LLM inventing a pesticide dose is not a bad customer experience --
it is a crop loss and a poisoning risk (§9). Every row is invisible to the agent
until an agronomist approves it, and that invisibility is enforced three ways:
a partial index, a query-side filter, and a test that proves it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uaagro_domain.enums import ApprovalState, DoseBasis, Season

from ..base import Base, StandardMixin, UserFacingMixin, enum_column, uuid_fk, uuid_pk
from ..types import Vector768


class Crop(Base, StandardMixin):
    __tablename__ = "crops"

    id: Mapped[uuid.UUID] = uuid_pk()
    name_en: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    name_hi: Mapped[str] = mapped_column(String(120), nullable=False)
    name_local: Mapped[str | None] = mapped_column(String(120), nullable=True)
    season: Mapped[Season] = enum_column(Season, constraint_name="crop_season_valid", index=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: ``[{key, name_hi, name_en, order}]`` -- sowing, tillering, flowering ...
    growth_stages: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )

    problems: Mapped[list[CropProblem]] = relationship(
        back_populates="crop", cascade="all, delete-orphan"
    )


class CropProblem(Base, StandardMixin):
    """A symptom as the farmer describes it, mapped to a problem class.

    KB §6: farmers report symptoms, not diagnoses. ``aliases`` carries the
    spoken forms -- झुलसा, माहू, सुंडी -- which feed both intent classification
    and the ASR lexicon.
    """

    __tablename__ = "crop_problems"
    __table_args__ = (Index("ix_crop_problems_aliases", "aliases", postgresql_using="gin"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    #: NULL for a symptom that spans crops -- yellowing leaves mean much the
    #: same on wheat and on paddy -- and set when the problem is crop-specific.
    crop_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("crops.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    problem_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    name_hi: Mapped[str] = mapped_column(String(160), nullable=False)
    symptoms_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    symptoms_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    #: True when the symptom is genuinely ambiguous and the agent must ask
    #: before advising -- yellowing leaves are nitrogen, water or disease
    #: (KB §6), and guessing then prescribing for the guess is forbidden.
    requires_clarification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    crop: Mapped[Crop | None] = relationship(back_populates="problems")


class CropRecommendation(Base, UserFacingMixin):
    """An agronomist-approved product recommendation with a dose.

    Nothing here is generated. The agent may read an approved row and scale it
    to the farmer's plot; it may never invent, adjust or substitute a figure
    (§9, §16.2, §23-3).
    """

    __tablename__ = "crop_recommendations"
    __table_args__ = (
        # The agent's only read path. Partial, so an unapproved row is not even
        # in the index the query planner reaches for.
        Index(
            "ix_crop_reco_servable",
            "crop_id",
            "growth_stage",
            "problem_id",
            postgresql_where=text("approval_state = 'approved' AND deleted_at IS NULL"),
        ),
        Index("ix_crop_reco_approval_queue", "approval_state", "created_at"),
        CheckConstraint("dose_value > 0", name="dose_positive"),
        CheckConstraint(
            "max_applications IS NULL OR max_applications > 0", name="max_applications_positive"
        ),
        # Approval must be attributable: §18 keeps every recommendation
        # traceable to an agronomist-signed row, which is what protects UA Agro
        # if a recommendation is ever disputed.
        CheckConstraint(
            "approval_state <> 'approved' "
            "OR (approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="approved_requires_approver",
        ),
        # KB §5: a crop-protection recommendation must carry its pre-harvest
        # interval and at least one precaution before it can be approved.
        CheckConstraint(
            "approval_state <> 'approved' OR NOT is_crop_protection "
            "OR (phi_days IS NOT NULL AND precaution_note_hi IS NOT NULL)",
            name="crop_protection_needs_phi_and_precaution",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    crop_id: Mapped[uuid.UUID] = uuid_fk("crops.id", ondelete="CASCADE")
    growth_stage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    problem_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    problem_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("crop_problems.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    product_variant_id: Mapped[uuid.UUID] = uuid_fk("product_variants.id")

    dose_value: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    dose_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    dose_basis: Mapped[DoseBasis] = enum_column(DoseBasis, constraint_name="dose_basis_valid")

    application_method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    timing_note_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    max_applications: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    #: Pre-harvest interval. Spoken with every crop-protection recommendation.
    phi_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    precaution_note_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    precaution_note_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Denormalised from the product so the CHECK constraint above can see it.
    is_crop_protection: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("100"))
    region_scope: Mapped[str | None] = mapped_column(String(80), nullable=True)
    season: Mapped[Season | None] = enum_column(
        Season, constraint_name="reco_season_valid", nullable=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    approval_state: Mapped[ApprovalState] = enum_column(
        ApprovalState,
        constraint_name="approval_state_valid",
        default=ApprovalState.DRAFT,
        index=True,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    @property
    def is_servable(self) -> bool:
        """Whether the agent may speak this row.

        Callers must still filter in SQL -- this property is a readable
        assertion for tests and the admin panel, not the enforcement point.
        """
        return (
            self.approval_state is ApprovalState.APPROVED
            and self.approved_by_user_id is not None
            and self.deleted_at is None
        )


class KbDocument(Base, UserFacingMixin):
    """A source document behind Tier-2 retrieval (§9)."""

    __tablename__ = "kb_documents"
    __table_args__ = (
        Index("ix_kb_documents_published", "is_published", "language"),
        Index("ix_kb_documents_scope", "is_published", "scope", "language"),
        CheckConstraint("scope IN ('inbound', 'outbound', 'both')", name="kb_document_scope_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(12), nullable=False)
    source: Mapped[str | None] = mapped_column(String(300), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    file_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Skip re-embedding when the content has not changed (§9).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Where a panel upload is in its background pipeline: ``pending`` (queued),
    #: ``indexing`` (extracting, chunking, embedding), ``indexed`` or ``failed``
    #: with the reason in ``ingest_error``. Rows ingested by the CLI are indexed
    #: by the time they exist.
    ingest_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'indexed'")
    )
    ingest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What the panel shows beside the title. The size is the stored object's
    #: (an upload's bytes, a note's UTF-8, a crawl's extracted text); the word
    #: count is of the text that was actually chunked, written when indexing
    #: finishes -- so a document with 40 chunks and 12 words is a scan.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Which calls may quote this: ``inbound`` for the helpline, ``outbound``
    #: for campaign calls, ``both`` for the crop and product material that
    #: belongs on either. Retrieval filters on it (§9), so a document out of
    #: scope is not merely hidden in the panel -- the agent cannot reach it.
    scope: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'both'"))
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    chunks: Mapped[list[KbChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KbChunk(Base, StandardMixin):
    """One retrievable passage.

    Retrieved text is data, never instruction (§16.3). The retrieval layer wraps
    every chunk in delimiters and the system prompt states that content inside
    them is untrusted -- an uploaded PDF is an injection vector on a KB that
    admins can add to.
    """

    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_kb_chunk_position"),
        Index(
            "ix_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        Index("ix_kb_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_kb_chunks_crop_tags", "crop_tags", postgresql_using="gin"),
        Index("ix_kb_chunks_product_tags", "product_tags", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = uuid_fk("kb_documents.id", ondelete="CASCADE")
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(12), nullable=False, index=True)

    embedding: Mapped[list[float] | None] = mapped_column(Vector768, nullable=True)
    #: Built with the 'simple' **and** 'english' configs unioned (migration
    #: 0003). Either alone loses matches silently: 'english' stems romanised
    #: Hindi apart, 'simple' gives up English morphology. Retrieval must query
    #: both, or it asks for lexemes the index does not hold.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)

    crop_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    product_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    section_path: Mapped[str | None] = mapped_column(String(400), nullable=True)

    document: Mapped[KbDocument] = relationship(back_populates="chunks")


class AnswerCache(Base, UserFacingMixin):
    """Tier-3 curated answers for the head of the query distribution (§9).

    Roughly forty questions cover half an agri helpline's inbound volume. A hit
    skips the LLM entirely and, with pre-synthesised audio, the TTS too.

    Anything containing a price or stock figure is never cached -- those go to
    Tier 1 on every turn. ``contains_volatile_data`` makes that a stored,
    testable property rather than an operator's good intention.
    """

    __tablename__ = "answer_cache"
    __table_args__ = (
        UniqueConstraint("intent_key", "language", name="uq_answer_cache_intent_language"),
        Index("ix_answer_cache_variants", "question_variants", postgresql_using="gin"),
        CheckConstraint(
            "NOT (is_active AND contains_volatile_data)",
            name="volatile_answers_never_cached",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    intent_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(12), nullable=False)
    question_variants: Mapped[list[str]] = mapped_column(
        ARRAY(String(300)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Pre-synthesised 8 kHz audio in the telephony codec, if available.
    audio_object_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_refs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("86400"))
    contains_volatile_data: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
