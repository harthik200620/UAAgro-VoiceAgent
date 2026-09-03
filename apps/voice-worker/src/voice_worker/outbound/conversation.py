"""The outbound conversation (§13.2).

::

    CONNECT ─▶ IDENTIFY_SELF ─▶ VERIFY_PERSON ─▶ PERMISSION ─▶ PITCH
            ─▶ INTEREST_CHECK ─▶ CONFIRM ─▶ WHATSAPP ─▶ CLOSE
                        │            │
                        │            └─▶ OBJECTION ─▶ INTEREST_CHECK (max 2)
                        └─▶ OPT_OUT ─▶ SUPPRESS ─▶ CLOSE

Four rules from §13.2 that are enforced here rather than left to the prompt,
because each is the kind of thing a well-meaning model gets wrong in the
direction of being helpful:

**Disclosure is first, and barge-in is suppressed for 600 ms.** Identifying the
business and the automated nature of the call is both a legal posture and the
thing that stops the call being taken for fraud. A farmer who says "हाँ?" over
the first syllable must still hear it.

**Never pitch to a third party.** If the person who answered is not the farmer,
the offer is not delivered -- a callback is logged and the call ends. A model
told to "deliver the offer" will happily deliver it to whoever picked up.

**Opt-out is synchronous.** ``internal_dnc`` is written before the call ends,
never queued. §13.2 is explicit, and the reason is that a background job that
fails leaves a person who asked to be removed still on the list, with a record
saying they were removed.

**Never claim a message was sent when it was not.** If the WhatsApp dispatch
fails, the agent reads the offer aloud and raises a ticket. Saying "मैंने भेज
दिया है" when nothing was sent is a small lie that destroys the thing the whole
call was trying to build.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

import structlog

from ..text.script import whole_word

log = structlog.get_logger(__name__)

#: §13.2: barge-in is suppressed for this long so the mandatory disclosure is
#: always heard, even by a caller who starts talking immediately.
DISCLOSURE_PROTECT_MS = 600

#: §13.2: two objection loops, then accept the no. A third attempt is the point
#: at which a service call becomes a nuisance call.
MAX_OBJECTION_LOOPS = 2

#: §13.2: the offer template goes out within this long of confirmation.
WHATSAPP_DISPATCH_S = 5.0

#: §13.2's pitch ceiling. Forty seconds of speech is roughly 110 Hindi words at
#: the pace §5.3 sets.
MAX_PITCH_WORDS = 110


class OutboundState(StrEnum):
    """§13.2."""

    CONNECT = "connect"
    IDENTIFY_SELF = "identify_self"
    VERIFY_PERSON = "verify_person"
    PERMISSION = "permission"
    PITCH = "pitch"
    INTEREST_CHECK = "interest_check"
    OBJECTION = "objection"
    CONFIRM = "confirm"
    WHATSAPP = "whatsapp"
    OPT_OUT = "opt_out"
    SUPPRESS = "suppress"
    TRANSFER = "transfer"
    CLOSE = "close"


TRANSITIONS: dict[OutboundState, frozenset[OutboundState]] = {
    OutboundState.CONNECT: frozenset({OutboundState.IDENTIFY_SELF}),
    OutboundState.IDENTIFY_SELF: frozenset(
        {OutboundState.VERIFY_PERSON, OutboundState.OPT_OUT, OutboundState.CLOSE}
    ),
    # A third party ends the call at VERIFY_PERSON. There is deliberately no
    # edge from here to PITCH for the not-the-farmer case.
    OutboundState.VERIFY_PERSON: frozenset(
        {OutboundState.PERMISSION, OutboundState.OPT_OUT, OutboundState.CLOSE}
    ),
    OutboundState.PERMISSION: frozenset(
        {OutboundState.PITCH, OutboundState.OPT_OUT, OutboundState.CLOSE}
    ),
    OutboundState.PITCH: frozenset(
        {OutboundState.INTEREST_CHECK, OutboundState.OPT_OUT, OutboundState.TRANSFER}
    ),
    OutboundState.INTEREST_CHECK: frozenset(
        {
            OutboundState.CONFIRM,
            OutboundState.OBJECTION,
            OutboundState.OPT_OUT,
            OutboundState.TRANSFER,
            OutboundState.CLOSE,
        }
    ),
    OutboundState.OBJECTION: frozenset(
        {OutboundState.INTEREST_CHECK, OutboundState.OPT_OUT, OutboundState.CLOSE}
    ),
    OutboundState.CONFIRM: frozenset({OutboundState.WHATSAPP, OutboundState.CLOSE}),
    OutboundState.WHATSAPP: frozenset({OutboundState.CLOSE}),
    OutboundState.OPT_OUT: frozenset({OutboundState.SUPPRESS}),
    OutboundState.SUPPRESS: frozenset({OutboundState.CLOSE}),
    OutboundState.TRANSFER: frozenset({OutboundState.CLOSE}),
    OutboundState.CLOSE: frozenset(),
}


# --------------------------------------------------------------------------- #
# Recognisers
# --------------------------------------------------------------------------- #

_AFFIRMATIVE = re.compile(
    whole_word("हाँ|हां|हा|जी हाँ|जी हां|जी|बिल्कुल|ठीक है|ठीक|चाहिए|yes|ok|okay|sure|haan|ha ji"),
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    whole_word("नहीं|ना|नही|नहीं चाहिए|रहने दो|no|nahi|nahin|not interested"),
    re.IGNORECASE,
)
#: §13.2: `9` or "don't call me again". Deliberately broader than the DTMF key,
#: because a farmer in a field cannot press one.
_OPT_OUT = re.compile(
    whole_word(
        "मत करो कॉल|कॉल मत|फ़ोन मत|फोन मत|दोबारा मत|नंबर हटा|नंबर हटाओ|"
        "परेशान मत|बंद करो|remove my number|stop calling|do not call|dont call"
    ),
    re.IGNORECASE,
)
#: Someone other than the farmer answered.
_THIRD_PARTY = re.compile(
    whole_word(
        "नहीं हैं|घर पर नहीं|बाहर गए|खेत गए|मैं नहीं|दूसरा नंबर|"
        "wrong number|not here|not at home|he is out|she is out"
    ),
    re.IGNORECASE,
)


class Reply(StrEnum):
    """What the farmer's turn meant."""

    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    OPT_OUT = "opt_out"
    THIRD_PARTY = "third_party"
    UNCLEAR = "unclear"


def classify_reply(text: str, *, dtmf: str | None = None) -> Reply:
    """§13.2: DTMF and speech are handled identically.

    A farmer in a field may not be able to press a key and one in a noisy market
    may not be heard, so both modalities are offered and neither is privileged
    -- except that a keypress is unambiguous where speech is not, so it is
    checked first.
    """
    if dtmf is not None:
        match dtmf.strip():
            case "1":
                return Reply.AFFIRMATIVE
            case "2":
                return Reply.NEGATIVE
            case "9":
                return Reply.OPT_OUT

    if not text or not text.strip():
        return Reply.UNCLEAR

    # Opt-out first. "नहीं, दोबारा मत करना" is both a refusal and an opt-out,
    # and treating it as a plain no would leave the farmer on the list.
    if _OPT_OUT.search(text):
        return Reply.OPT_OUT
    if _THIRD_PARTY.search(text):
        return Reply.THIRD_PARTY
    if _NEGATIVE.search(text):
        return Reply.NEGATIVE
    if _AFFIRMATIVE.search(text):
        return Reply.AFFIRMATIVE
    return Reply.UNCLEAR


# --------------------------------------------------------------------------- #
# The pitch
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Offer:
    """One offer, from the ``offers`` table.

    §13.2: the pitch is *rendered from this record, never improvised*. Every
    field the script needs is here, so a model asked to fill a gap has nothing
    to invent -- which is the point.
    """

    name_hi: str
    products_hi: Sequence[str]
    saving_rupees: Decimal
    valid_until_hi: str
    centre_name_hi: str

    def render(self) -> str:
        """§13.2's structure: what, how much, which products, until when, where.

        A fixed template rather than a prompt. The saving is a number a farmer
        will act on, and a model paraphrasing "up to ₹500" into "a big saving"
        makes the call useless while sounding better.
        """
        products = ", ".join(self.products_hi)
        return (
            f"{self.name_hi} — {products} पर "
            f"{self.saving_rupees} रुपये तक की बचत। "
            f"यह ऑफ़र {self.valid_until_hi} तक है। "
            f"आप {self.centre_name_hi} से ले सकते हैं।"
        )

    @property
    def within_length_cap(self) -> bool:
        return len(self.render().split()) <= MAX_PITCH_WORDS


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #


@dataclass
class OutboundFlow:
    """One outbound call's position and counters."""

    state: OutboundState = OutboundState.CONNECT
    history: list[OutboundState] = field(default_factory=lambda: [OutboundState.CONNECT])
    objection_loops: int = 0
    confirmed: bool = False
    opted_out: bool = False
    whatsapp_sent: bool = False
    #: Set when the dispatch failed and the offer was read aloud instead.
    spoke_offer_aloud: bool = False

    def can(self, target: OutboundState) -> bool:
        return target in TRANSITIONS[self.state]

    def to(self, target: OutboundState) -> OutboundState:
        if not self.can(target):
            raise ValueError(f"{self.state.value} -> {target.value} is not valid")
        self.state = target
        self.history.append(target)
        return target

    def next_after_interest(self, reply: Reply) -> OutboundState:
        """Where INTEREST_CHECK goes, given what the farmer said.

        The objection loop is capped here rather than in the prompt. §13.2 caps
        it at two, and a model asked to "handle objections" has no natural stop
        -- it will keep finding new angles, which is exactly how a service call
        becomes a nuisance call.
        """
        match reply:
            case Reply.OPT_OUT:
                return OutboundState.OPT_OUT
            case Reply.AFFIRMATIVE:
                return OutboundState.CONFIRM
            case Reply.NEGATIVE | Reply.UNCLEAR:
                if self.objection_loops < MAX_OBJECTION_LOOPS:
                    self.objection_loops += 1
                    return OutboundState.OBJECTION
                # §13.2: then accept the no.
                return OutboundState.CLOSE
            case _:
                return OutboundState.CLOSE


@dataclass
class OutboundAgent:
    """Drives §13.2's script. Everything spoken is a fixed string or an offer.

    No LLM. An outbound promotional call is the one conversation in this system
    where every line is scripted, legally constrained and identical for every
    recipient -- and a model that improvised here could pitch a product the
    campaign does not cover, or vary the disclosure.
    """

    offer: Offer
    farmer_name_hi: str | None = None
    flow: OutboundFlow = field(default_factory=OutboundFlow)
    #: Written synchronously on opt-out (§13.2). Injected so the test can prove
    #: it was awaited before the call ended.
    suppress: Callable[[], Awaitable[None]] | None = None
    #: Returns True when the template was accepted for delivery.
    send_whatsapp: Callable[[], Awaitable[bool]] | None = None
    create_ticket: Callable[[str], Awaitable[str]] | None = None

    def disclosure(self) -> str:
        """§13.2's mandatory opening. Spoken before anything else, every time."""
        return "नमस्ते! मैं यूए एग्रो के नवीन खुशहाली किसान सेवा केंद्र से बोल रहा हूँ। यह एक ऑटोमैटिक कॉल है।"

    def verify_person(self) -> str:
        who = f"{self.farmer_name_hi} जी" if self.farmer_name_hi else "आप"
        return f"क्या मैं {who} से बात कर रहा हूँ?"

    def permission(self) -> str:
        return "आपका दो मिनट का समय ले सकता हूँ?"

    def interest_check(self) -> str:
        """§13.2 offers both modalities in one sentence."""
        return "अगर आप यह ऑफ़र लेना चाहते हैं, तो अपने फ़ोन पर एक दबाइए — या बस 'हाँ' बोल दीजिए।"

    async def handle_opt_out(self) -> str:
        """§13.2: immediate, unconditional, confirmed out loud.

        The suppression is awaited *before* the confirmation is returned. A
        background job here would let the call end with the farmer told they
        were removed and the record saying so, while the write had not
        happened -- and the next campaign would call them again.
        """
        self.flow.to(OutboundState.OPT_OUT)
        if self.suppress is not None:
            await self.suppress()
        self.flow.opted_out = True
        self.flow.to(OutboundState.SUPPRESS)
        log.info("outbound.opted_out")
        return "जी बिल्कुल, मैं आपका नंबर हटा देता हूँ। असुविधा के लिए क्षमा कीजिए।"

    async def handle_confirmation(self) -> str:
        """§13.2's WHATSAPP step, including the failure branch.

        The failure branch is the point. Claiming a message was sent when it was
        not is a small lie that destroys the trust the whole call was building,
        and it is the default behaviour of any implementation that speaks the
        line before checking the result.
        """
        self.flow.confirmed = True
        self.flow.to(OutboundState.WHATSAPP)

        sent = False
        if self.send_whatsapp is not None:
            try:
                sent = await self.send_whatsapp()
            except Exception as exc:
                log.warning("outbound.whatsapp_failed", error=type(exc).__name__)
                sent = False

        if sent:
            self.flow.whatsapp_sent = True
            return "मैंने ऑफ़र की पूरी जानकारी आपके व्हाट्सऐप पर भेज दी है।"

        # Read it aloud instead, and raise a ticket so somebody follows up.
        self.flow.spoke_offer_aloud = True
        if self.create_ticket is not None:
            await self.create_ticket("whatsapp_dispatch_failed")
        return "व्हाट्सऐप पर भेजने में दिक़्क़त आ रही है, तो मैं आपको यहीं बता देता हूँ। " + self.offer.render()


__all__ = (
    "DISCLOSURE_PROTECT_MS",
    "MAX_OBJECTION_LOOPS",
    "MAX_PITCH_WORDS",
    "TRANSITIONS",
    "WHATSAPP_DISPATCH_S",
    "Offer",
    "OutboundAgent",
    "OutboundFlow",
    "OutboundState",
    "Reply",
    "classify_reply",
)
