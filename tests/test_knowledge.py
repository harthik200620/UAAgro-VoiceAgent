"""Chunking, ingestion and Tier-2/3 retrieval (§9).

The suite splits in two. Chunking and fusion are pure and tested directly; the
retrieval gate ingests the real ``UA_AGRO_KNOWLEDGE_BASE.md`` into Postgres and
runs the twenty §21 queries against it.

Dense retrieval needs a downloaded model, so these run BM25-only unless one is
present. That is stated in the assertions rather than hidden: a BM25-only pass
is a weaker result than a hybrid pass, and a suite that did not distinguish them
would report the same green either way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.retrieval_queries import CASES, MINIMUM_HITS
from voice_worker.knowledge.chunking import (
    MAX_TOKENS,
    Chunk,
    carries_dose,
    chunk_markdown,
    detect_language,
    estimate_tokens,
    parse_blocks,
)
from voice_worker.knowledge.embeddings import DIMENSIONS, MODEL_NAME, E5Embedder, ensure_model
from voice_worker.knowledge.ingest import ingest_markdown
from voice_worker.knowledge.retrieval import (
    CLOSE_DELIMITER,
    OPEN_DELIMITER,
    RRF_K,
    HybridRetriever,
    RetrievedChunk,
    _reciprocal_rank_fusion,
)
from voice_worker.tools.base import ToolContext, ToolRegistry
from voice_worker.tools.knowledge import SearchKnowledge
from voice_worker.tools.session import reset_session_factory, set_session_factory

KB_PATH = Path("UA_AGRO_KNOWLEDGE_BASE.md")


def _read_kb() -> str:
    """The shipping corpus, or skip. Read synchronously on purpose: the async
    lint rule is about the media path, and a fixture is not one."""
    if not KB_PATH.is_file():  # pragma: no cover
        pytest.skip("knowledge base not present")
    return KB_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def test_a_table_is_never_split() -> None:
    """§9. A dosage table cut in half returns a quantity whose basis and
    pre-harvest interval are in the neighbouring chunk -- a dose stripped of
    every qualifier that made it safe."""
    markdown = "# Doses\n\n" + "\n".join(
        ["| crop | product | dose | PHI |", "| --- | --- | --- | --- |"]
        + [f"| crop{i} | product{i} | {i * 10} ml per acre | {i} days |" for i in range(80)]
    )
    chunks = chunk_markdown(markdown)
    table_chunks = [c for c in chunks if "| crop0 |" in c.content]
    assert len(table_chunks) == 1
    # Whole, even though that puts it over the window: an oversized table is a
    # worse chunk than a small one, and a bisected one is a safety problem.
    assert "| crop79 |" in table_chunks[0].content


def test_a_dose_keeps_the_line_that_introduced_it() -> None:
    markdown = (
        "# Spray\n\n"
        "Apply at flowering, in the evening:\n"
        "- Imidacloprid 17.8% SL, 100 ml per acre\n"
        "- Wait 21 days before harvest\n"
    )
    chunks = chunk_markdown(markdown)
    assert len(chunks) == 1
    assert "flowering" in chunks[0].content
    assert "100 ml" in chunks[0].content
    assert chunks[0].contains_dose


def test_chunks_carry_the_section_they_came_from() -> None:
    """§9 requires a citation, and a citation needs a section."""
    markdown = "# Doc\n\n## Section A\n\nText A.\n\n## Section B\n\nText B.\n"
    chunks = chunk_markdown(markdown)
    assert all(c.section_path.startswith("Doc") for c in chunks)


def test_unrelated_sections_are_not_merged() -> None:
    """Merging small siblings is fine; merging across top-level sections gives
    a chunk whose recorded section describes neither half."""
    markdown = (
        "# Doc\n\n## 1. Products\n\n### Fertiliser\n\nA.\n\n### Seeds\n\nB.\n\n"
        "## 2. Safety\n\n### Poisoning\n\nC.\n"
    )
    chunks = chunk_markdown(markdown)
    for chunk in chunks:
        assert not ("A." in chunk.content and "C." in chunk.content), (
            "a chunk spans two top-level sections"
        )


def test_a_merged_chunk_cites_the_parent_it_actually_spans() -> None:
    """§9 requires a citation, and a citation that names one subsection while
    the chunk holds a dozen is worse than none: it sends a reader to a heading
    whose text does not contain the sentence they were quoted."""
    markdown = "# Doc\n\n## Snippets\n\n" + "".join(
        f"### Case {i}\n\nShort line {i}.\n\n" for i in range(8)
    )
    chunks = chunk_markdown(markdown)
    for chunk in chunks:
        cases = [i for i in range(8) if f"Short line {i}." in chunk.content]
        if len(cases) > 1:
            assert chunk.section_path == "Doc > Snippets", (
                f"chunk spans cases {cases} but cites {chunk.section_path!r}"
            )


def test_prose_chunks_stay_inside_the_window() -> None:
    """§9's 300-500 token target. Tables are exempt; prose is not."""
    paragraph = "This is a sentence about wheat and fertiliser application. " * 200
    chunks = chunk_markdown(f"# Doc\n\n{paragraph}")
    assert len(chunks) > 1
    for chunk in chunks:
        # The overlap prefix is additive on top of the window, so the ceiling
        # is the window plus its share, not the window itself.
        assert chunk.tokens <= MAX_TOKENS * 1.3


@pytest.mark.parametrize(
    ("text_in", "expected"),
    [
        ("यूरिया की बोरी", "hi"),
        ("urea bag price", "en"),
        ("DAP 50 kg", "en"),
    ],
)
def test_language_detection(text_in: str, expected: str) -> None:
    assert detect_language(text_in) == expected


def test_devanagari_costs_more_tokens_per_character() -> None:
    """A multilingual sentencepiece vocabulary splits Devanagari finer. One
    ratio for both scripts would over-fill Hindi chunks and let the model
    truncate them."""
    hindi = "यूरिया की बोरी का दाम"
    latin = "urea bag price today"
    assert len(hindi) < len(latin) * 1.2
    assert estimate_tokens(hindi) > estimate_tokens(latin)


def test_the_real_knowledge_base_chunks_sensibly() -> None:
    """The document that actually ships (§9)."""
    chunks = chunk_markdown(_read_kb())

    assert len(chunks) >= 10
    assert all(c.content.strip() for c in chunks)
    assert {c.language for c in chunks} == {"hi", "en"}, "both languages must be represented"

    oversized = [c for c in chunks if c.tokens > MAX_TOKENS * 1.05]
    for chunk in oversized:
        rows = [line for line in chunk.content.splitlines() if line.strip().startswith("|")]
        assert rows, (
            f"a {chunk.tokens}-token chunk of prose in {chunk.section_path!r}: only "
            "tables may exceed the window"
        )


def test_blocks_track_heading_depth() -> None:
    blocks = parse_blocks("# A\n\ntext\n\n## B\n\ntext\n\n# C\n\ntext\n")
    paths = [b.section_path for b in blocks]
    assert paths == ["A", "A > B", "C"]


@pytest.mark.parametrize(
    "line",
    ["100 ml per acre", "2 बोरी प्रति एकड़", "1.5 kg", "250 ग्राम"],
)
def test_dose_lines_are_recognised(line: str) -> None:
    assert carries_dose(line)


def test_plain_prose_is_not_mistaken_for_a_dose() -> None:
    assert not carries_dose("The centre opens at 8 in the morning.")


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #


def _chunk(chunk_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="d",
        document_title="Doc",
        content=f"content {chunk_id}",
        section_path="Doc > Section",
        language="en",
        score=0.0,
    )


def test_rrf_rewards_agreement_between_retrievers() -> None:
    """§9 fuses ranks, not scores. A chunk both retrievers rank second should
    beat one that only a single retriever ranks first."""
    bm25 = [_chunk("a"), _chunk("shared")]
    dense = [_chunk("b"), _chunk("shared")]
    fused = _reciprocal_rank_fusion({"bm25": bm25, "dense": dense}, limit=3)
    assert fused[0].chunk_id == "shared"
    assert set(fused[0].found_by) == {"bm25", "dense"}


def test_rrf_uses_the_specified_constant() -> None:
    fused = _reciprocal_rank_fusion({"bm25": [_chunk("a")]}, limit=1)
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))


def test_fusion_is_stable_for_equal_scores() -> None:
    """Two chunks tying must order the same way every run, or the top-4 handed
    to the model changes between identical calls."""
    lists = {"bm25": [_chunk("b"), _chunk("a")]}
    first = [c.chunk_id for c in _reciprocal_rank_fusion(lists, limit=2)]
    second = [c.chunk_id for c in _reciprocal_rank_fusion(lists, limit=2)]
    assert first == second


def test_retrieved_text_is_wrapped_as_untrusted() -> None:
    """§16.3: an admin-uploadable KB is a prompt-injection surface, so every
    chunk reaches the model inside delimiters it is told not to obey."""
    context = _chunk("a").as_context()
    assert context.startswith(OPEN_DELIMITER)
    assert context.endswith(CLOSE_DELIMITER)
    assert "id=a" in context


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #


def test_a_missing_model_names_the_command_that_fetches_it(tmp_path: Path) -> None:
    """§0 rule 4. A model is a download, not a credential."""
    from uaagro_domain.errors import ConfigurationError

    with pytest.raises(ConfigurationError) as raised:
        ensure_model(tmp_path)
    assert MODEL_NAME in raised.value.remedy
    assert "hf_hub_download" in raised.value.remedy
    # And says what breaks without it, so "it still works" is not a reasonable
    # conclusion to draw from a passing test suite.
    assert "BM25-only" in raised.value.remedy


async def test_a_worker_without_the_model_still_boots() -> None:
    """A worker that refused to start could not answer the 70% of calls §9
    routes to Tier 1."""
    embedder = E5Embedder(Path("does/not/exist"))
    unavailable = await embedder.warm()
    assert unavailable is not None
    assert not embedder.ready
    assert unavailable.remedy


def test_the_vector_width_matches_the_column() -> None:
    """Changing the model means a migration, not a config edit."""
    from uaagro_db.models import KbChunk

    assert DIMENSIONS == KbChunk.__table__.c.embedding.type.dim


# --------------------------------------------------------------------------- #
# Retrieval against the real corpus -- the §21 Phase 3 gate
# --------------------------------------------------------------------------- #

pytest_plugins: tuple[str, ...] = ()


@pytest.fixture(scope="module")
def embedder() -> E5Embedder | None:
    """The real model if it has been downloaded, else None (BM25-only).

    Multi-threaded: a 500-token passage costs about two seconds on one core, so
    the single-thread setting a live worker uses would make ingesting the seed
    corpus a 35-second fixture. Nothing here is competing with an audio loop.
    """
    import asyncio
    import os

    candidate = E5Embedder(threads=max(1, (os.cpu_count() or 2) - 1))
    if asyncio.run(candidate.warm()) is not None:
        return None
    return candidate


KB_TITLE = "UA Agro Knowledge Base (test)"


@pytest.fixture
async def ingested(app_engine, embedder):  # type: ignore[no-untyped-def]
    """The real knowledge base, ingested and published, as the only corpus.

    Every other published document is removed first. Retrieval ranks across the
    whole published corpus, so a document another test left behind competes for
    the top four and moves the gate -- which is exactly what happened: two runs
    of identical code disagreed about which queries hit, because test order
    decided what else was in the index at the time. A gate that measures a
    different corpus on each run measures nothing.
    """
    markdown = _read_kb()
    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await session.execute(
            text("DELETE FROM kb_documents WHERE title <> :keep"), {"keep": KB_TITLE}
        )
        await session.commit()

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            await session.execute(
                text(
                    "SELECT set_config('app.role','voice_agent',true),"
                    "       set_config('app.centre_ids','',true)"
                )
            )
            yield session
            await session.commit()

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        report = await ingest_markdown(
            session,
            title=KB_TITLE,
            markdown=markdown,
            doc_type="knowledge_base",
            source="UA_AGRO_KNOWLEDGE_BASE.md",
            embedder=embedder,
            # Published directly: this fixture is exercising retrieval, and the
            # approval gate has its own test below.
            publish=True,
        )
        await session.commit()

    set_session_factory(factory)
    try:
        yield report, HybridRetriever(embedder=embedder)
    finally:
        reset_session_factory()
        # The document is deliberately left in place. Deleting it would make
        # every test in this module re-embed all 18 chunks -- around 25 seconds
        # each, and the twenty gate queries alone are twenty tests. Leaving it
        # lets the content-hash skip in `ingest_markdown` do what it exists for,
        # which also means this suite exercises the re-ingest path on every run
        # after the first.


@pytest.mark.integration
async def test_ingestion_indexes_the_corpus(ingested) -> None:  # type: ignore[no-untyped-def]
    report, _ = ingested
    assert report.chunks_written >= 10
    if report.chunks_unembedded:
        assert report.warnings, "un-embedded chunks must be reported, not silent"


@pytest.mark.integration
async def test_ingesting_twice_re_embeds_nothing(
    app_engine, embedder
) -> None:  # type: ignore[no-untyped-def]
    """§9: re-embed on version change only. Otherwise a one-line edit costs the
    whole corpus."""
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    markdown = "# Doc\n\n## S\n\n" + ("Wheat needs nitrogen at tillering. " * 40)

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        first = await ingest_markdown(
            session, title="dup-test", markdown=markdown, doc_type="test", embedder=embedder
        )
        second = await ingest_markdown(
            session, title="dup-test", markdown=markdown, doc_type="test", embedder=embedder
        )
        await session.execute(text("DELETE FROM kb_documents WHERE title = 'dup-test'"))
        await session.commit()

    assert first.chunks_written >= 1
    assert second.skipped_unchanged
    assert second.chunks_embedded == 0


@pytest.mark.integration
async def test_a_chunker_change_re_chunks_an_unchanged_document(
    app_engine, embedder, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Otherwise a chunker improvement never reaches a corpus already loaded.

    "Unchanged" is the expected result of re-ingesting a document, so a stale
    corpus and a correctly-skipped one look identical from the outside. This
    bit the suite itself: the section-path fix landed and the test database
    kept serving chunks built by the previous version.
    """
    from voice_worker.knowledge import ingest as ingest_module

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    markdown = "# Doc\n\n## S\n\nWheat needs nitrogen at tillering.\n"

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        first = await ingest_markdown(
            session, title="ver-test", markdown=markdown, doc_type="test", embedder=embedder
        )
        same = await ingest_markdown(
            session, title="ver-test", markdown=markdown, doc_type="test", embedder=embedder
        )
        monkeypatch.setattr(
            ingest_module, "CHUNKER_VERSION", ingest_module.CHUNKER_VERSION + 1
        )
        bumped = await ingest_markdown(
            session, title="ver-test", markdown=markdown, doc_type="test", embedder=embedder
        )
        await session.execute(text("DELETE FROM kb_documents WHERE title = 'ver-test'"))
        await session.commit()

    assert first.chunks_written >= 1
    assert same.skipped_unchanged, "identical text and chunker must skip"
    assert not bumped.skipped_unchanged, "a chunker bump must force a re-chunk"
    # Same text, so the vectors are reusable even though the document is not.
    assert bumped.chunks_embedded == 0
    assert bumped.chunks_reused >= 1


@pytest.mark.integration
async def test_an_edited_document_loses_its_approval(
    app_engine, embedder
) -> None:  # type: ignore[no-untyped-def]
    """The reviewer approved the text that was there, not what replaced it."""
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await ingest_markdown(
            session, title="edit-test", markdown="# A\n\nfirst.", doc_type="test",
            embedder=embedder, publish=True,
        )
        await session.execute(
            text(
                "UPDATE kb_documents SET approved_at = now(),"
                " approved_by_user_id = (SELECT id FROM users LIMIT 1)"
                " WHERE title = 'edit-test'"
            )
        )
        await ingest_markdown(
            session, title="edit-test", markdown="# A\n\nsecond, different.",
            doc_type="test", embedder=embedder,
        )
        row = (
            await session.execute(
                text(
                    "SELECT is_published, approved_at, version FROM kb_documents"
                    " WHERE title = 'edit-test'"
                )
            )
        ).one()
        await session.execute(text("DELETE FROM kb_documents WHERE title = 'edit-test'"))
        await session.commit()

    is_published, approved_at, version = row
    assert version == 2
    assert approved_at is None
    assert is_published is False


@pytest.mark.integration
async def test_an_unpublished_document_is_never_retrieved(
    app_engine, embedder
) -> None:  # type: ignore[no-untyped-def]
    """§9: ingestion is not publication. Uploading a file must not put its text
    in front of a caller."""
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    marker = "zylophonic tractor calibration"
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await ingest_markdown(
            session,
            title="draft-test",
            markdown=f"# Draft\n\n## S\n\n{marker} is described here.",
            doc_type="test",
            embedder=embedder,
            publish=False,
        )
        await session.commit()

    try:
        async with maker() as session:
            await session.execute(text("SELECT set_config('app.role','voice_agent',true)"))
            result = await HybridRetriever(embedder=embedder).search(
                session, marker, language="en-IN"
            )
        # Not "nothing came back" -- dense retrieval always returns its nearest
        # neighbours, and other published documents exist. The assertion is that
        # the draft's own text is not among them.
        assert all(marker not in c.content for c in result.chunks)
        assert "draft-test" not in {c.document_title for c in result.chunks}
    finally:
        async with maker() as session:
            await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
            await session.execute(text("DELETE FROM kb_documents WHERE title = 'draft-test'"))
            await session.commit()


async def _retrieve(retriever, app_engine, case) -> list[str]:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','voice_agent',true)"))
        result = await retriever.search(session, case.query, language=case.language)
    assert result.chunks, f"nothing retrieved for {case.query!r}"
    return [c.section_path for c in result.chunks]


def _params() -> list[object]:
    """One parameter per query, the known misses marked ``xfail``.

    ``strict=False`` deliberately. HNSW is an approximate index whose graph is
    built with randomised level assignment, so a database rebuilt from scratch
    -- which every test session does -- produces a slightly different one. A
    query whose answer sits at rank four can fall either side of the cut, and at
    least one of these does. Strict xfail would turn that into a red suite on
    the runs where retrieval did *better*, which is the wrong direction to
    punish. :func:`test_the_gate_does_not_regress` holds the total, so a real
    regression still fails.
    """
    out: list[object] = []
    for case in CASES:
        reason = case.known_miss or case.borderline
        marks = [pytest.mark.xfail(reason=reason, strict=False)] if reason else []
        out.append(pytest.param(case, marks=marks, id=case.expect_section[:18]))
    return out


@pytest.mark.integration
@pytest.mark.parametrize("case", _params())
async def test_the_twenty_seeded_queries_retrieve_the_right_section(
    ingested, app_engine, case
) -> None:  # type: ignore[no-untyped-def]
    """§21's Phase 3 gate, one query at a time.

    Five currently miss and are marked ``known_miss`` with the reason. They are
    kept in the set rather than removed: a gate pruned of the queries it fails
    measures whatever already passes.
    """
    _, retriever = ingested
    sections = await _retrieve(retriever, app_engine, case)
    assert any(case.expect_section in s for s in sections), (
        f"{case.query!r} did not retrieve {case.expect_section!r}; got {sections}"
    )


@pytest.mark.integration
async def test_the_gate_does_not_regress(
    ingested, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """The §21 number itself, so it can only move up.

    The per-query tests xfail the five known misses, which on its own would let
    retrieval quietly rot: break a passing query and its individual test simply
    turns red, but break several while fixing one and the arithmetic is easy to
    lose. This asserts the total.
    """
    _, retriever = ingested
    hits = []
    for case in CASES:
        sections = await _retrieve(retriever, app_engine, case)
        if any(case.expect_section in s for s in sections):
            hits.append(case.query)

    assert len(hits) >= MINIMUM_HITS, (
        f"retrieval regressed: {len(hits)}/{len(CASES)}, was {MINIMUM_HITS}. "
        f"Missing: {[c.query for c in CASES if c.query not in hits]}"
    )


@pytest.mark.integration
async def test_both_languages_are_always_searched(
    ingested, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """§9. Searching only the caller's language is how a system with the right
    document in it returns nothing."""
    _, retriever = ingested
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','voice_agent',true)"))
        result = await retriever.search(session, "soil testing", language="hi-IN")
    assert result.languages_searched == ("hi", "en")


# --------------------------------------------------------------------------- #
# The search_knowledge tool
# --------------------------------------------------------------------------- #


@pytest.mark.integration
async def test_a_price_question_is_sent_to_tier_one(
    ingested,
) -> None:  # type: ignore[no-untyped-def]
    """§9: a nearest-neighbour search over documents will confidently return
    last month's price."""
    _, retriever = ingested
    registry = ToolRegistry()
    registry.register(SearchKnowledge(retriever))

    for query in ("डीएपी का दाम क्या है", "what is the price of urea", "क्या स्टॉक में है"):
        result = await registry.execute(
            "search_knowledge", {"query": query}, ToolContext(call_id="c1")
        )
        assert result.ok
        assert result.data["answered"] is False
        assert result.data["use_instead"] == "check_availability"


@pytest.mark.integration
async def test_a_dosage_question_is_flagged_for_the_advisory_tool(
    ingested,
) -> None:  # type: ignore[no-untyped-def]
    """§23-3: the agent never generates a dose, and a document is not an
    approved recommendation."""
    _, retriever = ingested
    registry = ToolRegistry()
    registry.register(SearchKnowledge(retriever))

    result = await registry.execute(
        "search_knowledge",
        {"query": "गेहूँ में कितनी मात्रा डालनी है"},
        ToolContext(call_id="c1"),
    )
    assert result.ok
    assert result.data.get("dose_must_come_from_tool") is True
    assert result.data.get("use_instead") == "recommend_for_crop"


@pytest.mark.integration
async def test_every_answer_carries_a_citation(
    ingested,
) -> None:  # type: ignore[no-untyped-def]
    """§9 makes an uncitable claim a test failure."""
    _, retriever = ingested
    registry = ToolRegistry()
    registry.register(SearchKnowledge(retriever))

    result = await registry.execute(
        "search_knowledge", {"query": "सॉइल टेस्टिंग सेवा"}, ToolContext(call_id="c1")
    )
    assert result.ok
    if result.data["answered"] and result.data["tier"] == 2:
        assert result.data["chunk_ids"]
        assert all(r["title"] for r in result.data["results"])
        assert OPEN_DELIMITER in result.data["context"]


@pytest.mark.integration
async def test_an_irrelevant_question_is_not_presented_as_answered(
    ingested,
) -> None:  # type: ignore[no-untyped-def]
    """§11.4 and §1 N1.

    A vector index always returns its nearest neighbours, however far away they
    are, so "nothing relevant exists" and "here are four passages" are
    indistinguishable from the outside. No score separates them on this corpus
    -- see ``retrieval._is_weak`` for the numbers, including the cross-encoder
    §9 specifies for exactly this -- so the uncertainty is passed to the model
    with an explicit instruction instead of being silently resolved.

    What must never happen is the tool asserting a confident answer it cannot
    support. Either it returns nothing, or it says the match is weak.
    """
    _, retriever = ingested
    registry = ToolRegistry()
    registry.register(SearchKnowledge(retriever))

    result = await registry.execute(
        "search_knowledge",
        {"query": "quantum chromodynamics lattice gauge"},
        ToolContext(call_id="c1"),
    )
    assert result.ok
    if result.data["answered"]:
        assert result.data.get("weak_match") is True
        assert result.data["fallback_step"] == "offer_escalation"
        assert "do not know" in result.data["instruction"]
    else:
        assert result.data["next_step"] == "offer_escalation"


@pytest.mark.integration
async def test_the_answer_cache_is_tried_before_retrieval(
    ingested, app_engine
) -> None:  # type: ignore[no-untyped-def]
    """§9 Tier 3: a hit skips the LLM and, with pre-synthesised audio, the TTS."""
    _, retriever = ingested
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','voice_agent',true)"))
        row = (
            await session.execute(
                text(
                    "SELECT intent_key, question_variants[1] FROM answer_cache"
                    " WHERE is_active AND array_length(question_variants,1) > 0 LIMIT 1"
                )
            )
        ).first()
    assert row is not None, "the seed should carry answer-cache rows"
    intent_key, variant = row

    registry = ToolRegistry()
    registry.register(SearchKnowledge(retriever))
    result = await registry.execute(
        "search_knowledge", {"query": variant}, ToolContext(call_id="c1")
    )
    assert result.ok
    assert result.data["tier"] == 3
    assert result.data["intent_key"] == intent_key
    assert result.data["answer"]


def test_a_volatile_answer_can_never_be_cached() -> None:
    """§9: anything containing a price or stock figure goes to Tier 1 every
    time. Enforced by the schema so an operator cannot cache one by accident."""
    from uaagro_db.models import AnswerCache

    constraints = {c.name for c in AnswerCache.__table__.constraints}
    assert any("volatile" in (name or "") for name in constraints)


def test_the_chunk_dataclass_is_hashable_for_dedup() -> None:
    a = Chunk(index=0, content="x", section_path="p", language="en")
    b = Chunk(index=0, content="x", section_path="p", language="en")
    assert a.content_hash == b.content_hash
