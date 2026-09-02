"""Hybrid retrieval: BM25 + dense, fused with RRF (§9 Tier 2).

The shape §9 specifies:

    BM25 (tsvector)  ─┐
                      ├─▶ Reciprocal Rank Fusion (k=60) ─▶ rerank top-20 ─▶ top-4
    Dense (pgvector) ─┘

Four things here are load-bearing, and each exists because the obvious
alternative fails in a specific way:

**RRF fuses ranks, not scores.** A BM25 score and a cosine distance are not on
the same scale and normalising them into one is guesswork that changes with the
corpus. Rank position is comparable by construction. ``k=60`` is the standard
damping constant: it keeps the top few results dominant without letting a single
list's first place win outright.

**Both languages, every time.** §9 requires retrieving in the caller's language
*and* English before fusing. Hindi agronomy prose is thin; the English corpus is
deeper and the model translates the answer anyway. Searching only the caller's
language is how a system with the right document in it returns nothing.

**Retrieved text is data, never instruction** (§16.3). Chunks come back wrapped
in delimiters, and the caller passes them to the model as untrusted reference
material. An admin-uploadable knowledge base is a live prompt-injection surface.

**A dose never comes from prose.** §23-3 and §16.2: chunks carrying a dosage are
retrievable as context but are flagged, and the caller must route a dosage
question to ``recommend_for_crop``. Retrieval returning a plausible dose out of a
document is exactly the failure the approval workflow exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import Float, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import KbChunk, KbDocument

from ..text.script import words
from .chunking import carries_dose
from .embeddings import DIMENSIONS, E5Embedder

log = structlog.get_logger(__name__)

#: §9. The damping constant in reciprocal rank fusion.
RRF_K = 60

#: §9: fuse to twenty, rerank, keep four.
FUSE_LIMIT = 20
FINAL_LIMIT = 4

#: pgvector's HNSW search breadth. Well above the default 40 and above
#: CANDIDATE_LIMIT, so a request for 30 neighbours is answered from a candidate
#: pool it cannot exhaust. An interpolated constant, never request data.
HNSW_EF_SEARCH = 200

#: Per-list depth. Deeper than FUSE_LIMIT on purpose: a document that is tenth
#: on both lists should beat one that is first on a single list, and it cannot
#: if the lists are cut before fusion sees it.
CANDIDATE_LIMIT = 30

#: §16.3. The model is told content between these is reference material, never
#: an instruction.
OPEN_DELIMITER = "<reference>"
CLOSE_DELIMITER = "</reference>"


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One passage, with everything a citation needs (§9)."""

    chunk_id: str
    document_id: str
    document_title: str
    content: str
    section_path: str
    language: str
    score: float
    source: str | None = None
    contains_dose: bool = False
    #: Which retrievers found it. Useful in the eval harness: a corpus where
    #: dense never contributes is a corpus that did not need embedding.
    found_by: tuple[str, ...] = ()

    def as_context(self) -> str:
        """The form passed to the model, delimited per §16.3."""
        header = f"[{self.document_title}"
        if self.section_path:
            header += f" > {self.section_path}"
        header += f"] (id={self.chunk_id})"
        return f"{OPEN_DELIMITER}\n{header}\n{self.content}\n{CLOSE_DELIMITER}"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """What retrieval found, and how."""

    chunks: tuple[RetrievedChunk, ...] = ()
    #: True when the dense half did not run. The answer is still grounded, but
    #: recall is lower and the operator should know why.
    degraded: bool = False
    degraded_reason: str | None = None
    languages_searched: tuple[str, ...] = ()
    #: True when nothing corroborated the match -- see :func:`_is_weak`. The
    #: chunks are still returned, because a weak match is often a real answer;
    #: what changes is that the agent is told to check the reference actually
    #: addresses the question before answering from it.
    weak: bool = False

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        """Recorded on the turn. §9 makes an uncitable claim a test failure."""
        return tuple(c.chunk_id for c in self.chunks)

    def as_context(self) -> str:
        return "\n\n".join(c.as_context() for c in self.chunks)


@dataclass
class HybridRetriever:
    """Tier-2 retrieval over ``kb_chunks``."""

    embedder: E5Embedder | None = None
    #: Set when the embedder failed to load, so every result says so.
    dense_unavailable_reason: str | None = None
    _reranker: Reranker | None = field(default=None, repr=False)

    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        language: str = "hi",
        crop: str | None = None,
        scope: str | None = None,
        limit: int = FINAL_LIMIT,
    ) -> RetrievalResult:
        """Retrieve, fuse and rerank.

        ``scope`` is the direction of the call this is being asked for --
        ``inbound`` or ``outbound``. A document marked for the other direction
        is not returned, so a campaign's offer sheet is not read out on the
        helpline three weeks later. ``None`` searches everything, which is what
        the ingest CLI and the retrieval eval want.
        """
        query = query.strip()
        if not query:
            return RetrievalResult()

        # §9: the caller's language and English, always. `dict.fromkeys` keeps
        # order while removing the duplicate when the caller speaks English.
        languages = tuple(dict.fromkeys([_base_language(language), "en"]))

        scopes = _scopes_for(scope)
        lexical = await self._bm25(session, query, languages=languages, crop=crop, scopes=scopes)
        dense, degraded, reason = await self._dense(
            session, query, languages=languages, crop=crop, scopes=scopes
        )

        fused = _reciprocal_rank_fusion({"bm25": lexical, "dense": dense}, limit=FUSE_LIMIT)
        ranked = await self._rerank(query, fused, limit=limit)

        log.info(
            "knowledge.retrieved",
            query_tokens=len(query.split()),
            languages=languages,
            bm25=len(lexical),
            dense=len(dense),
            returned=len(ranked),
            degraded=degraded,
        )
        return RetrievalResult(
            chunks=tuple(ranked),
            degraded=degraded,
            degraded_reason=reason,
            languages_searched=languages,
            weak=_is_weak(ranked),
        )

    async def _bm25(
        self,
        session: AsyncSession,
        query: str,
        *,
        languages: Sequence[str],
        crop: str | None,
        scopes: tuple[str, ...] | None = None,
    ) -> list[RetrievedChunk]:
        """Full-text search over ``kb_chunks.search_vector``.

        Three things about the query, each fixing a way the obvious version
        silently returns nothing:

        **Terms are OR-ed, not AND-ed.** ``websearch_to_tsquery`` requires
        *every* term, which is right for a search box and wrong for speech. A
        farmer asks a whole sentence -- "how to handle an interruption politely"
        -- and one word the corpus happens not to contain ("politely") takes the
        entire query to zero hits. Measured: that exact query matched nothing
        while "interruption" alone matched. ``ts_rank_cd`` then does the work
        AND was doing, ranking a chunk that matches four terms above one that
        matches one, without discarding the second.

        **Both text-search configurations**, matching how migration 0003
        indexes. ``english`` stems romanised Hindi apart -- "khaad" and "khad" --
        while ``simple`` gives up English morphology, so "services" stops
        matching a document about a service.

        **The query is tokenised here, not in SQL.** ``to_tsquery`` takes
        operator syntax and raises on a stray ``&`` or ``!``, which a transcript
        will eventually contain. Terms come from :func:`words`, which keeps
        Devanagari matras and drops everything that is not a word character, so
        what reaches Postgres cannot carry an operator.
        """
        terms = [t for t in words(query) if len(t) > 1][:24]
        if not terms:
            return []
        ored = " | ".join(terms)
        tsquery = func.to_tsquery("simple", ored).op("||")(func.to_tsquery("english", ored))
        statement = (
            select(
                KbChunk.id,
                KbChunk.document_id,
                KbDocument.title,
                KbChunk.content,
                KbChunk.section_path,
                KbChunk.language,
                KbDocument.source,
                func.ts_rank_cd(KbChunk.search_vector, tsquery).label("rank"),
            )
            .join(KbDocument, KbChunk.document_id == KbDocument.id)
            .where(
                KbDocument.is_published.is_(True),
                KbChunk.language.in_(list(languages)),
                KbChunk.search_vector.op("@@")(tsquery),
            )
        )
        if scopes is not None:
            statement = statement.where(KbDocument.scope.in_(list(scopes)))
        statement = statement.order_by(text("rank DESC")).limit(CANDIDATE_LIMIT)
        if crop:
            statement = statement.where(KbChunk.crop_tags.contains([crop]))

        rows = (await session.execute(statement)).all()
        return [_row_to_chunk(row, found_by="bm25") for row in rows]

    async def _dense(
        self,
        session: AsyncSession,
        query: str,
        *,
        languages: Sequence[str],
        crop: str | None,
        scopes: tuple[str, ...] | None = None,
    ) -> tuple[list[RetrievedChunk], bool, str | None]:
        """Nearest neighbours over the pgvector HNSW index."""
        if self.embedder is None or not self.embedder.ready:
            reason = self.dense_unavailable_reason or "the embedding model is not loaded"
            return [], True, reason

        # HNSW is an *approximate* index, and pgvector's default ``ef_search``
        # of 40 is tuned for throughput. That default made this retriever
        # non-reproducible: two runs of identical code over an identical corpus
        # disagreed about which chunks came back, because the graph is built
        # with randomised level assignment and a candidate sitting at rank four
        # falls in or out depending on which graph this database happened to
        # build. Embeddings were verified bit-identical first, so the index was
        # the only remaining source.
        #
        # Recall is worth more than the microseconds here: the retriever asks
        # for 30 candidates and hands 4 to the model, so a miss at the boundary
        # is a wrong answer while a slower search is a rounding error against
        # §7's budget. Transaction-local, so it cannot leak into another
        # request that borrows this pooled connection.
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"))

        vector = await self.embedder.embed_query(query)
        if len(vector) != DIMENSIONS:  # pragma: no cover - guards a model swap
            return [], True, f"embedding is {len(vector)}-dimensional, expected {DIMENSIONS}"

        # pgvector's own comparator, not a hand-rolled `op("<=>")`: it binds the
        # vector through the Vector type so the values never enter the SQL text,
        # and it emits the operator the HNSW vector_cosine_ops index is built
        # for. Spelling the operator out by hand type-checks and then fails at
        # bind time, which is a slow way to learn this.
        distance = KbChunk.embedding.cosine_distance(vector)
        statement = (
            select(
                KbChunk.id,
                KbChunk.document_id,
                KbDocument.title,
                KbChunk.content,
                KbChunk.section_path,
                KbChunk.language,
                KbDocument.source,
                (1 - distance).cast(Float).label("rank"),
            )
            .join(KbDocument, KbChunk.document_id == KbDocument.id)
            .where(
                KbDocument.is_published.is_(True),
                KbChunk.embedding.is_not(None),
                KbChunk.language.in_(list(languages)),
            )
        )
        if scopes is not None:
            statement = statement.where(KbDocument.scope.in_(list(scopes)))
        statement = statement.order_by(distance).limit(CANDIDATE_LIMIT)
        if crop:
            statement = statement.where(KbChunk.crop_tags.contains([crop]))

        rows = (await session.execute(statement)).all()
        return [_row_to_chunk(row, found_by="dense") for row in rows], False, None

    async def _rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:
        if self._reranker is not None:
            return await self._reranker.rerank(query, chunks, limit=limit)
        return list(chunks[:limit])


class Reranker:
    """Cross-encoder reranking of the fused top-20 down to top-4 (§9).

    Specified by §9, deliberately not enabled, and the reason is measured
    rather than assumed.

    ``jinaai/jina-reranker-v2-base-multilingual`` (int8 ONNX) was run over the
    seed corpus against the twenty §21 gate queries and six irrelevant ones:

    - **396 ms per pair** on CPU. §9's top-20 rerank is therefore about eight
      seconds, against a 1,500 ms budget for an entire tool-calling turn. Even
      reranking four candidates would spend the whole budget.
    - **No separation.** Worst real query scored -2.105; best nonsense scored
      -2.101. It does not distinguish relevant from irrelevant on this corpus
      any better than the bi-encoder does.

    So it costs five turn budgets and buys nothing measurable *here*. That is a
    statement about eighteen chunks of a seed document, not about reranking in
    general, which is why the seam stays: when UA Agro's real agronomy corpus
    lands, this should be re-measured before it is written off. A GPU or a
    smaller cross-encoder changes the first number; a real corpus changes the
    second.
    """

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:  # pragma: no cover - no implementation yet
        raise NotImplementedError


def _is_weak(chunks: Sequence[RetrievedChunk]) -> bool:
    """Whether nothing corroborated the top match.

    This is deliberately **not** a similarity threshold, because measurement
    says a threshold does not exist on this model. Over the seed corpus and the
    twenty §21 gate queries plus six deliberately irrelevant ones:

    ======================================  ==========  ==========  ==========
    Signal                                  real (min)  junk (max)  separation
    ======================================  ==========  ==========  ==========
    e5 cosine, absolute                         0.7861      0.7959     -0.0098
    e5 cosine minus corpus median               0.0079      0.0188     -0.0108
    jina-reranker-v2 cross-encoder logit       -2.105      -2.101      -0.004
    ======================================  ==========  ==========  ==========

    Every separation is *negative*: the worst real question scores below the
    best nonsense one. Any cut-off either rejects real questions or admits
    nonsense, and picking one would encode a false confidence into the system.
    The cross-encoder -- which §9 specifies precisely to solve this -- does not
    separate either, and costs 396 ms per pair measured, so the §9 top-20 rerank
    would be about eight seconds against a 1,500 ms turn budget.

    Some of that is the corpus: eighteen chunks of a document that describes
    what content *should* exist rather than being the content. §9 calls it "a
    seed and a schema, not a finished corpus". The thresholds should be revisited
    against UA Agro's real agronomy content with the Phase 4 eval harness.

    So corroboration is used instead of confidence. A chunk both retrievers
    found is a match two independent methods agree on. A chunk only the vector
    index found may still be right -- BM25 cannot match Hindi against English at
    all -- but it is the case where nothing checks the embedding's work, so the
    agent is told to verify relevance before answering rather than being handed
    it as established.
    """
    if not chunks:
        return True
    return "bm25" not in chunks[0].found_by


def _reciprocal_rank_fusion(
    lists: dict[str, Sequence[RetrievedChunk]], *, limit: int
) -> list[RetrievedChunk]:
    """Fuse ranked lists by ``sum(1 / (k + rank))`` (§9, k=60).

    Ranks, not scores: a BM25 score and a cosine similarity live on different
    scales, and any normalisation between them is a guess that shifts with the
    corpus. Position is comparable without assuming anything.
    """
    scores: dict[str, float] = {}
    best: dict[str, RetrievedChunk] = {}
    sources: dict[str, list[str]] = {}

    for name, chunks in lists.items():
        for rank, chunk in enumerate(chunks, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(chunk.chunk_id, []).append(name)
            best.setdefault(chunk.chunk_id, chunk)

    ordered = sorted(scores, key=lambda cid: (-scores[cid], cid))
    return [
        _with(best[cid], score=scores[cid], found_by=tuple(sources[cid])) for cid in ordered[:limit]
    ]


def _with(chunk: RetrievedChunk, *, score: float, found_by: tuple[str, ...]) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_title=chunk.document_title,
        content=chunk.content,
        section_path=chunk.section_path,
        language=chunk.language,
        score=score,
        source=chunk.source,
        contains_dose=chunk.contains_dose,
        found_by=found_by,
    )


def _row_to_chunk(row: Any, *, found_by: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=str(row.id),
        document_id=str(row.document_id),
        document_title=row.title,
        content=row.content,
        section_path=row.section_path or "",
        language=row.language,
        score=float(row.rank or 0.0),
        source=row.source,
        contains_dose=carries_dose(row.content),
        found_by=(found_by,),
    )


def _scopes_for(scope: str | None) -> tuple[str, ...] | None:
    """Which document scopes a call of this direction may quote.

    Always the shared material plus the one direction. ``None`` -- and any
    value that is not a direction -- means no filter, which is what the ingest
    CLI and the retrieval eval want: they are inspecting the corpus, not
    answering a farmer.
    """
    if scope in ("inbound", "outbound"):
        return ("both", scope)
    return None


def _base_language(language: str) -> str:
    """``hi-IN`` -> ``hi``. Chunks are tagged by language, not by locale."""
    return language.split("-")[0].lower()


__all__ = (
    "CLOSE_DELIMITER",
    "FINAL_LIMIT",
    "FUSE_LIMIT",
    "OPEN_DELIMITER",
    "RRF_K",
    "HybridRetriever",
    "Reranker",
    "RetrievalResult",
    "RetrievedChunk",
)
