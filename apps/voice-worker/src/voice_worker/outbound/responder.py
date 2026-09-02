"""The outbound script, driven from inside the live conversation pipeline (§13.2).

The pipeline that runs every call -- recogniser, turn detection, barge-in,
sentence-by-sentence synthesis, latency accounting -- asks one object for the
words: a ``Responder`` that turns the farmer's transcript into the agent's
reply. On an inbound call that is the LLM agent. On an outbound call it is
this class, which walks a fixed script and never improvises.

The script has four stages. Each farmer turn moves it forward or ends the call:

::

    VERIFY ──yes──▶ PERMISSION ──yes──▶ PITCHED ──1 / yes──▶ confirm, close
      │ wrong person   │ no               │ 2 / a question ─▶ answer, ask again
      ▼                ▼                  │ no ─────────────▶ accept it, close
    close           close                 │ 9 / "मत करना" ──▶ remove, close

Three things are deliberate:

**A "no" is accepted the first time.** §13.2 permits two objection loops. This
script uses none: the farmer said no, the agent thanks them and ends the call.
A helpline that argues is a helpline farmers stop answering.

**Questions go to the knowledge agent, then the script resumes.** Press two, or
say anything that is not a yes, a no or an opt-out, and the same agent that
answers the inbound helpline answers -- from the catalogue and the knowledge
base, never inventing a price or a dose -- before the interest question is
asked again. Capped, so a farmer who wants a long conversation is handed the
closing rather than kept on a promotional call.

**Opt-out is written before it is confirmed.** The suppression callback is
awaited before the confirmation line is yielded, so the call cannot end with
the farmer told they were removed while the write is still pending.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from uaagro_domain.enums import CallOutcome, ContactStatus, InterestLevel

from .conversation import Reply, classify_reply
from .script import WHATSAPP_SENT, OutboundScript

log = structlog.get_logger(__name__)

#: How the pipeline hands a keypress to the script: as a transcript the
#: recogniser could never produce.
DTMF_MARKER = re.compile(r"^\[dtmf (\d|\*|#)\]$")

#: After this many answered questions the interest check is asked one last
#: time and the call closes on the answer.
MAX_QUESTIONS = 3

WRONG_PERSON_LINE = "ठीक है, मैं फिर कभी फ़ोन करूँगा। धन्यवाद, नमस्ते जी।"
NO_TIME_LINE = "जी, कोई बात नहीं। मैं फिर कभी फ़ोन करूँगा।"
DECLINED_LINE = "जी, कोई बात नहीं।"
REPROMPT_LINE = "क्या आप यह लेना चाहेंगे? एक दबाइए, या बस हाँ बोल दीजिए।"
QUESTIONS_UNAVAILABLE_LINE = "इसकी पूरी जानकारी के लिए हमारे केंद्र पर बात कर लीजिए।"
WHATSAPP_FAILED_LINE = "व्हाट्सऐप पर भेजने में दिक़्क़त आ रही है, तो मैं आपको यहीं बता देता हूँ।"


class Stage(StrEnum):
    VERIFY = "verify"
    PERMISSION = "permission"
    PITCHED = "pitched"
    CLOSED = "closed"


class Questions(Protocol):
    """The knowledge agent, as the script sees it."""

    def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]: ...


@dataclass(slots=True)
class ContactResult:
    """What the call decided about the contact, for the campaign record."""

    #: The panel's vocabulary: pressed_1, pressed_2, talked, opted_out,
    #: wrong_person. None until the farmer has said something.
    outcome: str | None = None
    dtmf: str | None = None
    interest: InterestLevel | None = None
    call_outcome: CallOutcome | None = None
    whatsapp_sent: bool = False

    @property
    def status(self) -> ContactStatus:
        if self.outcome == "opted_out":
            return ContactStatus.OPTED_OUT
        return ContactStatus.COMPLETED


def dtmf_of(transcript: str) -> str | None:
    """The digit a DTMF marker carries, or None for real speech."""
    match = DTMF_MARKER.match(transcript.strip())
    return match.group(1) if match else None


class OutboundResponder:
    """Walks the script one farmer turn at a time."""

    #: Tells the pipeline to hand keypresses to :meth:`respond`.
    accepts_dtmf = True

    def __init__(
        self,
        *,
        script: OutboundScript,
        farmer_name: str | None,
        centre_name: str | None,
        questions: Questions | None = None,
        suppress: Callable[[], Awaitable[None]] | None = None,
        send_whatsapp: Callable[[], Awaitable[bool]] | None = None,
        create_ticket: Callable[[str], Awaitable[str]] | None = None,
        max_questions: int = MAX_QUESTIONS,
    ) -> None:
        self.script = script
        self.farmer_name = farmer_name
        self.centre_name = centre_name
        self.questions = questions
        self.suppress = suppress
        self.send_whatsapp = send_whatsapp
        self.create_ticket = create_ticket
        self.max_questions = max_questions

        self.stage = Stage.VERIFY
        self.result = ContactResult()
        self.call_over = False
        self._questions_asked = 0
        self._unclear = 0

    # -- the pipeline's view ------------------------------------------------ #

    def opening(self) -> str:
        """Spoken before the farmer says anything: disclosure and identity."""
        return self.script.rendered_opening(name=self.farmer_name)

    async def respond(self, transcript: str, *, language: str) -> AsyncIterator[str]:
        if self.call_over:
            return
        dtmf = dtmf_of(transcript)
        text = "" if dtmf else transcript
        if dtmf is not None:
            self.result.dtmf = dtmf
        reply = classify_reply(text, dtmf=dtmf)

        match self.stage:
            case Stage.VERIFY:
                async for line in self._verify(reply):
                    yield line
            case Stage.PERMISSION:
                async for line in self._permission(reply):
                    yield line
            case Stage.PITCHED:
                async for line in self._pitched(reply, text=text, dtmf=dtmf, language=language):
                    yield line
            case Stage.CLOSED:
                return

    # -- stages -------------------------------------------------------------- #

    async def _verify(self, reply: Reply) -> AsyncIterator[str]:
        if reply is Reply.OPT_OUT:
            async for line in self._opt_out():
                yield line
            return
        if reply in (Reply.THIRD_PARTY, Reply.NEGATIVE):
            # §13.2: never pitch to a third party. No message, no offer, a
            # polite exit and a note to try again.
            self._finish("wrong_person", InterestLevel.WRONG_PERSON, CallOutcome.NOT_REACHED)
            yield WRONG_PERSON_LINE
            return
        self.stage = Stage.PERMISSION
        yield self.script.ask_time

    async def _permission(self, reply: Reply) -> AsyncIterator[str]:
        if reply is Reply.OPT_OUT:
            async for line in self._opt_out():
                yield line
            return
        if reply is Reply.NEGATIVE:
            self._finish("talked", InterestLevel.NOT_INTERESTED, CallOutcome.OFFER_DECLINED)
            yield NO_TIME_LINE
            yield self.script.closing
            return
        self.stage = Stage.PITCHED
        yield self._render(self.script.message)
        yield self._render(self.script.then_ask)

    async def _pitched(
        self, reply: Reply, *, text: str, dtmf: str | None, language: str
    ) -> AsyncIterator[str]:
        if reply is Reply.OPT_OUT:
            async for line in self._opt_out():
                yield line
            return

        if reply is Reply.AFFIRMATIVE:
            async for line in self._confirm():
                yield line
            return

        wants_details = dtmf == "2"
        asked_something = dtmf is None and reply is Reply.UNCLEAR and bool(text.strip())
        if wants_details or asked_something:
            if wants_details:
                self.result.outcome = "pressed_2"
            async for line in self._answer(text, language=language):
                yield line
            return

        if reply is Reply.NEGATIVE:
            self._finish("talked", InterestLevel.NOT_INTERESTED, CallOutcome.OFFER_DECLINED)
            yield DECLINED_LINE
            yield self.script.closing
            return

        # Silence or noise. One more chance, then the closing: a farmer who
        # cannot be heard should not be held on a promotional call.
        self._unclear += 1
        if self._unclear <= 1:
            yield REPROMPT_LINE
            return
        self._finish("talked", InterestLevel.NO_RESPONSE, CallOutcome.OFFER_DECLINED)
        yield self.script.closing

    # -- branches ------------------------------------------------------------ #

    async def _answer(self, text: str, *, language: str) -> AsyncIterator[str]:
        """Answer a question from the knowledge agent, then ask again."""
        self._questions_asked += 1
        if self.questions is None:
            yield QUESTIONS_UNAVAILABLE_LINE
        elif text.strip():
            try:
                async for chunk in self.questions.respond(text, language=language):
                    yield chunk
            except Exception as exc:
                # The knowledge agent failing must not end the campaign call
                # mid-sentence. Say so and carry on with the script.
                log.warning("outbound.question_failed", error=type(exc).__name__)
                yield QUESTIONS_UNAVAILABLE_LINE
        else:
            # Press two with nothing asked: repeat the message, which is what
            # "details" means before a question has been put.
            yield self._render(self.script.message)

        if self._questions_asked >= self.max_questions:
            # Enough. Ask once more; the next turn closes either way.
            self._unclear = 1
        yield REPROMPT_LINE

    async def _confirm(self) -> AsyncIterator[str]:
        """§13.2's confirmation, including the honest failure branch.

        Three shapes, and the words follow what actually happened: no WhatsApp
        configured for this campaign says nothing about WhatsApp; a message
        that went out says so; a message that failed is read aloud instead and
        a ticket makes sure a person follows up. Never "मैंने भेज दिया है"
        when nothing was sent.
        """
        self._finish("pressed_1", InterestLevel.INTERESTED, CallOutcome.OFFER_ACCEPTED)
        yield self._render(self.script.on_press_1)

        if self.send_whatsapp is not None:
            sent = False
            try:
                sent = await self.send_whatsapp()
            except Exception as exc:
                log.warning("outbound.whatsapp_failed", error=type(exc).__name__)
            self.result.whatsapp_sent = sent
            if sent:
                yield WHATSAPP_SENT
            else:
                yield WHATSAPP_FAILED_LINE
                yield self._render(self.script.message)
                if self.create_ticket is not None:
                    try:
                        await self.create_ticket("whatsapp_dispatch_failed")
                    except Exception as exc:
                        log.warning("outbound.ticket_failed", error=type(exc).__name__)
        yield self.script.closing

    async def _opt_out(self) -> AsyncIterator[str]:
        """Immediate, unconditional, and written before it is spoken."""
        if self.suppress is not None:
            await self.suppress()
        self._finish("opted_out", InterestLevel.NOT_INTERESTED, CallOutcome.OPTED_OUT)
        log.info("outbound.opted_out")
        yield self.script.opt_out
        yield self.script.closing

    # -- helpers ------------------------------------------------------------- #

    def _render(self, text: str) -> str:
        return self.script.render(text, name=self.farmer_name, centre=self.centre_name)

    def _finish(self, outcome: str, interest: InterestLevel, call_outcome: CallOutcome) -> None:
        # A press-two that ends in a yes is a yes; a press-two that ends in a no
        # keeps "pressed_2", which is what the panel counts as "wanted details".
        if not (outcome == "talked" and self.result.outcome == "pressed_2"):
            self.result.outcome = outcome
        self.result.interest = interest
        self.result.call_outcome = call_outcome
        self.stage = Stage.CLOSED
        self.call_over = True


__all__ = (
    "DTMF_MARKER",
    "MAX_QUESTIONS",
    "ContactResult",
    "OutboundResponder",
    "Questions",
    "Stage",
    "dtmf_of",
)
