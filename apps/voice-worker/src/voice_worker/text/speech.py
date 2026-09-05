"""``text_for_speech``: catalogue text to speakable Hindi (§5.3).

§5.3 is blunt about this: *never hand raw catalogue strings to TTS*. A product
row says ``NPK 12-32-16``, ``50 kg``, ``₹1,350``. Synthesised literally, a
farmer hears "en-pee-kay twelve dash thirty-two dash sixteen", which is not
Hindi and not a number they can act on.

Rule order is load-bearing and is the reason this is one ordered pipeline rather
than a set of independent substitutions:

1. **Currency first.** ``₹1,350`` must be consumed whole; otherwise the bare-number
   rule turns ``1,350`` into digits and the rupee symbol is orphaned.
2. **Compound grades next.** ``12-32-16`` is three nutrient percentages, not one
   number and not a subtraction.
3. **Units before bare numbers**, so ``50kg`` becomes "पचास किलो" rather than
   "पचास" followed by an unread "kg".
4. **Abbreviations before anything alphabetic**, longest first, so ``NPK`` does
   not partially match inside a longer token.
5. **Bare numbers last**, once everything with a unit or symbol attached has
   already been claimed.

The output is Devanagari with no Latin script, no digits and no symbols left.
:func:`assert_speakable` enforces that in tests.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .numbers import (
    digits_to_words,
    normalise_digits,
    npk_grade_to_words,
    rupees_to_words,
    to_hindi_words,
)
from .translit import transliterate

# --------------------------------------------------------------------------- #
# Domain vocabulary
# --------------------------------------------------------------------------- #

#: Spoken forms for the abbreviations that appear in an agri catalogue. Letters
#: are spelled out in Devanagari because Sarvam reads Latin characters as
#: English, which breaks the prosody mid-sentence.
ABBREVIATIONS: dict[str, str] = {
    "NPK": "एन पी के",
    "DAP": "डी ए पी",
    "SSP": "एस एस पी",
    "MOP": "एम ओ पी",
    "CAN": "सी ए एन",
    "PSB": "पी एस बी",
    "GA3": "जी ए थ्री",
    "PHI": "प्रतीक्षा अवधि",
    "CIB": "सी आई बी",
    "FPO": "एफ पी ओ",
    "KCC": "के सी सी",
    "EC": "ई सी",
    "SC": "एस सी",
    "WP": "डब्ल्यू पी",
    "WG": "डब्ल्यू जी",
    "SL": "एस एल",
    "SG": "एस जी",
    "WDG": "डब्ल्यू डी जी",
    "GST": "जी एस टी",
    "MRP": "एम आर पी",
    "ID": "आई डी",
    "OK": "ठीक",
}

#: Units, in the register a farmer uses. Keys are matched case-insensitively
#: and may be attached to a number (``50kg``) or separated (``50 kg``).
UNITS: dict[str, str] = {
    "kg": "किलो",
    "kgs": "किलो",
    "kilo": "किलो",
    "kilogram": "किलो",
    "g": "ग्राम",
    "gm": "ग्राम",
    "gms": "ग्राम",
    "gram": "ग्राम",
    "mg": "मिलीग्राम",
    "ml": "मिलीलीटर",
    "l": "लीटर",
    "ltr": "लीटर",
    "litre": "लीटर",
    "liter": "लीटर",
    "quintal": "कुंतल",
    "ton": "टन",
    "acre": "एकड़",
    "acres": "एकड़",
    "bigha": "बीघा",
    "katha": "कट्ठा",
    "hectare": "हेक्टेयर",
    "ha": "हेक्टेयर",
    "bag": "बोरी",
    "bags": "बोरी",
    "bori": "बोरी",
    "day": "दिन",
    "days": "दिन",
    "%": "प्रतिशत",
}

#: Ordered longest-first so ``kgs`` is not matched as ``kg`` plus a stray ``s``.
_UNIT_PATTERN = "|".join(
    re.escape(unit) for unit in sorted(UNITS, key=len, reverse=True) if unit != "%"
)

_CURRENCY = re.compile(r"(?:₹|\bRs\.?|\bINR)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_NPK_GRADE = re.compile(r"\b(\d{1,2})[-:](\d{1,2})[-:](\d{1,2})\b")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_NUMBER_UNIT = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})\b", re.IGNORECASE)
_PHONE = re.compile(r"\b(?:\+?91[\s-]?)?([6-9]\d{9})\b")
_DECIMAL = re.compile(r"\b(\d+)\.(\d+)\b")
_BARE_NUMBER = re.compile(r"\b\d[\d,]*\b")
_ABBREV = re.compile(r"\b(" + "|".join(sorted(ABBREVIATIONS, key=len, reverse=True)) + r")\b")
_MULTISPACE = re.compile(r"\s{2,}")

#: Sentence boundaries. Devanagari uses ``।`` (danda) as well as the Latin stop,
#: and §5.3 chunks on these so barge-in cancels at a natural point.
_SENTENCE_END = re.compile(r"(?<=[।?!.])\s+")
#: Clause boundaries, for the first release only: a comma or a dash followed
#: by more text. Kept on the released clause so the synthesiser pauses there.
_CLAUSE_END = re.compile("[,،;]\\s+|\\s+[\u2014\u2013-]\\s+")
#: How many words must precede a clause boundary before the opening clause
#: is released on its own. Shared by the agent and the pipeline so the two
#: buffers between the model and the synthesiser agree on the unit.
FIRST_CLAUSE_WORDS = 3

#: §5.3: one idea per sentence, under about fifteen words. Longer sentences are
#: unrecoverable when the line drops a packet.
MAX_SENTENCE_WORDS = 15


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


def text_for_speech(text: str, *, language: str = "hi-IN") -> str:
    """Normalise text so it can be synthesised directly.

    ``language`` selects the target vocabulary. Only Hindi is implemented; other
    languages return the input with digits and symbols expanded to Hindi words,
    which is wrong for them and is why :func:`supported_languages` exists and
    Phase 2's language work checks it.
    """
    if not text or not text.strip():
        return ""
    if language.split("-")[0].lower() not in {"hi", "bho", "awa"}:
        return _for_other_language(text, language)

    result = normalise_digits(text)

    result = _CURRENCY.sub(lambda m: " " + rupees_to_words(_clean_number(m.group(1))), result)
    result = _NPK_GRADE.sub(
        lambda m: " " + npk_grade_to_words(f"{m.group(1)}-{m.group(2)}-{m.group(3)}"), result
    )
    result = _PHONE.sub(lambda m: " " + digits_to_words(m.group(1)), result)
    result = _PERCENT.sub(lambda m: " " + _spoken_number(m.group(1)) + " प्रतिशत", result)
    result = _NUMBER_UNIT.sub(
        lambda m: f" {_spoken_number(m.group(1))} {UNITS[m.group(2).lower()]}", result
    )
    result = _ABBREV.sub(lambda m: " " + ABBREVIATIONS[m.group(1).upper()], result)
    result = _DECIMAL.sub(
        lambda m: f" {to_hindi_words(int(m.group(1)))} दशमलव {digits_to_words(m.group(2))}",
        result,
    )
    result = _BARE_NUMBER.sub(lambda m: " " + _spoken_number(m.group(0)), result)

    # A bare unit left without a number (a stray "kg" in a description).
    result = re.sub(
        rf"\b({_UNIT_PATTERN})\b",
        lambda m: UNITS[m.group(1).lower()],
        result,
        flags=re.IGNORECASE,
    )
    result = result.replace("%", " प्रतिशत ")
    # '+' joins two actives in a formulation (Cymoxanil 8% + Mancozeb 64%),
    # and reads naturally as 'and'.
    result = re.sub(r"\s*\+\s*", " और ", result)
    result = re.sub(r"\s*&\s*", " और ", result)
    # Anything still in Latin is an ingredient or brand with no Hindi name
    # on its catalogue row. Transliterating keeps the sentence in one
    # language; the real fix is a name_hi value, and the leftover is
    # reported by unspeakable_fragments before transliteration hides it.
    result = transliterate(result)

    return _MULTISPACE.sub(" ", result).strip()


#: What the currency sign, the percent sign and a joining "+" become in the
#: languages a caller may switch to. Digits are left as digits: every
#: synthesiser on the routing table reads them in its own language, and a
#: Hindi number-word inside a Tamil sentence is the wrong kind of wrong.
_RUPEE_WORD: dict[str, str] = {
    "en": "rupees",
    "mr": "रुपये",
    "bn": "টাকা",
    "ta": "ரூபாய்",
    "te": "రూపాయలు",
    "kn": "ರೂಪಾಯಿ",
    "ml": "രൂപ",
    "gu": "રૂપિયા",
    "pa": "ਰੁਪਏ",
}
_PERCENT_WORD: dict[str, str] = {
    "en": "percent",
    "mr": "टक्के",
    "bn": "শতাংশ",
    "ta": "சதவீதம்",
    "te": "శాతం",
    "kn": "ಶೇಕಡಾ",
    "ml": "ശതമാനം",
    "gu": "ટકા",
    "pa": "ਪ੍ਰਤੀਸ਼ਤ",
}
_AND_WORD: dict[str, str] = {
    "en": "and",
    "mr": "आणि",
    "bn": "এবং",
    "ta": "மற்றும்",
    "te": "మరియు",
    "kn": "ಮತ್ತು",
    "ml": "ഒപ്പം",
    "gu": "અને",
    "pa": "ਅਤੇ",
}


def _for_other_language(text: str, language: str) -> str:
    """The non-Hindi path: symbols become words, numbers stay numerals."""
    bare = language.split("-")[0].lower()
    rupee = _RUPEE_WORD.get(bare, "rupees")
    percent = _PERCENT_WORD.get(bare, "percent")
    joiner = _AND_WORD.get(bare, "and")
    result = normalise_digits(text)
    result = _CURRENCY.sub(lambda m: f" {_clean_number(m.group(1)).normalize():f} {rupee}", result)
    result = result.replace("%", f" {percent} ")
    result = re.sub(r"\s*\+\s*", f" {joiner} ", result)
    result = re.sub(r"\s*&\s*", f" {joiner} ", result)
    return _MULTISPACE.sub(" ", result).strip()


def _clean_number(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _spoken_number(raw: str) -> str:
    value = _clean_number(raw)
    whole = int(value)
    if value == whole:
        return to_hindi_words(whole)
    fractional = str(value).split(".")[1]
    return f"{to_hindi_words(whole)} दशमलव {digits_to_words(fractional)}"


# --------------------------------------------------------------------------- #
# Chunking for the TTS stream
# --------------------------------------------------------------------------- #


def split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries, Devanagari danda included.

    §5.3 streams TTS a sentence at a time so barge-in cancels at a natural
    point rather than mid-word.
    """
    parts = [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


class SentenceBuffer:
    """Accumulates streamed text and releases complete sentences.

    §5.2 streams the model's answer; §5.3 synthesises a sentence at a time. The
    join between them is this: text arrives in fragments that respect no
    boundary at all, and the synthesiser must be handed whole sentences.

    "Complete" means a terminator *followed by* something -- whitespace or more
    text. A danda arriving as the last character so far is not yet a sentence:
    the next fragment may be a decimal point's worth of context, and more
    importantly flushing on it would race the model to the punctuation and cut
    "बारह सौ पचास।" into two utterances if the token boundary fell mid-number.

    Whatever is left when the stream ends is released by :meth:`flush`, so an
    answer whose last sentence lacks punctuation is still spoken.
    """

    __slots__ = ("_buffer", "_first_clause_words", "_released")

    def __init__(self, *, first_clause_words: int = 0) -> None:
        self._buffer = ""
        #: When set, the *first* release of the stream may happen at a clause
        #: boundary -- a comma or a dash -- once this many words precede it.
        #: The opening clause is what the farmer waits for; a full sentence of
        #: Hindi is another 300-500 ms of tokens behind the comma. Later
        #: releases stay on sentence boundaries, where the prosody is right.
        self._first_clause_words = first_clause_words
        self._released = 0

    def add(self, fragment: str) -> list[str]:
        """Take a fragment; return whatever complete sentences it completed."""
        self._buffer += fragment
        released: list[str] = []

        while True:
            match = _SENTENCE_END.search(self._buffer)
            if match is None:
                break
            sentence = self._buffer[: match.start()].strip()
            self._buffer = self._buffer[match.end() :]
            if sentence:
                released.append(sentence)

        if not released and self._released == 0 and self._first_clause_words > 0:
            clause = self._first_clause()
            if clause:
                released.append(clause)

        self._released += len(released)
        return released

    def _first_clause(self) -> str | None:
        """The opening clause, if a boundary with enough words behind it exists."""
        for match in _CLAUSE_END.finditer(self._buffer):
            head = self._buffer[: match.end()].strip()
            if len(head.split()) < self._first_clause_words:
                # "जी हाँ," on its own is not worth a synthesiser round trip;
                # the next boundary along may be.
                continue
            self._buffer = self._buffer[match.end() :]
            return head
        return None

    def flush(self) -> list[str]:
        """Release the tail, terminated or not."""
        remaining = self._buffer.strip()
        self._buffer = ""
        return [remaining] if remaining else []

    @property
    def pending(self) -> str:
        return self._buffer


def sentence_is_too_long(sentence: str) -> bool:
    return len(sentence.split()) > MAX_SENTENCE_WORDS


def over_long_sentences(text: str) -> list[str]:
    """Sentences breaching the §5.3 length rule.

    Surfaced by the response validator rather than silently truncated: the fix
    is a shorter prompt, not a clipped answer.
    """
    return [s for s in split_sentences(text) if sentence_is_too_long(s)]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
_SYMBOL = re.compile(r"[₹%@#&*_/\\]")


def unspeakable_fragments(text: str) -> list[str]:
    """Anything left that the TTS would mispronounce.

    Latin letters, digits and symbols all survive a missing rule silently --
    the synthesiser produces *something*, just not Hindi. This is what turns
    that into a test failure.
    """
    problems: list[str] = []
    for token in text.split():
        if _LATIN.search(token) or _DIGIT.search(token) or _SYMBOL.search(token):
            problems.append(token)
    return problems


def assert_speakable(text: str) -> None:
    """Raise if normalisation left anything unspeakable. Used by the tests."""
    problems = unspeakable_fragments(text)
    if problems:
        raise AssertionError(
            f"text_for_speech left {len(problems)} unspeakable fragment(s): {problems}. "
            f"Add a rule rather than letting the synthesiser guess."
        )


def supported_languages() -> frozenset[str]:
    """Languages with a real normalisation vocabulary.

    Everything else falls through to the Hindi rules, which is wrong for them.
    Marathi and the other Indic paths need their own tables before their tier
    claim in §5.1 is honest; that is tracked as a Phase 2 gap rather than
    hidden behind a silent fallback.
    """
    return frozenset({"hi-IN"})
