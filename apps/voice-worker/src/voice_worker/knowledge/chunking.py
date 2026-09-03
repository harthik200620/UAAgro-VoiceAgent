"""Semantic chunking for Tier-2 retrieval (§9).

§9 asks for 300-500 token chunks with 15% overlap that never split mid-table and
never split mid-dosage-row. The last two are the whole point. A dosage row cut in
half is worse than no chunk at all: retrieval returns "Imidacloprid 17.8% SL,
100 ml" with the per-acre basis and the pre-harvest interval left in the
neighbouring chunk, and every safety qualifier that made the number usable is
gone. §16.2 treats a dose without its precaution as unservable, so a chunker that
can produce one is a safety bug, not a formatting preference.

Splitting therefore happens at structural boundaries -- headings first, then
paragraphs, then sentences -- and tables are atomic: a table larger than the
window is emitted whole and oversized rather than cut.

Token counts are estimated, not tokenised. The retrieval budget is a soft
target, the embedding model truncates at 512 anyway, and pulling in a tokeniser
to be precise about a soft target would make chunking depend on the model
being downloaded.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from ..text.script import whole_word

#: Bumped whenever a change here would produce different chunks for the same
#: input -- boundary rules, the window, overlap, tagging, language detection.
#:
#: Ingestion skips a document whose text has not changed (§9), which is right
#: for the common case and wrong for this one: improving the chunker would
#: leave every existing corpus on the old boundaries forever, with no error and
#: no way to notice from the outside. The version participates in the document
#: hash, so a change here re-chunks on the next ingest.
CHUNKER_VERSION = 3

#: §9. The window the retriever aims for.
MIN_TOKENS = 300
MAX_TOKENS = 500
OVERLAP_RATIO = 0.15

#: Devanagari runs longer per token than Latin script under a multilingual
#: sentencepiece vocabulary, so one ratio for both would systematically
#: over-fill Hindi chunks and truncate them at the model.
CHARS_PER_TOKEN_LATIN = 4.0
CHARS_PER_TOKEN_DEVANAGARI = 2.6

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
#: A line carrying a quantity and a unit: a dosage row in prose form. Kept with
#: its neighbours so the dose, basis and precaution stay in one chunk.
#:
#: Closed with :func:`whole_word` rather than ``\b``. A unit ending in a matra
#: -- ``बोरी``, ``बोतल`` -- never matches a ``\b``, because ``\b`` is defined
#: against ``\w`` and ``\w`` excludes combining marks. Written the obvious way,
#: ``ग्राम`` registers as a dose and ``बोरी`` does not, which is the worst kind
#: of half-working: the failure is silent and looks like the document simply
#: had no dosage in it.
_DOSE_LINE = re.compile(
    r"\d+(?:\.\d+)?\s*" + whole_word("ml|ग्राम|gram|g|kg|किलो|लीटर|litre|liter|l|बोरी|बोतल"),
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[।.!?])\s+")


def estimate_tokens(text: str) -> int:
    """Approximate token count, script-aware."""
    if not text:
        return 0
    devanagari = len(_DEVANAGARI.findall(text))
    other = len(text) - devanagari
    return int(devanagari / CHARS_PER_TOKEN_DEVANAGARI + other / CHARS_PER_TOKEN_LATIN) + 1


def carries_dose(text: str) -> bool:
    """True when the text states a quantity with a unit.

    Used by retrieval to flag a chunk as dosage-bearing. §23-3 forbids the
    agent generating a dose, so a chunk that looks like one must be routed to
    ``recommend_for_crop`` rather than read out of a document.
    """
    return bool(_DOSE_LINE.search(text))


def detect_language(text: str) -> str:
    """``hi`` when the text is substantially Devanagari, else ``en``.

    Crude on purpose: the only decision it feeds is which BM25 configuration
    and which half of the §9 dual-language retrieval a chunk belongs to, and
    both tolerate a wrong answer on a mixed passage far better than they
    tolerate a dependency that has to be downloaded.
    """
    if not text:
        return "en"
    devanagari = len(_DEVANAGARI.findall(text))
    return "hi" if devanagari / max(len(text), 1) > 0.15 else "en"


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage, with the metadata §9 requires on each."""

    index: int
    content: str
    section_path: str
    language: str
    crop_tags: tuple[str, ...] = ()
    product_tags: tuple[str, ...] = ()
    #: True when the chunk carries a dose. Surfaced so retrieval can refuse to
    #: answer a dosage question from prose -- §23-3 says the agent never
    #: generates a dose, and Tier 1 is the only place one may come from.
    contains_dose: bool = False

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def embedding_text(self) -> str:
        """What is embedded and indexed: the heading, then the passage.

        A chunk's section path is some of the most discriminating text it has --
        "CONVERSATION SNIPPETS -- THE TARGET REGISTER" says more about what a
        passage answers than most of its sentences do -- and it is exactly the
        text a bare chunk loses. Measured over the seed corpus, three of five
        remaining gate misses were queries whose answer sat under a heading that
        named the topic while the chunk body only demonstrated it.

        The raw ``content`` is what the agent is shown and cites; this is only
        what retrieval matches against.
        """
        if not self.section_path:
            return self.content
        return f"{self.section_path}\n\n{self.content}"

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)


@dataclass(slots=True)
class _Block:
    """A structural unit that is never split internally."""

    text: str
    kind: str  # paragraph | table | list | heading | dose
    section_path: str
    splittable: bool = True

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def parse_blocks(markdown: str) -> list[_Block]:
    """Split a document into blocks that must not be broken internally."""
    blocks: list[_Block] = []
    path: list[str] = []
    buffer: list[str] = []
    kind = "paragraph"

    def flush() -> None:
        nonlocal buffer, kind
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(
                _Block(
                    text=text,
                    kind=kind,
                    section_path=" > ".join(path),
                    # Tables and dosage runs are atomic. Everything else may be
                    # split at a sentence boundary if it is too long.
                    splittable=kind not in ("table", "dose"),
                )
            )
        buffer = []
        kind = "paragraph"

    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            del path[level - 1 :]
            path.append(title)
            continue

        is_table = bool(_TABLE_ROW.match(line))
        is_dose = bool(_DOSE_LINE.search(line)) and bool(_LIST_ITEM.match(line))
        line_kind = "table" if is_table else "dose" if is_dose else "paragraph"

        if not line.strip():
            # A blank line ends a paragraph but not a table: a table separated
            # by a blank line is still one table to a reader, and cutting there
            # would put the header in one chunk and the numbers in another.
            if kind != "table":
                flush()
            continue

        if line_kind != kind and buffer:
            # A dose line following prose belongs with the prose that
            # introduced it -- "spray at flowering:" then the quantity.
            if not (kind == "paragraph" and line_kind == "dose"):
                flush()
            kind = line_kind
        elif not buffer:
            kind = line_kind

        buffer.append(line)

    flush()
    return blocks


def _split_long(block: _Block) -> Iterator[str]:
    """Break an oversized splittable block at sentence boundaries.

    An oversized *unsplittable* block -- a long table -- is yielded whole. It
    will exceed the window and be truncated by the embedding model, which is
    the lesser harm: half a dosage table retrieved confidently is worse than a
    whole one retrieved imperfectly.
    """
    if not block.splittable or block.tokens <= MAX_TOKENS:
        yield block.text
        return

    sentences = _SENTENCE_END.split(block.text)
    current: list[str] = []
    size = 0
    for sentence in sentences:
        cost = estimate_tokens(sentence)
        if current and size + cost > MAX_TOKENS:
            yield " ".join(current)
            current, size = [], 0
        current.append(sentence)
        size += cost
    if current:
        yield " ".join(current)


def chunk_markdown(
    markdown: str,
    *,
    crop_vocabulary: Sequence[str] = (),
    product_vocabulary: Sequence[str] = (),
) -> list[Chunk]:
    """Chunk a markdown document per §9.

    Args:
        markdown: The document.
        crop_vocabulary: Crop names to tag chunks with, for filtered retrieval.
        product_vocabulary: Product names, same purpose.

    Returns:
        Chunks in document order, each 300-500 estimated tokens where the
        structure allows, with 15% overlap carried from the previous chunk.
    """
    blocks = parse_blocks(markdown)
    pieces: list[tuple[str, str]] = []
    for block in blocks:
        for text in _split_long(block):
            pieces.append((text, block.section_path))

    chunks: list[Chunk] = []
    current: list[str] = []
    current_path = ""
    size = 0
    overlap_tail = ""

    def emit() -> None:
        nonlocal current, size, overlap_tail
        if not current:
            return
        body = "\n\n".join(current).strip()
        if not body:
            current, size = [], 0
            return
        content = f"{overlap_tail}\n\n{body}".strip() if overlap_tail else body
        chunks.append(
            _build(content, len(chunks), current_path, crop_vocabulary, product_vocabulary)
        )
        # §9: 15% overlap, so a sentence answering the question is not orphaned
        # at a boundary where neither neighbour retrieves well.
        overlap_tail = _tail(body, int(estimate_tokens(body) * OVERLAP_RATIO))
        current, size = [], 0

    for text, path in pieces:
        cost = estimate_tokens(text)
        if current and size + cost > MAX_TOKENS:
            emit()
        elif current and path != current_path:
            # A section boundary ends a chunk -- unless what is accumulated is
            # still under the minimum and the next section is a sibling. §9
            # wants 300-500 tokens, and a corpus of 100-token chunks retrieves
            # badly: each carries too little context to answer from, and the
            # index fills with near-duplicates of the same heading.
            #
            # Merging siblings keeps the citation honest, because the recorded
            # path becomes their common parent rather than one of the two.
            # Merging across unrelated sections would not, so that still cuts.
            shared = _common_path(current_path, path)
            if size >= MIN_TOKENS or not shared:
                emit()
                current_path = path
            else:
                # Widen to the common parent and keep it. Falling through to
                # `current_path = path` here would label a chunk spanning a
                # dozen subsections with whichever one happened to come last --
                # content intact, citation wrong, and §9's citation is the whole
                # point of recording a section at all.
                current_path = shared
        else:
            current_path = path
        current.append(text)
        size += cost
        if size >= MAX_TOKENS:
            emit()

    emit()
    return chunks


#: Shallowest path depth a merge may fall back to. Depth 1 is the document's
#: own title, which every section in a single-file corpus shares -- merging on
#: that would put the whole document in one chunk and cite it as "the document",
#: which is not a citation.
MIN_MERGE_DEPTH = 2


def _common_path(left: str, right: str) -> str:
    """The deepest *real* section both paths sit under, or "" if there is none.

    Two subsections of "3. PRODUCT CATALOGUE" share that parent and may be
    merged under it. A subsection of "3. PRODUCT CATALOGUE" and one of
    "5. CROP ADVISORY" share only the document title, and a chunk spanning both
    would cite a section that describes neither -- so that returns "" and the
    chunk is cut instead.
    """
    left_parts = left.split(" > ")
    right_parts = right.split(" > ")
    shared: list[str] = []
    for a, b in zip(left_parts, right_parts, strict=False):
        if a != b:
            break
        shared.append(a)
    if len(shared) < MIN_MERGE_DEPTH:
        return ""
    return " > ".join(shared)


def _tail(text: str, tokens: int) -> str:
    """The last ``tokens`` worth of text, cut at a sentence boundary."""
    if tokens <= 0:
        return ""
    sentences = _SENTENCE_END.split(text)
    out: list[str] = []
    size = 0
    for sentence in reversed(sentences):
        out.insert(0, sentence)
        size += estimate_tokens(sentence)
        if size >= tokens:
            break
    return " ".join(out).strip()


def _build(
    content: str,
    index: int,
    section_path: str,
    crops: Sequence[str],
    products: Sequence[str],
) -> Chunk:
    lowered = content.lower()
    return Chunk(
        index=index,
        content=content,
        section_path=section_path,
        language=detect_language(content),
        crop_tags=tuple(sorted({c for c in crops if c.lower() in lowered})),
        product_tags=tuple(sorted({p for p in products if p.lower() in lowered})),
        contains_dose=carries_dose(content),
    )


__all__ = (
    "CHUNKER_VERSION",
    "MAX_TOKENS",
    "MIN_TOKENS",
    "OVERLAP_RATIO",
    "Chunk",
    "carries_dose",
    "chunk_markdown",
    "detect_language",
    "estimate_tokens",
    "parse_blocks",
)
