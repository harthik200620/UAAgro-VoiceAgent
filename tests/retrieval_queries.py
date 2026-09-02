"""The twenty retrieval queries that gate Phase 3 (§21).

Each names the section of ``UA_AGRO_KNOWLEDGE_BASE.md`` that answers it. The
assertion is that the section appears in the retrieved top-4 -- not that a
particular chunk id does, because chunk boundaries move when the document is
edited and a test that broke on a reflowed paragraph would be measuring the
chunker rather than retrieval.

Half the queries are in Hindi against sections written in English. That is the
§9 requirement that makes this suite worth running: Hindi agronomy prose is
sparse, so a Hindi question has to reach an English document or the corpus is
unusable to most callers. A monolingual retriever passes the English half and
fails here, which is exactly the signal wanted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    query: str
    #: Substring that must appear in the section path of at least one result.
    expect_section: str
    language: str = "hi-IN"
    #: Why this one is in the set, when that is not obvious.
    note: str = ""
    #: Set when this query does not currently retrieve its section, with the
    #: reason. Recorded rather than deleted: a query removed for failing is a
    #: gate that measures whatever already passes. The aggregate count is
    #: asserted separately, so any regression below today's number fails even
    #: though the individual case is expected to miss.
    known_miss: str = ""
    #: Set when this query lands on the top-4 boundary and flips between runs.
    #: Distinct from a known miss: it usually hits. HNSW builds its graph with
    #: randomised level assignment, so a database rebuilt from scratch -- which
    #: every test session does -- ranks a marginal candidate either side of the
    #: cut. Not counted in the guaranteed floor, and not asserted either way.
    borderline: str = ""


CASES: tuple[RetrievalCase, ...] = (
    # -- company and services ------------------------------------------- #
    RetrievalCase(
        "आपकी कंपनी क्या करती है",
        "COMPANY IDENTITY",
        note="Hindi question, English section: the core cross-lingual case.",
        known_miss="Too broad. Every chunk of a company knowledge base is about "
        "the company, so 'what does your company do' has no discriminating term "
        "and the ranking is close to arbitrary.",
    ),
    RetrievalCase("what services do you offer", "COMPANY IDENTITY", language="en-IN"),
    RetrievalCase(
        "ड्रोन से छिड़काव की सेवा है क्या",
        "COMPANY IDENTITY",
        known_miss="The services list names drone spraying in one line; the "
        "chunk that wins instead is the crop-recommendation table, which "
        "discusses spraying at length.",
    ),
    RetrievalCase("soil testing service", "COMPANY IDENTITY", language="en-IN"),
    # -- catalogue and composition -------------------------------------- #
    RetrievalCase(
        "डीएपी में क्या क्या होता है",
        "PRODUCT CATALOGUE",
        note="Composition, not price -- price must never come from retrieval.",
    ),
    RetrievalCase(
        "how should the agent describe a fertiliser",
        "PRODUCT CATALOGUE",
        language="en-IN",
        known_miss="Section 3.3 is marked [VERIFY] and is two lines long. The "
        "question is about how to speak, and half the document is about how to "
        "speak.",
    ),
    RetrievalCase("माल खत्म हो गया तो क्या कहें", "PRODUCT CATALOGUE"),
    RetrievalCase("what to say when a product is out of stock", "PRODUCT CATALOGUE",
                  language="en-IN"),
    # -- crop calendar --------------------------------------------------- #
    RetrievalCase(
        "गेहूँ की बुवाई कब होती है",
        "CROP CALENDAR",
        borderline="Measured at rank four -- the cut itself. Hits on most runs "
        "and misses on some, with no code change between them.",
    ),
    RetrievalCase("rabi sowing window", "CROP CALENDAR", language="en-IN"),
    RetrievalCase("कौन से महीने में सबसे ज़्यादा कॉल आती हैं", "CROP CALENDAR"),
    # -- problems and advisory ------------------------------------------ #
    RetrievalCase(
        "पत्ती पीली पड़ रही है",
        "COMMON PROBLEM VOCABULARY",
        note="A symptom with several causes. Retrieval finds the vocabulary; "
        "the diagnosis is a tool's job, not a document's.",
    ),
    RetrievalCase("yellowing leaves diagnosis", "COMMON PROBLEM VOCABULARY", language="en-IN"),
    RetrievalCase("तना छेदक कीड़ा", "COMMON PROBLEM VOCABULARY"),
    # -- units ----------------------------------------------------------- #
    RetrievalCase(
        "एक बीघा में कितने एकड़ होते हैं",
        "UNIT CONVERSION",
        note="The bigha varies by district; the document says so and the agent "
        "must confirm rather than assume.",
        borderline="Measured at rank four or just outside it. Observed hitting on "
        "two runs in three of identical code over an identical corpus.",
    ),
    RetrievalCase("bigha to acre conversion", "UNIT CONVERSION", language="en-IN"),
    # -- FAQs and register ----------------------------------------------- #
    RetrievalCase("क्या आप उधार पर सामान देते हैं", "FREQUENTLY ASKED QUESTIONS"),
    RetrievalCase(
        "do you deliver to the village",
        "FREQUENTLY ASKED QUESTIONS",
        language="en-IN",
        known_miss="The FAQ answer is one Hindi row in a 27-row table; the "
        "centre directory, which is entirely about village-level delivery, "
        "outranks it.",
    ),
    RetrievalCase(
        "अगर जवाब नहीं पता तो क्या कहना है",
        "CONVERSATION SNIPPETS",
        note="§11.4's most important exemplar: how to not know something.",
    ),
    RetrievalCase(
        "how to handle an interruption politely",
        "CONVERSATION SNIPPETS",
        language="en-IN",
        known_miss="The subsection is two lines of dialogue that demonstrate "
        "the behaviour without naming it -- no sentence in it contains "
        "'interruption' or 'politely' outside the heading.",
    ),
)

assert len(CASES) == 20, "§21 Phase 3 gates on twenty seeded queries"


#: Queries that do not currently retrieve their section. Five of twenty.
#:
#: The pattern across all five is the same: a broad or paraphrastic question
#: whose answer the corpus *demonstrates* rather than *states*. §9 calls this
#: document "a seed and a schema, not a finished corpus" -- eighteen chunks that
#: mostly describe how the agent should answer questions, so a question about
#: how to answer questions is close to every chunk at once.
#:
#: Four independently correct retrieval fixes landed while this number stayed at
#: fifteen -- the AND/OR query bug, the index/query configuration mismatch, the
#: missing section headings, the wrong section paths on merged chunks. Each was
#: a real defect and each shuffled *which* five missed without changing how
#: many. That is the signature of a corpus ceiling, not a retrieval bug, and it
#: is the reason this stopped at fifteen rather than being tuned to twenty
#: against a placeholder corpus.
KNOWN_MISSES: tuple[str, ...] = tuple(c.query for c in CASES if c.known_miss)

#: Queries that flip between runs. See ``RetrievalCase.borderline``.
BORDERLINE: tuple[str, ...] = tuple(c.query for c in CASES if c.borderline)

#: The floor the gate must not fall below.
#:
#: Excludes the borderline queries, so this is the *guaranteed* number rather
#: than the typical one: 14-15 of 20 hit on a given run, and 13 is what has held
#: on every run measured. Classified by measured rank rather than by whichever
#: query happened to fail most recently -- a case sitting at rank four is
#: unstable by construction, whether or not it failed today.
MINIMUM_HITS = len(CASES) - len(KNOWN_MISSES) - len(BORDERLINE)
