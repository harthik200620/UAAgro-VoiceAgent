"""Latin to Devanagari transliteration -- a fallback, not the main path.

Active-ingredient names are Latin and there are hundreds of them:
``Imidacloprid``, ``Chlorantraniliprole``, ``Tebuconazole``. Left as Latin, the
synthesiser switches to English pronunciation mid-Hindi-sentence, and the
prosody break is audible and jarring on an 8 kHz line.

**The correct source is the catalogue.** ``products.name_hi`` exists for exactly
this reason and the tool layer speaks it. This module is what catches the
remainder -- an ingredient array, an untranslated description, a brand nobody
added a Hindi form for -- so the failure mode is an approximate Hindi
pronunciation rather than a language switch.

It approximates. English orthography is not phonetic and this makes no attempt
to be a general transliterator; it targets the phonotactics of agrochemical
names, which are mostly Greek and Latin roots with predictable syllables.
Whether the result is *good enough* is a listening question, and it is on the
Phase 2 bake-off checklist rather than assumed.
"""

from __future__ import annotations

import re

#: Independent vowel forms, used word-initially.
_VOWEL_INDEPENDENT: dict[str, str] = {
    "a": "अ",
    "aa": "आ",
    "i": "इ",
    "ee": "ई",
    "u": "उ",
    "oo": "ऊ",
    "e": "ए",
    "ai": "ऐ",
    "o": "ओ",
    "au": "औ",
}

#: Matra forms, used after a consonant. ``a`` is the implicit vowel and adds
#: nothing, which is why it maps to the empty string.
_VOWEL_MATRA: dict[str, str] = {
    "a": "",
    "aa": "ा",
    "i": "ि",
    "ee": "ी",
    "u": "ु",
    "oo": "ू",
    "e": "े",
    "ai": "ै",
    "o": "ो",
    "au": "ौ",
}

#: Consonants, longest key first at match time.
_CONSONANTS: dict[str, str] = {
    "chh": "छ",
    "sch": "श",
    "ch": "च",
    "sh": "श",
    "th": "थ",
    "ph": "फ",
    "kh": "ख",
    "gh": "घ",
    "dh": "ध",
    "bh": "भ",
    "jh": "झ",
    "ck": "क",
    "ng": "ंग",
    "qu": "क्व",
    "b": "ब",
    "c": "क",
    "d": "ड",
    "f": "फ़",
    "g": "ग",
    "h": "ह",
    "j": "ज",
    "k": "क",
    "l": "ल",
    "m": "म",
    "n": "न",
    "p": "प",
    "q": "क",
    "r": "र",
    "s": "स",
    "t": "ट",
    "v": "व",
    "w": "व",
    "x": "क्स",
    "y": "य",
    "z": "ज़",
}

_HALANT = "्"

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")

#: Names worth getting exactly right rather than approximately. Short list on
#: purpose: it covers the ingredients in the seeded catalogue, and the honest
#: fix for the rest is a Hindi name on the product row.
KNOWN_TERMS: dict[str, str] = {
    "imidacloprid": "इमिडाक्लोप्रिड",
    "thiamethoxam": "थायामेथोक्सम",
    "chlorantraniliprole": "क्लोरएंट्रानिलिप्रोल",
    "cartap": "कार्टाप",
    "lambda": "लैम्ब्डा",
    "cyhalothrin": "साइहैलोथ्रिन",
    "emamectin": "इमामेक्टिन",
    "benzoate": "बेंजोएट",
    "propiconazole": "प्रोपिकोनाज़ोल",
    "tebuconazole": "टेबुकोनाज़ोल",
    "mancozeb": "मैंकोज़ेब",
    "cymoxanil": "साइमोक्सानिल",
    "carbendazim": "कार्बेन्डाज़िम",
    "trifloxystrobin": "ट्राइफ्लोक्सीस्ट्रोबिन",
    "sulphur": "सल्फर",
    "sulfosulfuron": "सल्फोसल्फ्यूरॉन",
    "pendimethalin": "पेंडीमिथालिन",
    "bispyribac": "बिस्पायरिबैक",
    "glyphosate": "ग्लाइफोसेट",
    "thiophanate": "थायोफेनेट",
    "methyl": "मिथाइल",
    "sodium": "सोडियम",
    "gibberellic": "जिबरेलिक",
    "acid": "एसिड",
    "nitrogen": "नाइट्रोजन",
    "phosphorus": "फ़ॉस्फ़ोरस",
    "potassium": "पोटाश",
    "potash": "पोटाश",
    "zinc": "जिंक",
    "boron": "बोरॉन",
    "calcium": "कैल्शियम",
    "iron": "आयरन",
    "urea": "यूरिया",
    "bayer": "बायर",
    "crystal": "क्रिस्टल",
    "crop": "क्रॉप",
    "iffco": "इफ़्को",
    "kribhco": "क्रिभको",
    "coromandel": "कोरोमंडल",
    "dhanuka": "धानुका",
    "rallis": "रैलिस",
    "khushhali": "खुशहाली",
}


#: 'y' acting as a vowel: not word-initial, and not followed by a vowel.
#: Chemical names are full of these -- Pyraclostrobin, Azoxystrobin,
#: Cyhalothrin -- and treating them as the consonant य produces clusters no
#: Hindi speaker can pronounce.
_VOWEL_Y = re.compile(r"(?<=[a-z])y(?![aeiou])")

#: English silent final 'e': ``-azole`` is "-ज़ोल", never "-ज़ोले".
_SILENT_FINAL_E = re.compile(r"(?<=[bcdfghjklmnpqrstvwxz])e$")


def _apply_english_spelling_rules(word: str) -> str:
    """Fold the two English spelling conventions that matter here.

    Applied before transliteration so the machinery below stays a simple
    grapheme mapping rather than a phonology engine.
    """
    word = _VOWEL_Y.sub("i", word)
    return _SILENT_FINAL_E.sub("", word)


def transliterate_word(word: str) -> str:
    """Transliterate one Latin word to Devanagari."""
    lowered = word.lower().strip("'-")
    if not lowered:
        return word
    if lowered in KNOWN_TERMS:
        return KNOWN_TERMS[lowered]

    lowered = _apply_english_spelling_rules(lowered)

    out: list[str] = []
    index = 0
    at_start = True
    length = len(lowered)

    while index < length:
        matched = False

        # Consonant clusters, longest first.
        for size in (3, 2, 1):
            chunk = lowered[index : index + size]
            if chunk in _CONSONANTS:
                consonant = _CONSONANTS[chunk]
                index += size
                # Look ahead for the vowel that follows.
                vowel = ""
                for vsize in (2, 1):
                    vchunk = lowered[index : index + vsize]
                    if vchunk in _VOWEL_MATRA:
                        vowel = _VOWEL_MATRA[vchunk]
                        index += vsize
                        break
                else:
                    # No vowel follows: the consonant is bare, so suppress the
                    # implicit 'a' unless this is the final letter, where Hindi
                    # drops it naturally anyway.
                    if index < length:
                        consonant += _HALANT
                out.append(consonant + vowel)
                at_start = False
                matched = True
                break
        if matched:
            continue

        # A vowel, in independent form only at the start of the word.
        for vsize in (2, 1):
            chunk = lowered[index : index + vsize]
            if chunk in _VOWEL_INDEPENDENT:
                out.append(_VOWEL_INDEPENDENT[chunk] if at_start else _VOWEL_MATRA[chunk] or "अ")
                index += vsize
                at_start = False
                matched = True
                break
        if matched:
            continue

        index += 1  # unmappable character; skip rather than emit noise

    return "".join(out)


def transliterate(text: str) -> str:
    """Replace every Latin word in ``text`` with a Devanagari approximation."""
    return _LATIN_WORD.sub(lambda m: transliterate_word(m.group(0)), text)


def has_latin(text: str) -> bool:
    return bool(_LATIN_WORD.search(text))
