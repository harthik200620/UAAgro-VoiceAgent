"""Knowing when the farmer is done (§11.1 CLOSE, §11.4).

A helpline call ends one of three ways, and the first live test showed that
none of them was actually ending it:

* the farmer says so -- "बस, धन्यवाद", "ठीक है, रखता हूँ", "thank you";
* the agent asks "और कुछ पूछना है?" and the farmer says "नहीं";
* the farmer goes quiet and stays quiet.

The first two are decided here, on the words, without the model: a farewell
is not a question, and asking a language model whether "धन्यवाद" means
goodbye costs a second and a half of silence at the one moment the caller has
already started to put the phone down. The closing line comes from the
published config (``closing_template``) and is rendered into the audio cache
when the call is built, so the goodbye is instant. The third is the silence
ladder in the pipeline, which uses the two prompts below.

The rule for "the farmer is done" is deliberately narrow. "ठीक है" alone is
an acknowledgement that continues a conversation as often as it ends one, so
it counts only when nothing else is said with it and the agent had just asked
whether there was anything more. "नमस्ते" ends a call only when it stands
alone -- at the start of a call it is a greeting, and the greeting has already
been answered by then.
"""

from __future__ import annotations

import re

from ..text.script import whole_word, words
from ..text.speech import split_sentences

#: The default closing, used when a config has none. The published
#: ``closing_template`` normally replaces it.
CLOSING_LINE_HI = "धन्यवाद! और कुछ पूछना हो तो कभी भी फ़ोन कीजिए। नमस्ते।"

#: §11.4's silence ladder, spoken from cache.
SILENCE_PROMPT_HI = "जी, मैं सुन रहा हूँ।"
SILENCE_WARN_HI = "और कुछ पूछना हो तो बताइए।"

#: Words that say goodbye. Any of these, with nothing substantive beside it,
#: ends the call.
_FAREWELL_WORDS: frozenset[str] = frozenset(
    {
        "धन्यवाद",
        "धन्यबाद",
        "शुक्रिया",
        "थैंक्स",
        "थैंक्यू",
        "थैंक",
        "थैंकयू",
        "थेंक्स",
        "thanks",
        "thank",
        "thankyou",
        "thx",
        "बाय",
        "bye",
        "byebye",
        "अलविदा",
        "नमस्ते",
        "नमस्कार",
        "प्रणाम",
        "राम",
        "रखता",
        "रखती",
        "रखिए",
        "रखो",
        "रखूँ",
        "रखूं",
        "रखते",
        "चलता",
        "चलती",
        "चलिए",
        "अच्छा",
        "डन",
        "done",
    }
)
#: Phrases that say "that is all". Matched as whole words in sequence.
_DONE_PHRASES = (
    "बस इतना ही",
    "इतना ही",
    "और कुछ नहीं",
    "कुछ नहीं",
    "बस बस",
    "हो गया",
    "हो गई",
    "काम हो गया",
    "बात हो गई",
    "फ़ोन रखता",
    "फोन रखता",
    "फ़ोन रखती",
    "फोन रखती",
    "फ़ोन रखो",
    "फोन रखो",
    "रख देता",
    "रख देती",
    "that's all",
    "thats all",
    "that is all",
    "nothing else",
    "no more",
    "bas",
    "bus",
    "theek hai bas",
)
#: A plain "no", for the turn after "और कुछ पूछना है?".
_NEGATIVES: frozenset[str] = frozenset(
    {"नहीं", "नही", "ना", "नहि", "no", "nope", "nahi", "nahin", "na", "बस", "bas", "bus"}
)
#: Words that carry no content of their own and may sit beside a farewell.
_FILLER: frozenset[str] = frozenset(
    {
        "जी",
        "हाँ",
        "हां",
        "ठीक",
        "है",
        "हैं",
        "ओके",
        "ok",
        "okay",
        "अच्छा",
        "बहुत",
        "आपका",
        "आपको",
        "भी",
        "सर",
        "भाई",
        "साहब",
        "भैया",
        "चलो",
        "चलिए",
        "फिर",
        "तो",
        "अब",
        "बस",
        "बहुत-बहुत",
        "बहोत",
        "यू",
        "you",
        "too",
        "sir",
        "ji",
        "haan",
        "theek",
        "thik",
        "hai",
        "acha",
        "achha",
        "और",
        "कुछ",
        "नहीं",
        "नही",
        "ना",
        "no",
        "nahi",
        "मैं",
        "मै",
        "हूँ",
        "हूं",
        "फ़ोन",
        "फोन",
        "देता",
        "देती",
        "रखता",
        "रखती",
        "इतना",
        "ही",
        "काम",
        "बात",
        "हो",
        "गया",
        "गई",
        "गयी",
        "राम",
        "कर",
        "करता",
        "करती",
        "लेता",
        "लेती",
        "देन",
        "then",
        "दीजिए",
        "दीजिये",
        "दो",
        "सब",
        "अभी",
    }
)
#: The agent asked whether there was anything more.
_ASKED_MORE = re.compile(
    r"(और कुछ|कुछ और|और कोई|और क्या|कोई और|कुछ और पूछ|और मदद|anything else|कुछ और चाहिए)",
    re.IGNORECASE,
)
_QUESTION_MARKERS = re.compile(
    whole_word(
        "क्या|कैसे|कैसा|कब|कहाँ|कहां|कितना|कितनी|कितने|कौन|कौनसा|कौन सा|किस|क्यों|क्यूँ|"
        "रेट|दाम|भाव|स्टॉक|बताइए|बताओ|बताना|चाहिए|मिलेगा|मिलेगी|कीमत|price|rate|how|what|"
        "when|where|which|why"
    ),
    re.IGNORECASE,
)

#: The farmer asking, in so many words, for the line to be ended. These end
#: the call whatever else is said around them: "you can hang the call" carries
#: four words that :func:`is_farewell` would read as substance, and "कॉल काट
#: दीजिए" is phrased as a request, which is exactly what a question mark
#: veto would throw away. An explicit instruction needs neither test.
_HANGUP_REQUESTS = (
    "कॉल काट",
    "कॉल कट",
    "काल काट",
    "फ़ोन काट",
    "फोन काट",
    "लाइन काट",
    "कॉल ख़त्म",
    "कॉल खत्म",
    "कॉल ख़तम",
    "कॉल खतम",
    "कॉल समाप्त",
    "फ़ोन रख",
    "फोन रख",
    "कॉल बंद",
    "फ़ोन बंद",
    "फोन बंद",
    "रख दीजिए",
    "रख दीजिये",
    "रख दो",
    "हैंग द कॉल",
    "हैंग अप",
    "हैंग कर",
    "डिस्कनेक्ट",
    "hang up",
    "hang the call",
    "hang it up",
    "cut the call",
    "end the call",
    "disconnect",
)

#: The agent's own last sentence taking leave. A model that has decided the
#: call is over says so -- "अभी कॉल खत्म करते हैं ... धन्यवाद" -- and used to
#: be answered by the caller hanging up, because nothing was watching for it.
_AGENT_TAKES_LEAVE = re.compile(
    r"(कॉल\s*(ख़त्म|खत्म|ख़तम|खतम|समाप्त|बंद)|फ़ोन\s*रख|फोन\s*रख|नमस्ते|नमस्कार|अलविदा|"
    r"फिर\s*मिलते|good\s*bye|goodbye)",
    re.IGNORECASE,
)

#: Anything longer than this is a sentence, not a goodbye.
MAX_FAREWELL_WORDS = 8


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in words(text)]


def _has_phrase(tokens: list[str], phrase: str) -> bool:
    parts = phrase.lower().split()
    n = len(parts)
    return any(tokens[i : i + n] == parts for i in range(len(tokens) - n + 1))


def is_farewell(transcript: str) -> bool:
    """The farmer said goodbye, or said that was all.

    Short, carrying a farewell word or a done-phrase, and nothing that reads
    as a question or a product beside it.
    """
    tokens = _tokens(transcript)
    if not tokens or len(tokens) > MAX_FAREWELL_WORDS:
        return False
    if _QUESTION_MARKERS.search(transcript):
        return False
    said_goodbye = any(t in _FAREWELL_WORDS for t in tokens)
    said_done = any(_has_phrase(tokens, p) for p in _DONE_PHRASES)
    if not (said_goodbye or said_done):
        return False
    # "अच्छा" and "नमस्ते" alone are ambiguous; they end a call only with
    # nothing else of substance said. Everything else must also be filler.
    substantive = [t for t in tokens if t not in _FILLER and t not in _FAREWELL_WORDS]
    if substantive:
        return False
    if tokens in (["अच्छा"], ["ठीक", "है"]):
        return False
    return True


def declines_more(transcript: str) -> bool:
    """A plain "no" -- meaningful only after the agent asked for more."""
    tokens = _tokens(transcript)
    if not tokens or len(tokens) > 5:
        return False
    if _QUESTION_MARKERS.search(transcript):
        return False
    if not any(t in _NEGATIVES for t in tokens) and not any(
        _has_phrase(tokens, p) for p in _DONE_PHRASES
    ):
        return False
    return all(t in _FILLER or t in _NEGATIVES or t in _FAREWELL_WORDS for t in tokens)


def asks_to_hang_up(transcript: str) -> bool:
    """The farmer told the agent to end the call.

    Unlike a farewell this is an instruction, so nothing else in the sentence
    can talk it out of being one: neither the length cap nor the question-mark
    veto applies. "क्या आप कॉल काट सकते हैं?" is a request, not an enquiry.
    """
    tokens = _tokens(transcript)
    if not tokens:
        return False
    return any(_has_phrase(tokens, phrase) for phrase in _HANGUP_REQUESTS)


def agent_said_goodbye(text: str) -> bool:
    """The agent's own reply took leave of the caller.

    Checked on the last sentence only, and never when that sentence asks
    something: the closing line's own "और कुछ पूछना हो तो कभी भी फ़ोन कीजिए"
    sits in front of "नमस्ते", and a mid-call "नमस्ते जी, बताइए" is a
    greeting. A reply that ends by saying goodbye ends the call -- the model
    is often the first to notice that the conversation is finished, and until
    this existed the caller had to hang up on an agent that had just said
    farewell.
    """
    if not text:
        return False
    sentences = [s for s in split_sentences(text) if s.strip()]
    if not sentences:
        return False
    tail = sentences[-1]
    if _QUESTION_MARKERS.search(tail):
        return False
    return _AGENT_TAKES_LEAVE.search(tail) is not None


def asked_for_more(agent_text: str | None) -> bool:
    """Whether the agent's last line asked if there was anything else."""
    if not agent_text:
        return False
    return _ASKED_MORE.search(agent_text) is not None


def farmer_is_done(transcript: str, *, last_agent_line: str | None) -> bool:
    """The rule the agent applies before asking the model anything."""
    if asks_to_hang_up(transcript):
        return True
    return is_farewell(transcript) or (
        declines_more(transcript) and asked_for_more(last_agent_line)
    )


__all__ = (
    "CLOSING_LINE_HI",
    "MAX_FAREWELL_WORDS",
    "SILENCE_PROMPT_HI",
    "SILENCE_WARN_HI",
    "agent_said_goodbye",
    "asked_for_more",
    "asks_to_hang_up",
    "declines_more",
    "farmer_is_done",
    "is_farewell",
)
