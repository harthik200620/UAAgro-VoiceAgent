"""Post-ASR product matching (§5.5).

The recogniser hears ``यूरीया`` where the catalogue says ``यूरिया``, or ``uria``
where it says ``urea``. §5.5 requires both to resolve to the same SKU.

The mechanism is the catalogue's own ``lexicon_variants`` column, which carries
every way a farmer might *say* a product -- KB §3.1 calls it the highest-leverage
column in the import file, and it is. Fuzzy matching is not asked to bridge
Devanagari to Latin by phonetics; it only has to absorb the small spelling drift
around each listed variant. A general cross-script phonetic matcher would be
guessing, and a wrong SKU is a wrong agrochemical.

Two guards make a wrong match less likely than no match:

* a **confidence floor**, below which nothing is returned;
* an **ambiguity check** -- when two different SKUs score within
  :data:`AMBIGUITY_MARGIN` of each other, the result is reported as ambiguous
  rather than resolved, so the agent asks instead of picking.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .script import strip_punctuation

#: Below this, a match is not returned at all.
DEFAULT_THRESHOLD = 0.82

#: Two candidates closer than this are treated as ambiguous. Tuned to catch
#: near-identical brand names, where picking the higher score by a hair would
#: dispense the wrong product.
AMBIGUITY_MARGIN = 0.05

#: Longest variant, in tokens, worth scanning for inside a sentence.
MAX_PHRASE_TOKENS = 4

#: Postpositions and particles that carry no product identity. Dropped before
#: scanning so "गेहूँ का बीज" reaches the "गेहूँ बीज" variant -- a farmer speaks
#: sentences, not catalogue phrases, and the postposition distinguishes nothing.
_PARTICLES: frozenset[str] = frozenset(
    {"का", "की", "के", "को", "में", "से", "पर", "वाला", "वाली", "वाले", "ka", "ki", "ke"}
)

#: Nukta-bearing Devanagari letters fold to their base form. ASR output is
#: inconsistent about the nukta, and ज़/ज or ड़/ड should never decide a match.
_NUKTA_FOLD = str.maketrans(
    {
        "क़": "क",  # क़ -> क
        "ख़": "ख",  # ख़ -> ख
        "ग़": "ग",  # ग़ -> ग
        "ज़": "ज",  # ज़ -> ज
        "ड़": "ड",  # ड़ -> ड
        "ढ़": "ढ",  # ढ़ -> ढ
        "फ़": "फ",  # फ़ -> फ
        "य़": "य",  # य़ -> य
    }
)

#: The combining nukta itself, when it survives decomposition.
_COMBINING_NUKTA = "़"

#: Vowel-length distinctions the recogniser routinely gets wrong: ि/ी, ु/ू.
_MATRA_FOLD = str.maketrans({"ी": "ि", "ू": "ु"})

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Fold the variation that should never decide a match.

    Composes to NFC first so a decomposed nukta and a precomposed one compare
    equal, then folds nukta and vowel length, lowercases, and strips
    punctuation.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFC", text).translate(_NUKTA_FOLD)
    folded = folded.replace(_COMBINING_NUKTA, "").translate(_MATRA_FOLD)
    folded = strip_punctuation(folded.lower())
    return _WHITESPACE.sub(" ", folded).strip()


def levenshtein(left: str, right: str) -> int:
    """Edit distance. Iterative two-row form: the catalogue is scanned per turn
    and the recursive version would allocate far too much on the audio path."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, lc in enumerate(left, start=1):
        current = [i]
        for j, rc in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (lc != rc),  # substitution
                )
            )
        previous = current
    return previous[-1]


def similarity(left: str, right: str) -> float:
    """Normalised edit similarity in ``[0, 1]``."""
    if not left and not right:
        return 1.0
    longest = max(len(left), len(right))
    if longest == 0:
        return 1.0
    return 1.0 - (levenshtein(left, right) / longest)


@dataclass(frozen=True, slots=True)
class Place:
    """A district the helpline serves, in both scripts, for STT and matching."""

    name_en: str
    name_hi: str


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    """One catalogue item and every spoken form that should reach it."""

    sku: str
    name_hi: str
    name_en: str
    variants: tuple[str, ...]
    #: The kind of product (``categories.slug``) and the crops it is for.
    #: Read by the direct-answer layer to narrow "आलू का बीज" to a product
    #: without a crop name having to *be* a product name.
    category: str = ""
    crops: tuple[str, ...] = ()

    def normalised_variants(self) -> tuple[str, ...]:
        forms = {normalise(self.name_hi), normalise(self.name_en)}
        forms.update(normalise(v) for v in self.variants)
        return tuple(sorted(f for f in forms if f))


@dataclass(frozen=True, slots=True)
class Match:
    """A resolved product mention."""

    sku: str
    score: float
    matched_text: str
    matched_variant: str
    #: True when a second SKU scored within :data:`AMBIGUITY_MARGIN`. The agent
    #: must ask rather than choose.
    ambiguous: bool = False
    runner_up_sku: str | None = None


@dataclass
class Lexicon:
    """The catalogue's spoken vocabulary, indexed for matching.

    Built once at worker start from ``products`` and held in memory: §7.5
    requires everything warm, and re-reading 500 rows per turn would spend the
    latency budget on a lookup that never changes mid-call.
    """

    entries: list[LexiconEntry] = field(default_factory=list)
    #: The districts, for keyterms and for "बाराबंकी वाला सेंटर कहाँ है".
    places: tuple[Place, ...] = ()
    _exact: dict[str, str] = field(default_factory=dict, repr=False)
    _by_length: dict[int, list[tuple[str, str]]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_entries(cls, entries: list[LexiconEntry], places: tuple[Place, ...] = ()) -> Lexicon:
        lexicon = cls(entries=entries, places=places)
        lexicon._build()
        return lexicon

    def _build(self) -> None:
        self._exact.clear()
        self._by_length.clear()
        for entry in self.entries:
            for variant in entry.normalised_variants():
                # First writer wins: an earlier SKU claiming a variant is a
                # catalogue problem, surfaced by `conflicts()` rather than
                # silently resolved differently on each rebuild.
                self._exact.setdefault(variant, entry.sku)
                self._by_length.setdefault(len(variant), []).append((variant, entry.sku))

    def conflicts(self) -> dict[str, list[str]]:
        """Variants claimed by more than one SKU.

        A genuine catalogue error: two products answering to the same spoken
        name means the agent cannot know which the farmer meant. Surfaced for
        the admin panel rather than resolved by guessing.
        """
        seen: dict[str, list[str]] = {}
        for entry in self.entries:
            for variant in entry.normalised_variants():
                seen.setdefault(variant, []).append(entry.sku)
        return {v: skus for v, skus in seen.items() if len(set(skus)) > 1}

    @property
    def variant_count(self) -> int:
        return sum(len(e.normalised_variants()) for e in self.entries)

    def keyterms(self) -> list[str]:
        """Every spoken form, for injection as STT keyterms (§5.5).

        Where the recogniser accepts keyterm boosting this is the cheaper half
        of the job -- correcting after the fact is always lossier than biasing
        the decode.
        """
        terms: set[str] = set()
        for entry in self.entries:
            terms.add(entry.name_hi)
            terms.add(entry.name_en)
            terms.update(entry.variants)
        for place in self.places:
            terms.add(place.name_en)
            terms.add(place.name_hi)
        return sorted(t for t in terms if t)

    # -- matching --------------------------------------------------------- #

    def match(self, text: str, *, threshold: float = DEFAULT_THRESHOLD) -> Match | None:
        """Resolve a single spoken product name.

        Returns ``None`` below the threshold. §1 N1 makes no answer better than
        a confident wrong one.
        """
        needle = normalise(text)
        if not needle:
            return None

        exact = self._exact.get(needle)
        if exact is not None:
            return Match(sku=exact, score=1.0, matched_text=text, matched_variant=needle)

        # Only compare against variants of a plausible length: an edit distance
        # large enough to bridge a big length gap cannot clear the threshold.
        span = max(1, int(len(needle) * (1 - threshold)) + 1)
        best: tuple[float, str, str] | None = None
        second: tuple[float, str, str] | None = None

        for length in range(len(needle) - span, len(needle) + span + 1):
            for variant, sku in self._by_length.get(length, ()):
                score = similarity(needle, variant)
                if score < threshold:
                    continue
                candidate = (score, sku, variant)
                if best is None or score > best[0]:
                    if best is not None and best[1] != sku:
                        second = best
                    best = candidate
                elif (second is None or score > second[0]) and sku != best[1]:
                    second = candidate

        if best is None:
            return None

        score, sku, variant = best
        ambiguous = second is not None and (score - second[0]) < AMBIGUITY_MARGIN
        return Match(
            sku=sku,
            score=score,
            matched_text=text,
            matched_variant=variant,
            ambiguous=ambiguous,
            runner_up_sku=second[1] if ambiguous and second else None,
        )

    def find_all(self, sentence: str, *, threshold: float = DEFAULT_THRESHOLD) -> list[Match]:
        """Find every product mentioned in an utterance.

        Scans n-grams longest-first so ``सिंगल सुपर फॉस्फेट`` matches as one
        product rather than as three unrelated tokens, and consumed tokens are
        not reconsidered.
        """
        tokens = [t for t in normalise(sentence).split() if t not in _PARTICLES]
        if not tokens:
            return []

        matches: list[Match] = []
        index = 0
        while index < len(tokens):
            claimed = 0
            for size in range(min(MAX_PHRASE_TOKENS, len(tokens) - index), 0, -1):
                phrase = " ".join(tokens[index : index + size])
                found = self.match(phrase, threshold=threshold)
                if found is not None:
                    matches.append(found)
                    claimed = size
                    break
            index += claimed or 1
        return matches
