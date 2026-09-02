"""The outbound script as the panel edits it (§13.2, §15 Flows).

An outbound call says a fixed sequence of things. Two of them are the law's
words, not the operator's -- the disclosure that this is an automated call from
a named business, and the unconditional opt-out -- and those are the same on
every version and cannot be edited. The rest is the operator's: the message
(an offer today, a meeting invitation next month), the follow-up question, what
the agent says when the farmer presses one, and how it signs off.

This module is the boundary between the JSON the panel stores on the config
version and the lines the conversation speaks. It lives in the domain package
because both sides of that boundary -- the control plane that edits and the
media path that speaks -- need the same defaults, and two copies of a Hindi
sentence drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: §13.2's mandatory opening. Business name, automated nature, then the
#: identity check. The name placeholder is filled per call.
DISCLOSURE = "नमस्ते! मैं यूए एग्रो के नवीन खुशहाली किसान सेवा केंद्र से बोल रहा हूँ। यह एक ऑटोमैटिक कॉल है।"
VERIFY_PERSON = "क्या मैं {नाम} जी से बात कर रहा हूँ?"
VERIFY_PERSON_UNKNOWN = "क्या मैं सही व्यक्ति से बात कर रहा हूँ?"

DEFAULT_ASK_TIME = "आपका दो मिनट का समय ले सकता हूँ?"
DEFAULT_MESSAGE = ""
DEFAULT_THEN_ASK = (
    "अगर आप यह ऑफ़र लेना चाहते हैं, तो अपने फ़ोन पर एक दबाइए — या बस 'हाँ' बोल दीजिए। "
    "और जानकारी के लिए दो दबाइए।"
)
#: Spoken after a yes. Says nothing about WhatsApp on purpose: the line that
#: claims a message was sent is added only once one actually was (§13.2).
DEFAULT_ON_PRESS_1 = "जी बहुत अच्छा। केंद्र पर अपना नाम बताइएगा, आपको यह मिल जाएगा।"
WHATSAPP_SENT = "मैंने पूरी जानकारी आपके व्हाट्सऐप पर भेज दी है।"
#: §13.2: immediate, unconditional, confirmed out loud. Not editable.
OPT_OUT = "जी बिल्कुल, मैं आपका नंबर आज ही हटा देता हूँ। आगे से कॉल नहीं आएगी। असुविधा के लिए क्षमा कीजिए।"
DEFAULT_CLOSING = "जी, आपका समय देने के लिए धन्यवाद। नमस्ते जी।"

#: The most words the editable message may carry. §13.2 caps the pitch at
#: about forty seconds; at Hindi speaking pace that is roughly 110 words.
MAX_MESSAGE_WORDS = 110

#: Placeholders the operator may type. Both spellings of each are accepted so
#: a Hindi-typing operator and an English-typing one write the same script.
_PLACEHOLDERS = {
    "name": ("{नाम}", "{name}"),
    "centre": ("{केंद्र}", "{centre}"),
}

_EDITABLE = ("ask_time", "message", "then_ask", "on_press_1", "closing")

#: Panel key -> stored key, for the editable lines only.
PANEL_KEYS = {
    "askTime": "ask_time",
    "message": "message",
    "thenAsk": "then_ask",
    "onPress1": "on_press_1",
    "closing": "closing",
}


@dataclass(frozen=True, slots=True)
class OutboundScript:
    """Every line of one outbound version, defaults already applied."""

    ask_time: str = DEFAULT_ASK_TIME
    message: str = DEFAULT_MESSAGE
    then_ask: str = DEFAULT_THEN_ASK
    on_press_1: str = DEFAULT_ON_PRESS_1
    closing: str = DEFAULT_CLOSING

    # -- the fixed lines -------------------------------------------------- #

    @property
    def opening(self) -> str:
        """The disclosure plus the identity check, with the name placeholder."""
        return f"{DISCLOSURE} {VERIFY_PERSON}"

    @property
    def opt_out(self) -> str:
        return OPT_OUT

    # -- storage ---------------------------------------------------------- #

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> OutboundScript:
        """Build from the JSON on ``agent_configs.script``.

        Unknown keys are ignored and blank values fall back to the default,
        so an older version, or one saved by a future panel with more fields,
        still speaks a complete script.
        """
        values: dict[str, str] = {}
        for key in _EDITABLE:
            value = (raw or {}).get(key)
            if isinstance(value, str) and value.strip():
                values[key] = " ".join(value.split())
        return cls(**values)

    def to_config(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in _EDITABLE}

    def to_panel(self) -> dict[str, str]:
        """The contract's ``OutboundScript`` shape, locked lines included."""
        return {
            "opening": self.opening,
            "askTime": self.ask_time,
            "message": self.message,
            "thenAsk": self.then_ask,
            "onPress1": self.on_press_1,
            "onPress2": "knowledge_base",
            "optOut": self.opt_out,
            "closing": self.closing,
        }

    @classmethod
    def from_panel(cls, raw: dict[str, Any]) -> OutboundScript:
        """The inverse of :meth:`to_panel`. Locked keys are ignored on purpose."""
        return cls.from_config({snake: raw.get(camel) for camel, snake in PANEL_KEYS.items()})

    # -- validation ------------------------------------------------------- #

    def problems(self) -> list[str]:
        """Why this script cannot be published, in the operator's terms."""
        out: list[str] = []
        if not self.message.strip():
            out.append("The message is empty. Say what the call is about.")
        words = len(self.message.split())
        if words > MAX_MESSAGE_WORDS:
            out.append(
                f"The message is {words} words; keep it under {MAX_MESSAGE_WORDS} "
                "so the farmer hears it in under forty seconds."
            )
        return out

    def digits_used(self) -> list[str]:
        """Lines that contain digits.

        Not an error -- the speech normaliser reads "₹50" as "पचास रुपये" --
        but the panel shows the spoken form so the operator can hear what the
        farmer will.
        """
        return [key for key in _EDITABLE if re.search(r"\d", getattr(self, key))]

    # -- rendering -------------------------------------------------------- #

    @staticmethod
    def render(text: str, *, name: str | None, centre: str | None) -> str:
        """Fill the placeholders; an unknown farmer gets no dangling brace."""
        out = text
        for spelling in _PLACEHOLDERS["name"]:
            out = out.replace(spelling, name or "")
        for spelling in _PLACEHOLDERS["centre"]:
            out = out.replace(spelling, centre or "हमारे केंद्र")
        return " ".join(out.split())

    def rendered_opening(self, *, name: str | None) -> str:
        """The disclosure, then the identity check that fits what we know."""
        verify = VERIFY_PERSON.replace("{नाम}", name) if name else VERIFY_PERSON_UNKNOWN
        return f"{DISCLOSURE} {verify}"


__all__ = (
    "DISCLOSURE",
    "MAX_MESSAGE_WORDS",
    "OPT_OUT",
    "PANEL_KEYS",
    "WHATSAPP_SENT",
    "OutboundScript",
)
