"""Numbers, in both directions (§5.3, §5.5).

Two jobs that look similar and are not:

**Parsing** turns what a farmer said into a value. ``दो सौ पाँच``, ``two sau
paanch``, ``२०५`` and ``205`` are all 205. §5.5 requires every quantity and
phone number to pass through here before it reaches a tool call -- an off-by-one
here is a wrong quantity of agrochemical, not a formatting bug.

**Rendering** turns a value into speakable Hindi. ``1250`` becomes
``बारह सौ पचास``, not ``एक हज़ार दो सौ पचास``: Indian speakers count prices in
hundreds well past a thousand, and the formal form sounds like a news bulletin
rather than a shopkeeper (§5.3, §11.3).

Hindi has an irregular name for every number to a hundred -- there is no rule to
derive ``उनहत्तर`` from 69 -- so the table below is the whole of the hard part
and is written out in full rather than generated.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- #
# Hindi cardinals
# --------------------------------------------------------------------------- #

#: 0-99. Irregular throughout; no generation rule exists.
HINDI_ONES: tuple[str, ...] = (
    "शून्य",
    "एक",
    "दो",
    "तीन",
    "चार",
    "पाँच",
    "छह",
    "सात",
    "आठ",
    "नौ",
    "दस",
    "ग्यारह",
    "बारह",
    "तेरह",
    "चौदह",
    "पंद्रह",
    "सोलह",
    "सत्रह",
    "अठारह",
    "उन्नीस",
    "बीस",
    "इक्कीस",
    "बाईस",
    "तेईस",
    "चौबीस",
    "पच्चीस",
    "छब्बीस",
    "सत्ताईस",
    "अट्ठाईस",
    "उनतीस",
    "तीस",
    "इकतीस",
    "बत्तीस",
    "तैंतीस",
    "चौंतीस",
    "पैंतीस",
    "छत्तीस",
    "सैंतीस",
    "अड़तीस",
    "उनतालीस",
    "चालीस",
    "इकतालीस",
    "बयालीस",
    "तैंतालीस",
    "चवालीस",
    "पैंतालीस",
    "छियालीस",
    "सैंतालीस",
    "अड़तालीस",
    "उनचास",
    "पचास",
    "इक्यावन",
    "बावन",
    "तिरेपन",
    "चौवन",
    "पचपन",
    "छप्पन",
    "सत्तावन",
    "अट्ठावन",
    "उनसठ",
    "साठ",
    "इकसठ",
    "बासठ",
    "तिरेसठ",
    "चौंसठ",
    "पैंसठ",
    "छियासठ",
    "सड़सठ",
    "अड़सठ",
    "उनहत्तर",
    "सत्तर",
    "इकहत्तर",
    "बहत्तर",
    "तिहत्तर",
    "चौहत्तर",
    "पचहत्तर",
    "छिहत्तर",
    "सतहत्तर",
    "अठहत्तर",
    "उन्यासी",
    "अस्सी",
    "इक्यासी",
    "बयासी",
    "तिरासी",
    "चौरासी",
    "पचासी",
    "छियासी",
    "सत्तासी",
    "अट्ठासी",
    "नवासी",
    "नब्बे",
    "इक्यानवे",
    "बानवे",
    "तिरानवे",
    "चौरानवे",
    "पचानवे",
    "छियानवे",
    "सत्तानवे",
    "अट्ठानवे",
    "निन्यानवे",
)

HUNDRED = "सौ"
THOUSAND = "हज़ार"
LAKH = "लाख"
CRORE = "करोड़"

#: Common fractional quantity words. A farmer says "डेढ़ बीघा", never
#: "एक दशमलव पाँच बीघा", and missing these mis-reads a plot size by 50%.
HINDI_FRACTIONS: dict[str, Decimal] = {
    "आधा": Decimal("0.5"),
    "आधी": Decimal("0.5"),
    "पौन": Decimal("0.75"),
    "पौना": Decimal("0.75"),
    "सवा": Decimal("1.25"),
    "डेढ़": Decimal("1.5"),
    "डेढ": Decimal("1.5"),
    "ढाई": Decimal("2.5"),
    "अढ़ाई": Decimal("2.5"),
    "साढ़े": Decimal("0.5"),  # a modifier: साढ़े तीन == 3.5
    "साढ़े तीन": Decimal("3.5"),
}

#: ``साढ़े`` and ``सवा``/``पौने`` modify the number that follows rather than
#: standing alone, so they are handled separately from the table above.
_MULTIPLIER_PREFIXES: dict[str, Decimal] = {
    "साढ़े": Decimal("0.5"),  # साढ़े चार  -> 4.5
    "सवा": Decimal("0.25"),  # सवा चार   -> 4.25
    "पौने": Decimal("-0.25"),  # पौने चार  -> 3.75
}

#: Devanagari digits to ASCII.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# --------------------------------------------------------------------------- #
# Spoken-word lookup, built once
# --------------------------------------------------------------------------- #


def _romanised(word: str) -> tuple[str, ...]:
    """Latin spellings a code-mixing speaker or ASR might produce."""
    return _ROMAN_VARIANTS.get(word, ())


#: Latin spellings for the numbers that actually come up in a price or a
#: quantity. Not exhaustive by design -- the fuzzy matcher in ``lexicon.py``
#: catches the tail, and a wrong guess on a number is worse than no guess.
_ROMAN_VARIANTS: dict[str, tuple[str, ...]] = {
    "शून्य": ("shunya", "zero"),
    "एक": ("ek", "ik", "one"),
    "दो": ("do", "two"),
    "तीन": ("teen", "tin", "three"),
    "चार": ("char", "chaar", "four"),
    "पाँच": ("panch", "paanch", "five"),
    "छह": ("chah", "chhah", "chhe", "six"),
    "सात": ("saat", "sat", "seven"),
    "आठ": ("aath", "ath", "eight"),
    "नौ": ("nau", "no", "nine"),
    "दस": ("das", "ten"),
    "ग्यारह": ("gyarah", "eleven"),
    "बारह": ("barah", "baarah", "twelve"),
    "तेरह": ("terah", "thirteen"),
    "चौदह": ("chaudah", "fourteen"),
    "पंद्रह": ("pandrah", "fifteen"),
    "सोलह": ("solah", "sixteen"),
    "सत्रह": ("satrah", "seventeen"),
    "अठारह": ("atharah", "eighteen"),
    "उन्नीस": ("unnis", "nineteen"),
    "बीस": ("bees", "bis", "twenty"),
    "पच्चीस": ("pachchis", "pachees"),
    "तीस": ("tees", "thirty"),
    "चालीस": ("chalis", "chaalis", "forty"),
    "पचास": ("pachas", "pachaas", "fifty"),
    "साठ": ("sath", "saath", "sixty"),
    "सत्तर": ("sattar", "seventy"),
    "अस्सी": ("assi", "eighty"),
    "नब्बे": ("nabbe", "ninety"),
}

_SCALE_WORDS: dict[str, int] = {
    HUNDRED: 100,
    "सौ": 100,
    "hundred": 100,
    "sau": 100,
    THOUSAND: 1_000,
    "हजार": 1_000,
    "hazar": 1_000,
    "hajar": 1_000,
    "thousand": 1_000,
    LAKH: 100_000,
    "lakh": 100_000,
    "lac": 100_000,
    CRORE: 10_000_000,
    "crore": 10_000_000,
    "karod": 10_000_000,
}


def _build_word_values() -> dict[str, int]:
    table: dict[str, int] = {}
    for value, word in enumerate(HINDI_ONES):
        table[word] = value
        for variant in _romanised(word):
            table.setdefault(variant, value)
    # English tens and teens that a code-mixing speaker uses directly.
    table.update(
        {
            "twentyone": 21,
            "twentytwo": 22,
            "twentyfive": 25,
            "hundred": 100,
        }
    )
    return table


_WORD_VALUES: dict[str, int] = _build_word_values()

_TOKEN_SPLIT = re.compile(r"[\s,\-]+")


# --------------------------------------------------------------------------- #
# Rendering: value -> speakable Hindi
# --------------------------------------------------------------------------- #


def to_hindi_words(value: int) -> str:
    """Render an integer in the Indian system: crore, lakh, thousand, hundred.

    Formal register. For money use :func:`to_hindi_price_words`, which produces
    the colloquial hundreds form a shopkeeper actually says.
    """
    if value < 0:
        return f"ऋण {to_hindi_words(-value)}"
    if value < 100:
        return HINDI_ONES[value]

    parts: list[str] = []
    for divisor, name in ((10_000_000, CRORE), (100_000, LAKH), (1_000, THOUSAND)):
        if value >= divisor:
            count, value = divmod(value, divisor)
            parts.append(f"{to_hindi_words(count)} {name}")
    if value >= 100:
        count, value = divmod(value, 100)
        parts.append(f"{HINDI_ONES[count]} {HUNDRED}")
    if value:
        parts.append(HINDI_ONES[value])
    return " ".join(parts)


def to_hindi_price_words(value: int) -> str:
    """Render money the way it is actually spoken.

    ``1250`` becomes ``बारह सौ पचास``, not ``एक हज़ार दो सौ पचास`` -- §5.3 gives
    exactly this example. Indian speakers count in hundreds to about ten
    thousand; past that the thousand/lakh form takes over and the hundreds form
    starts to sound absurd.
    """
    if value < 0:
        return f"ऋण {to_hindi_price_words(-value)}"
    if 1_000 <= value < 10_000:
        hundreds, remainder = divmod(value, 100)
        spoken = f"{to_hindi_words(hundreds)} {HUNDRED}"
        if remainder:
            spoken = f"{spoken} {HINDI_ONES[remainder]}"
        return spoken
    return to_hindi_words(value)


def rupees_to_words(amount: Decimal | int | float | str) -> str:
    """``1250`` -> ``बारह सौ पचास रुपये``. Paise are spoken only when non-zero."""
    value = _to_decimal(amount)
    whole = int(value)
    paise = int((value - whole) * 100)
    spoken = f"{to_hindi_price_words(whole)} रुपये"
    if paise:
        spoken = f"{spoken} {to_hindi_words(paise)} पैसे"
    return spoken


def digits_to_words(digits: str, *, paired: bool = False) -> str:
    """Speak a number one digit at a time -- for phone numbers and references.

    §11.3 requires reading numbers back for confirmation, and digit-by-digit is
    what makes the read-back verifiable. Indians commonly say a mobile in pairs
    ("अट्ठानवे, छिहत्तर, ..."), which is faster, but ``छिहत्तर`` (76) and
    ``छियासठ`` (66) differ by one syllable -- on an 8 kHz line beside a tractor
    that is a number the farmer will confirm as correct when it is not. Pairs
    are available via ``paired=True`` for reading a number the farmer already
    knows; capture confirmation uses the default.
    """
    cleaned = re.sub(r"\D", "", digits)
    if not cleaned:
        return ""
    if not paired:
        return " ".join(HINDI_ONES[int(d)] for d in cleaned)
    chunks = [cleaned[i : i + 2] for i in range(0, len(cleaned), 2)]
    return " ".join(
        to_hindi_words(int(chunk)) if len(chunk) == 2 else HINDI_ONES[int(chunk)]
        for chunk in chunks
    )


def npk_grade_to_words(grade: str) -> str:
    """``12-32-16`` -> ``बारह बत्तीस सोलह`` (§5.3).

    Each figure is a separate nutrient percentage, so they are spoken as three
    numbers rather than as one twelve-million figure.
    """
    parts = re.split(r"[-:\s]+", grade.strip())
    return " ".join(to_hindi_words(int(p)) for p in parts if p.isdigit())


# --------------------------------------------------------------------------- #
# Parsing: what the farmer said -> a value
# --------------------------------------------------------------------------- #


def normalise_digits(text: str) -> str:
    """Devanagari digits to ASCII, so ``२०५`` and ``205`` parse identically."""
    return text.translate(_DEVANAGARI_DIGITS)


def parse_number(text: str) -> Decimal | None:
    """Parse a spoken or written quantity.

    Handles digits, Devanagari digits, Hindi words, Latin transliterations,
    mixed forms like ``दो सौ पाँच``, and the fractional words (``डेढ़``,
    ``ढाई``, ``साढ़े तीन``) that carry real quantities in rural speech.

    Returns ``None`` rather than guessing when the input is not a number.
    §1 N1 makes a wrong number worse than no number -- the agent asks again.
    """
    if not text or not text.strip():
        return None

    cleaned = normalise_digits(text.strip().lower())

    # A bare numeric literal, with or without Indian digit grouping.
    bare = cleaned.replace(",", "")
    if re.fullmatch(r"\d+(\.\d+)?", bare):
        try:
            return Decimal(bare)
        except InvalidOperation:
            return None

    tokens = [t for t in _TOKEN_SPLIT.split(cleaned) if t]
    if not tokens:
        return None

    # Standalone fraction words: "आधा", "डेढ़", "ढाई".
    if len(tokens) == 1 and tokens[0] in HINDI_FRACTIONS:
        return HINDI_FRACTIONS[tokens[0]]

    # Modifier + number: "साढ़े तीन" -> 3.5, "सवा दो" -> 2.25, "पौने चार" -> 3.75.
    if len(tokens) == 2 and tokens[0] in _MULTIPLIER_PREFIXES:
        base = _accumulate(tokens[1:])
        if base is not None:
            return base + _MULTIPLIER_PREFIXES[tokens[0]]
        return None

    return _accumulate(tokens)


def _accumulate(tokens: list[str]) -> Decimal | None:
    """Fold number words into a value, honouring the Indian scale words.

    ``दो लाख पचास हज़ार`` folds as (2 x 100000) + (50 x 1000).
    """
    total = Decimal(0)
    current = Decimal(0)
    seen_any = False

    for token in tokens:
        if token in _SCALE_WORDS:
            scale = Decimal(_SCALE_WORDS[token])
            if scale >= 1_000:
                # Lakh and above close the running group.
                total += (current if current else Decimal(1)) * scale
                current = Decimal(0)
            else:
                current = (current if current else Decimal(1)) * scale
            seen_any = True
            continue

        if token in _WORD_VALUES:
            current += Decimal(_WORD_VALUES[token])
            seen_any = True
            continue

        if re.fullmatch(r"\d+(\.\d+)?", token):
            current += Decimal(token)
            seen_any = True
            continue

        # An unrecognised token means this is prose, not a number.
        return None

    return (total + current) if seen_any else None


def extract_numbers(text: str) -> list[Decimal]:
    """Every number in a sentence, in order.

    Used when a farmer packs two quantities into one breath -- "दस बोरी डीएपी
    और पाँच बोरी यूरिया".
    """
    found: list[Decimal] = []
    tokens = [t for t in _TOKEN_SPLIT.split(normalise_digits(text.lower())) if t]
    buffer: list[str] = []

    for token in tokens:
        if token in _WORD_VALUES or token in _SCALE_WORDS or re.fullmatch(r"\d+(\.\d+)?", token):
            buffer.append(token)
            continue
        if buffer:
            value = _accumulate(buffer)
            if value is not None:
                found.append(value)
            buffer = []

    if buffer:
        value = _accumulate(buffer)
        if value is not None:
            found.append(value)
    return found


def parse_phone_number(text: str) -> str | None:
    """Recover a ten-digit mobile from speech.

    A farmer reading a number aloud produces a mix of digits and words, often
    with the ASR splitting them oddly. Only an exact ten digits is accepted --
    §11.3 requires reading it back, and a nine-digit guess would waste that.
    """
    cleaned = normalise_digits(text.lower())
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]

    # Fall back to word-by-word, which is how a careful speaker dictates.
    spoken: list[str] = []
    for token in _TOKEN_SPLIT.split(cleaned):
        if not token:
            continue
        if token.isdigit():
            spoken.append(token)
        elif token in _WORD_VALUES and 0 <= _WORD_VALUES[token] <= 9:
            spoken.append(str(_WORD_VALUES[token]))
        else:
            return None
    joined = "".join(spoken)
    return joined if len(joined) == 10 else None


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)
