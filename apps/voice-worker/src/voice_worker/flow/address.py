"""How the agent addresses the farmer, kept in proportion (§11.3).

The model was told "use the farmer's name with जी", and it did -- at the start
of every sentence. A ten-turn call became "हार्थिक जी, ... हार्थिक जी, ... जी
हार्थिक जी", which is not respect, it is a tic. The same thing happened with
"जी," as an opener and with "एक क्षण, देख रहा हूँ" as a way of saying nothing.

The prompt now asks for restraint, and a prompt is a request. This module is
the rule: the greeting has already used the name, so replies do not; a bare
"जी," opener is dropped; "सर" is allowed a couple of times per call and no
more; and a sentence that only announces a delay is not spoken at all. It runs
on every generated sentence before validation, so what the farmer hears, what
the validator checks and what the memory records are the same words.

It also decides what a *backchannel* is -- "हाँ", "जी", "अच्छा" while the agent
is talking -- because that is a listener keeping up, not a question, and the
pipeline continues rather than answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..text.script import whole_word, words
from ..text.translit import has_latin, transliterate

#: How many times "सर" may be said in a whole call before it is trimmed.
SIR_PER_CALL = 2

#: "जी" on its own at the front of a sentence. "जी हाँ" and "जी नहीं" are
#: answers and are kept; a lone "जी," is filler.
_LEADING_JI = re.compile(r"^\s*जी\s*[,،]\s*")
#: A trailing "जी" before the full stop: "... मिलेगा जी।". A trailing "सर"
#: is the budget's business, not this pattern's.
_TRAILING_JI = re.compile(r"[,،]?\s*जी\s*(?=[।.!?]*\s*$)")
#: A comma left stranded against the full stop once a vocative went.
_COMMA_BEFORE_STOP = re.compile(r"\s*[,،]\s*(?=[।.!?])")
_SIR = re.compile(whole_word(r"सर(?:\s+जी)?"))
_TIDY_COMMAS = re.compile(r"(?:\s*[,،]\s*){2,}")
_LEADING_PUNCT = re.compile("^[\\s,،\\-\u2013\u2014]+")
_SPACE_BEFORE_STOP = re.compile(r"\s+([।.!?,])")
_MULTISPACE = re.compile(r"\s{2,}")

#: A sentence that only says "wait, I am checking". The words vary; the
#: shape does not: a delay word, or a looking/checking verb, and nothing else.
_FILLER = re.compile(
    r"^[\s,،]*(?:जी[,،]?\s*)?"
    r"(?:(?:एक|ज़रा|जरा|बस)\s*)?"
    r"(?:(?:क्षण|मिनट|मिनिट|सेकंड|सेकेंड|पल)\s*)?"
    r"(?:(?:रुकिए|रुकिये|रुकें|रुको|ठहरिए|ठहरिये|इंतज़ार कीजिए|इंतजार कीजिए)\s*)?"
    r"[,،]?\s*"
    r"(?:(?:मैं|मै)\s*)?"
    r"(?:(?:अभी|ज़रा|जरा)\s*)?"
    r"(?:देख(?:ता|ती)\s*हूँ|देख(?:ता|ती)\s*हूं|देख\s*रह[ाी]\s*हूँ|देख\s*रह[ाी]\s*हूं|"
    r"देख\s*लेत[ाी]\s*हूँ|देख\s*लेत[ाी]\s*हूं|चेक\s*कर(?:ता|ती)\s*हूँ|चेक\s*कर(?:ता|ती)\s*हूं|"
    r"पता\s*कर(?:ता|ती)\s*हूँ|पता\s*कर(?:ता|ती)\s*हूं|देखत[ाी]\s*हूँ)?"
    r"[\s।.!]*$"
)

#: What a listener says to show they are following. Any utterance made only
#: of these, up to three words, is a backchannel rather than a turn.
BACKCHANNELS: frozenset[str] = frozenset(
    {
        "हाँ", "हां", "हा", "हँ", "जी", "हम्म", "हम्", "हूँ", "हूं", "अच्छा",
        "ठीक", "है", "ओके", "ओ", "के", "सही", "बिल्कुल", "बिलकुल", "हैलो", "हेलो", "हलो",
        "बताइए", "बताइये", "बताओ", "बोलिए", "बोलो", "सुन", "रहा", "रही", "समझ", "गया",
        "गयी", "गई", "ok", "okay", "yes", "yeah", "hmm", "hm", "haan", "ha", "ji", "hello",
        "achha", "acha", "theek", "thik", "right", "sahi",
    }
)


@dataclass
class AddressBudget:
    """Per-call state: which names to strip, and how much "सर" is left."""

    #: Forms the model might write the farmer's name in. Filled by
    #: :func:`name_forms`; empty for an unknown caller.
    name_forms: tuple[str, ...] = ()
    sir_allowed: int = SIR_PER_CALL
    sir_used: int = 0
    _name_pattern: re.Pattern[str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        forms = [f for f in self.name_forms if f]
        if forms:
            joined = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
            # "हार्थिक जी", "जी हार्थिक जी", "Harthik जी", or the bare name.
            self._name_pattern = re.compile(
                whole_word(rf"(?:जी\s+)?(?:{joined})(?:\s+जी)?"), re.IGNORECASE
            )


def name_forms(name: str | None) -> tuple[str, ...]:
    """The spellings a name may take in the model's output.

    The record holds "Harthik"; the model writes "हार्थिक". Both are matched,
    and so are the first word of a two-word name and the transliteration of
    it, because the model shortens names the way people do.
    """
    if not name or not name.strip():
        return ()
    forms: list[str] = []
    for part in (name.strip(), name.strip().split()[0]):
        if part and part not in forms:
            forms.append(part)
        if has_latin(part):
            hindi = transliterate(part).strip()
            if hindi and hindi not in forms:
                forms.append(hindi)
    return tuple(forms)


def is_filler(sentence: str) -> bool:
    """A sentence that only announces a delay."""
    stripped = sentence.strip()
    if not stripped or len(words(stripped)) > 8:
        return False
    return _FILLER.match(stripped) is not None and bool(words(stripped))


def is_backchannel(transcript: str) -> bool:
    """"हाँ", "जी", "अच्छा", "ठीक है" -- a listener, not a question."""
    tokens = [w.lower() for w in words(transcript)]
    if not tokens or len(tokens) > 3:
        return False
    return all(token in BACKCHANNELS for token in tokens)


def trim_address(sentence: str, budget: AddressBudget) -> str:
    """One sentence, with the vocatives it does not need removed.

    Returns an empty string when nothing worth saying is left, which is how a
    pure filler sentence is dropped.
    """
    text = sentence.strip()
    if not text:
        return ""
    if is_filler(text):
        return ""

    if budget._name_pattern is not None:
        text = budget._name_pattern.sub("", text)

    text = _LEADING_JI.sub("", text)

    def spend_sir(match: re.Match[str]) -> str:
        if budget.sir_used < budget.sir_allowed:
            budget.sir_used += 1
            return match.group(0)
        return ""

    text = _SIR.sub(spend_sir, text)
    text = _TRAILING_JI.sub("", text)

    text = _TIDY_COMMAS.sub(", ", text)
    text = _COMMA_BEFORE_STOP.sub("", text)
    text = _LEADING_PUNCT.sub("", text)
    text = _SPACE_BEFORE_STOP.sub(r"\1", text)
    text = _MULTISPACE.sub(" ", text).strip()

    # A trailing "सर" that was trimmed could leave "मिलेगा ।"; and a sentence
    # reduced to punctuation is nothing.
    if not words(text):
        return ""
    return text


__all__ = (
    "BACKCHANNELS",
    "SIR_PER_CALL",
    "AddressBudget",
    "is_backchannel",
    "is_filler",
    "name_forms",
    "trim_address",
)
