"""Devanagari character classes.

One module because getting this wrong is subtle and the failure is silent.

``\\w`` matches Devanagari *letters* and *digits* but **not** combining marks --
matras, anusvara, virama, nukta are all categories ``Mn``/``Mc``, which ``\\w``
excludes. Tokenising Hindi with a bare ``\\w+`` therefore strips the vowel signs
and turns ``यूरिया`` into ``यरय``: still a string, still matches nothing, no
error anywhere.

The obvious fix -- adding the whole Devanagari block ``\\u0900-\\u097f`` -- is
wrong in the other direction, because the block contains the sentence
punctuation: ``।`` (danda, U+0964) and ``॥`` (double danda, U+0965). Including
them makes ``चाहिए।`` a different word from ``चाहिए``, so a trailing-particle
check silently stops firing and an exact catalogue match silently misses.

:data:`WORD_CHARS` is the block with exactly those two code points cut out.
"""

from __future__ import annotations

import re

#: Devanagari danda and double danda: sentence punctuation living inside the
#: letter block.
DANDA = "।"
DOUBLE_DANDA = "॥"

#: Word characters for Hindi and Latin together: ``\w`` for letters and digits
#: in both scripts, plus the Devanagari block either side of the two danda code
#: points so combining marks survive and punctuation does not.
WORD_CHARS = r"\wऀ-ॣ०-ॿ"

#: One token.
WORD = re.compile(rf"[{WORD_CHARS}]+", re.UNICODE)

#: Anything that is neither a word character nor whitespace.
NON_WORD = re.compile(rf"[^{WORD_CHARS}\s]", re.UNICODE)

#: Word boundaries that survive Devanagari.
#:
#: ```` is defined against ``\w``, which excludes combining marks -- so
#: ``बोरी`` never matches, because the string ends on a matra that ``\w``
#: does not consider a word character and a boundary needs one on the inside.
#: The failure is silent and asymmetric: ``ग्राम`` ends on a consonant and
#: matches, ``बोरी`` and ``मात्रा`` do not, so half a keyword list works and the
#: other half quietly never fires.
#:
#: These are lookarounds over :data:`WORD_CHARS` instead. Use them anywhere a
#: pattern needs to match a whole Hindi word.
BOUNDARY_BEFORE = rf"(?<![{WORD_CHARS}])"
BOUNDARY_AFTER = rf"(?![{WORD_CHARS}])"


def whole_word(pattern: str) -> str:
    """Wrap ``pattern`` so it matches only as a complete word, in any script."""
    return f"{BOUNDARY_BEFORE}(?:{pattern}){BOUNDARY_AFTER}"


def words(text: str) -> list[str]:
    """Tokenise, keeping matras and dropping punctuation."""
    return WORD.findall(text or "")


def strip_punctuation(text: str) -> str:
    """Replace punctuation with spaces, leaving Devanagari intact."""
    return NON_WORD.sub(" ", text or "")
