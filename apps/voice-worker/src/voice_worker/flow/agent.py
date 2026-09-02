"""The DISCOVER ⇄ RESOLVE turn handler (§11.1).

Everything Phase 4 built, in the order one turn actually needs it:

1. **Safety** (§16.1). Before anything else, and it returns without reaching the
   model at all. A cached script and a transfer, never generated text.
2. **Intent** (§11.2). Rules first, so a request for a human works when the
   gateway does not.
3. **Escalation** (§12). Evaluated *before* generation, because §12.1's
   immediate triggers mean "no further agent turns" -- generating an answer and
   then discarding it would spend a turn's latency proving the transfer was
   right.
4. **Tools** (§6.3). At most two, in parallel where independent.
5. **Context** (§6.2), then **generation** (§6.1), then **validation** (§16.3).

The ordering is the design. Each step can end the turn, and the ones that can
end it most consequentially come first -- so the cheapest path through this
function is also the most urgent one.

:class:`Agent` satisfies the ``Responder`` protocol the Phase 2 pipeline already
takes, which is why the timing machinery needed no changes: the seam was put
there for this.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from uaagro_domain.enums import Intent, TransferReason

from ..text.speech import SentenceBuffer
from ..tools.base import ToolContext, ToolRegistry, ToolResult
from .context import CallerContext, ContextBuilder, ConversationMemory, DynamicHint
from .escalation import EscalationDecision, EscalationEngine, TurnSignals
from .intents import classify_by_rule, reconcile
from .safety import SafetyVerdict, combine
from .safety import detect as detect_safety
from .state import CallFlow, CallState
from .validator import (
    FALLBACK_SCRIPT_HI,
    MAX_WORDS,
    MAX_WORDS_DOSAGE,
    OutputValidator,
    ValidationOutcome,
)

log = structlog.get_logger(__name__)

#: §11.4: the hold phrase spoken while a slow tool or model is retried. Cached,
#: because the moment it is needed is the moment synthesis is least reliable.
HOLD_PHRASE_KEY = "hold.one_moment.hi"
HOLD_SCRIPT_HI = "जी, एक क्षण रुकिए, मैं देख रहा हूँ।"


@dataclass
class TurnResult:
    """What one turn decided, for the call record and the tests."""

    text: str
    intent: Intent = Intent.UNKNOWN
    tool_results: list[ToolResult] = field(default_factory=list)
    escalation: EscalationDecision | None = None
    safety: SafetyVerdict | None = None
    validation: ValidationOutcome | None = None
    #: True when the turn produced a cached phrase rather than generated text.
    cached: bool = False
    #: Set when the turn must not be followed by another agent turn (§12.1).
    ends_agent_turns: bool = False

    @property
    def grounded_tools(self) -> list[ToolResult]:
        return [r for r in self.tool_results if r.ok]

    def as_dict(self) -> dict[str, Any]:
        """Persisted to ``call_turns`` (§10)."""
        payload: dict[str, Any] = {
            "intent": self.intent.value,
            "tool_results": [r.to_dict() for r in self.tool_results],
        }
        if self.escalation is not None and self.escalation.escalate:
            payload["escalation"] = {
                "reason": self.escalation.reason.value if self.escalation.reason else None,
                "urgency": self.escalation.urgency.value,
                "immediate": self.escalation.immediate,
            }
        if self.safety is not None and self.safety.triggered:
            payload["safety"] = {"source": self.safety.source}
        if self.validation is not None and not self.validation.ok:
            payload["validation_failed"] = list(self.validation.rules)
        return payload


class IntentClassifier:
    """The optional LLM half of §11.2. ``None`` when the gateway is down."""

    async def classify(
        self, transcript: str, *, history: Sequence[str] = ()
    ) -> tuple[Intent, float] | None:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _Prepared:
    """What one turn decided before any generation.

    Passed between :meth:`Agent._prepare` and :meth:`Agent._finish` so the
    buffered and streaming paths cannot disagree about the intent, the tool
    results or the escalation they were working from.
    """

    transcript: str
    intent: Intent
    results: list[ToolResult]
    decision: EscalationDecision
    asr_confidence: float | None


@dataclass
class Agent:
    """One call's DISCOVER ⇄ RESOLVE handler."""

    registry: ToolRegistry
    context_builder: ContextBuilder
    gateway: Any = None
    validator: OutputValidator = field(default_factory=OutputValidator)
    escalation: EscalationEngine = field(default_factory=EscalationEngine)
    classifier: IntentClassifier | None = None
    memory: ConversationMemory = field(default_factory=ConversationMemory)
    caller: CallerContext | None = None
    hint: DynamicHint | None = None
    flow: CallFlow = field(default_factory=CallFlow)
    tool_context: ToolContext = field(
        default_factory=lambda: ToolContext(call_id="unknown")
    )
    #: The most recent finished turn. `respond` streams text and cannot return
    #: a result, so the escalation decision and validation outcome are left
    #: here for whatever needs them after the words have gone out.
    last_turn: TurnResult | None = field(default=None, repr=False)

    async def handle(
        self, transcript: str, *, asr_confidence: float | None = None
    ) -> TurnResult:
        """Run one turn end to end, answering only when the whole reply is in.

        The streaming counterpart is :meth:`respond`. Both share
        :meth:`_prepare` and :meth:`_finish` rather than each running their own
        copy of the safety check, the classifier and the escalation passes -- a
        safety path that fired on one and not the other would be the worst bug
        this file could carry.
        """
        fixed, prepared = await self._prepare(transcript, asr_confidence=asr_confidence)
        if fixed is not None:
            return fixed
        assert prepared is not None

        text, validation, exhausted = await self._generate(
            transcript, intent=prepared.intent, results=prepared.results
        )
        return self._finish(prepared, text, validation, exhausted)

    # -- the two phases both paths share ------------------------------------ #

    async def _prepare(
        self, transcript: str, *, asr_confidence: float | None = None
    ) -> tuple[TurnResult | None, _Prepared | None]:
        """Everything decided before the model is asked for a word.

        Returns either a finished turn -- safety, or an immediate transfer,
        both of which are fixed scripts with nothing to generate -- or the
        material a generation needs.
        """
        self.memory.add("user", transcript)

        safety = await self._check_safety(transcript)
        if safety.triggered:
            # §16.1: abandon all other logic. No intent classification, no tool
            # call, no generation -- the script is fixed and the transfer is
            # already decided.
            return self._safety_turn(safety), None

        intent = await self._classify(transcript)

        signals = TurnSignals(
            text=transcript, intent=intent, asr_confidence=asr_confidence
        )
        repeated = self.flow.intent_attempts.get(intent, 0) >= 1
        decision = self.escalation.evaluate(signals, repeated_intent=repeated)

        if decision.escalate and decision.immediate:
            # §12.1: no further agent turns. Generating first and discarding
            # would spend the turn's latency proving the transfer was right.
            return self._immediate_escalation_turn(intent, decision), None

        results = await self._run_tools(intent, transcript)

        if not decision.escalate:
            # A tool that came back restricted or empty changes the picture, so
            # §12.2 is re-checked with what the tools actually said rather than
            # with what the transcript suggested.
            #
            # Only when the first pass did not already escalate, and with the
            # rolling signals withheld. Feeding confidence and sentiment in
            # twice per turn would make §12.2's "rolling mean over three turns"
            # a mean over one and a half, which is not the threshold the spec
            # sets and is not one anybody chose.
            decision = self.escalation.evaluate(
                TurnSignals(
                    text=transcript,
                    intent=intent,
                    restricted_product=_any_restricted(results),
                    no_data_available=_nothing_found(results),
                ),
                repeated_intent=repeated,
            )

        return None, _Prepared(
            transcript=transcript,
            intent=intent,
            results=list(results),
            decision=decision,
            asr_confidence=asr_confidence,
        )

    def _finish(
        self,
        prepared: _Prepared,
        text: str,
        validation: ValidationOutcome,
        exhausted: bool,
    ) -> TurnResult:
        """Record the turn and assemble its result."""
        decision = prepared.decision
        if exhausted and not decision.escalate:
            # §16.3: two validation failures end in a cached safe phrase **and**
            # an escalation. Speaking the phrase without escalating leaves the
            # caller with an apology and nothing behind it, which is the dead
            # end §11.4 forbids.
            decision = EscalationDecision(
                escalate=True,
                reason=TransferReason.MISSING_DATA,
                detail="generation could not be made safe after one retry (§16.3)",
            )

        self.memory.add("assistant", text)
        resolved = validation.ok and bool(prepared.results) and not decision.escalate
        confident = (
            prepared.asr_confidence is None or prepared.asr_confidence >= 0.55
        )
        self.flow.record_turn(prepared.intent, confident=confident, resolved=resolved)

        self.last_turn = TurnResult(
            text=text,
            intent=prepared.intent,
            tool_results=prepared.results,
            escalation=decision if decision.escalate else None,
            validation=validation,
            cached=exhausted,
            ends_agent_turns=exhausted or (decision.escalate and decision.immediate),
        )
        return self.last_turn

    # -- steps -------------------------------------------------------------- #

    async def _check_safety(self, transcript: str) -> SafetyVerdict:
        keyword = detect_safety(transcript)
        if keyword.triggered:
            # The classifier is not consulted. It could only agree, and asking
            # would add a round trip to the one path that must not wait.
            return keyword
        return combine(keyword, classifier_says_emergency=None)

    async def _classify(self, transcript: str) -> Intent:
        rule = classify_by_rule(transcript)
        if rule.bypasses_llm or self.classifier is None:
            return rule.intent
        try:
            answer = await self.classifier.classify(transcript)
        except Exception as exc:
            # A classifier failure must not end a turn. The rules layer already
            # has an answer, and degrading to it is the whole reason it exists.
            log.warning("agent.classifier_failed", error=type(exc).__name__)
            return rule.intent
        if answer is None:
            return rule.intent
        from .intents import IntentResult

        model_intent, confidence = answer
        return reconcile(rule, IntentResult(model_intent, confidence, "classifier")).intent

    async def _run_tools(self, intent: Intent, transcript: str) -> list[ToolResult]:
        """Call the tools §11.2 routes this intent to. At most two (§6.3)."""
        from .intents import TOOL_HINTS

        planned = _plan(intent, transcript, TOOL_HINTS.get(intent, ()))
        if not planned:
            return []
        return await self.registry.execute_many(planned, self.tool_context)

    async def _generate(
        self, transcript: str, *, intent: Intent, results: Sequence[ToolResult]
    ) -> tuple[str, ValidationOutcome, bool]:
        """Build context, stream, validate, retry once (§6.2, §6.1, §16.3)."""
        if self.gateway is None:
            # No gateway wired: say so rather than inventing an answer. §1 N1
            # makes silence-with-escalation the correct degraded behaviour.
            return FALLBACK_SCRIPT_HI, ValidationOutcome(ok=False), True

        trimmed = [r.to_dict() for r in results]
        is_dosage = intent in (Intent.DOSAGE_QUERY, Intent.CROP_RECOMMENDATION)

        feedback: str | None = None
        outcome = ValidationOutcome(ok=False)
        text = ""

        for _ in range(2):
            # The retry is told what was wrong. A bare "try again" regenerates
            # the same class of output, and only one retry is available.
            message = (
                transcript if feedback is None else f"{transcript}\n\n[सुधार: {feedback}]"
            )
            built = self.context_builder.build(
                transcript=message,
                caller=self.caller,
                hint=self.hint,
                memory=self.memory,
                tool_results=trimmed,
                intent=intent,
            )
            text = await self._collect(built, max_tokens=_token_budget(is_dosage))
            outcome = self.validator.validate(
                text, tool_results=trimmed, is_dosage=is_dosage
            )
            if outcome.ok:
                return text, outcome, False
            feedback = "; ".join(str(v) for v in outcome.violations)

        # §16.3: two failures end in a cached phrase and an escalation.
        return FALLBACK_SCRIPT_HI, outcome, True

    async def _collect(self, built: Any, *, max_tokens: int | None = None) -> str:
        pieces: list[str] = []
        try:
            async for piece, _rung in self.gateway.stream(
                system_blocks=built.system_blocks,
                user_message=built.user_message,
                cacheable_prefix=built.cacheable_prefix,
                max_tokens=max_tokens,
            ):
                pieces.append(piece)
        except Exception as exc:
            # §11.4: an LLM failure becomes a hold phrase and an escalation,
            # never an exception in the audio loop.
            log.warning("agent.generation_failed", error=type(exc).__name__)
            return ""
        return "".join(pieces).strip()

    # -- terminal turns ------------------------------------------------------ #

    def _safety_turn(self, verdict: SafetyVerdict) -> TurnResult:
        from .safety import SAFETY_SCRIPT_HI

        self.memory.add("assistant", SAFETY_SCRIPT_HI)
        if self.flow.can(CallState.ESCALATE):
            self.flow.to(CallState.ESCALATE, reason="safety_emergency")
        return TurnResult(
            text=SAFETY_SCRIPT_HI,
            intent=Intent.SAFETY_EMERGENCY,
            safety=verdict,
            escalation=self.escalation.evaluate(
                TurnSignals(intent=Intent.SAFETY_EMERGENCY)
            ),
            cached=True,
            ends_agent_turns=True,
        )

    def _immediate_escalation_turn(
        self, intent: Intent, decision: EscalationDecision
    ) -> TurnResult:
        # §12.3-1: the caller is told before anything happens. The line itself
        # comes from the transfer tool, which knows who they are being joined
        # to; this is the placeholder the loop replaces.
        text = "जी, मैं आपको हमारे साथी से जोड़ रहा हूँ। एक क्षण रुकिए।"
        self.memory.add("assistant", text)
        if self.flow.can(CallState.ESCALATE):
            self.flow.to(
                CallState.ESCALATE,
                reason=decision.reason.value if decision.reason else None,
            )
        return TurnResult(
            text=text,
            intent=intent,
            escalation=decision,
            cached=True,
            ends_agent_turns=True,
        )

    # -- Responder protocol -------------------------------------------------- #

    async def respond(
        self, transcript: str, *, language: str = "hi-IN"
    ) -> AsyncIterator[str]:
        """The ``Responder`` seam the pipeline takes, one sentence at a time.

        Waiting for the complete answer before speaking any of it costs the
        whole generation. Measured through the configured endpoint: a first
        token at ~1.7 s and a full 35-word Hindi answer several seconds after
        that, none of which reaches the caller until the last token lands. The
        first sentence is usually finished long before.

        **Why this is safe, when the previous note here said it was not.**
        §16.3's validator is local, stateless and its rules are *monotonic* --
        register, guarantees, filler and grounding all fail on the offending
        sentence and cannot be redeemed by a later one. Crucially the tool
        results it grounds against are complete before generation starts, so a
        sentence can be grounded the moment it is complete. Only the word cap
        is cumulative, and that is checked cumulatively.

        Two rules keep the parts that are genuinely not decomposable intact:

        **A dosage answer is never streamed.** §16.2 requires the dose, the
        pre-harvest interval and the precaution to be spoken together. Speaking
        the dose and then stopping because a later sentence failed is the one
        outcome worse than being slow.

        **The first sentence is spoken only after it validates**, which keeps
        §16.3's retry available in the common failure case: nothing has been
        said yet, so a bad opening is regenerated with feedback exactly as
        before. Once speech has started it cannot be recalled, so a later
        failure stops there and hands over -- a partial true answer plus a
        handover, rather than a retraction.
        """
        fixed, prepared = await self._prepare(transcript)
        if fixed is not None:
            yield fixed.text
            return
        assert prepared is not None

        is_dosage = prepared.intent in (Intent.DOSAGE_QUERY, Intent.CROP_RECOMMENDATION)
        if is_dosage or self.gateway is None:
            text, validation, exhausted = await self._generate(
                transcript, intent=prepared.intent, results=prepared.results
            )
            self._finish(prepared, text, validation, exhausted)
            yield text
            return

        async for sentence in self._stream_validated(prepared):
            yield sentence

    async def _stream_validated(self, prepared: _Prepared) -> AsyncIterator[str]:
        """Stream the answer, releasing each sentence once it passes §16.3.

        One loop over one source of sentences, deliberately. An earlier version
        handled the streamed sentences and the buffer's final flush separately,
        and the two copies disagreed: a violation in the closing sentence --
        which is exactly where a model tends to append an invented price -- was
        caught and then silently dropped, so the turn ended with the caller
        having heard half an answer and nothing else.
        """
        trimmed = [r.to_dict() for r in prepared.results]
        built = self.context_builder.build(
            transcript=prepared.transcript,
            caller=self.caller,
            hint=self.hint,
            memory=self.memory,
            tool_results=trimmed,
            intent=prepared.intent,
        )

        spoken: list[str] = []
        outcome = ValidationOutcome(ok=True)

        try:
            async for sentence in self._generate_sentences(built):
                # Validated against everything said so far, not in isolation:
                # §11.3's word cap is the one rule that is cumulative, and a
                # per-sentence check would let five short sentences past it.
                candidate = " ".join([*spoken, sentence]).strip()
                outcome = self.validator.validate(
                    candidate, tool_results=trimmed, is_dosage=False
                )

                if outcome.ok:
                    spoken.append(sentence)
                    yield sentence
                    continue

                if not spoken:
                    # Nothing has reached the caller, so §16.3's retry is
                    # available in full: regenerate with the violation as
                    # feedback, exactly as the buffered path does.
                    async for whole in self._retry_whole(prepared):
                        yield whole
                    return

                # Already speaking. Those words cannot be recalled, so this
                # stops rather than contradicting itself, and §11.4 hands the
                # caller to a person instead of leaving them with half an
                # answer.
                log.info(
                    "agent.stream_truncated",
                    rules=[v.rule for v in outcome.violations],
                    sentences=len(spoken),
                )
                self._finish(prepared, " ".join(spoken), outcome, True)
                yield FALLBACK_SCRIPT_HI
                return
        except Exception as exc:
            log.warning("agent.stream_failed", error=type(exc).__name__)
            if not spoken:
                async for whole in self._retry_whole(prepared):
                    yield whole
                return
            self._finish(prepared, " ".join(spoken), outcome, True)
            return

        if not spoken:
            # The stream ended without producing a single usable sentence.
            self._finish(prepared, FALLBACK_SCRIPT_HI, ValidationOutcome(ok=False), True)
            yield FALLBACK_SCRIPT_HI
            return

        self._finish(prepared, " ".join(spoken), outcome, False)

    async def _generate_sentences(self, built: Any) -> AsyncIterator[str]:
        """The model's output, regrouped into whole sentences.

        The trailing flush is here rather than at the call site so that the
        last sentence of an answer travels the same path as every other one.
        """
        buffer = SentenceBuffer()
        async for piece, _rung in self.gateway.stream(
            system_blocks=built.system_blocks,
            user_message=built.user_message,
            cacheable_prefix=built.cacheable_prefix,
            max_tokens=_token_budget(False),
            temperature=None,
        ):
            for sentence in buffer.add(piece):
                yield sentence
        for sentence in buffer.flush():
            yield sentence

    async def _retry_whole(self, prepared: _Prepared) -> AsyncIterator[str]:
        """§16.3's retry, for when nothing has been spoken yet."""
        text, validation, exhausted = await self._generate(
            prepared.transcript, intent=prepared.intent, results=prepared.results
        )
        self._finish(prepared, text, validation, exhausted)
        yield text


#: The first tool §11.2 routes each intent to, and the argument it takes from
#: the transcript. Only the ones where the choice is genuinely not in doubt --
#: the model still picks in the general case, and a plan that guessed at
#: ``recommend_for_crop``'s crop argument from raw speech would be inventing an
#: argument, which §6.3 refuses.
_OPENING_TOOL: dict[Intent, tuple[str, str, int]] = {
    Intent.PRICE_ENQUIRY: ("search_products", "query", 200),
    Intent.PRODUCT_AVAILABILITY: ("search_products", "query", 200),
    Intent.PRODUCT_COMPOSITION: ("search_products", "query", 200),
    Intent.CROP_RECOMMENDATION: ("search_products", "query", 200),
    Intent.PROBLEM_DIAGNOSIS: ("search_knowledge", "query", 300),
    Intent.SCHEME_QUERY: ("search_knowledge", "query", 300),
    Intent.DOSAGE_QUERY: ("search_products", "query", 200),
}


def _token_budget(is_dosage: bool) -> int:
    """How many output tokens this turn may use.

    §11.3 caps an ordinary answer at ~35 words and a dosage answer at ~60, and
    the validator enforces both. This is the same limit applied at the other
    end, where it costs nothing to enforce: a model handed room for sixty words
    tends to use it, and every word past the cap is a word the farmer waits for
    and the validator then rejects -- turning one generation into two inside
    the caller's turn.

    The multiplier is deliberately generous against the estimate. Devanagari
    tokenises worse than the ~1.8 tokens per word the estimator assumes, and
    the failure mode of a cap set too low is far worse than one set too high:
    a truncated dosage answer loses the pre-harvest interval and the precaution
    that §16.2 requires be spoken, mid-sentence, with no signal that anything
    is missing.
    """
    words = MAX_WORDS_DOSAGE if is_dosage else MAX_WORDS
    return int(words * TOKENS_PER_WORD_CEILING)


#: Tokens per Hindi word, rounded up hard.
#:
#: The context estimator uses ~1.8 for budgeting *input*, where being 10% wrong
#: costs a little cache efficiency. Here being wrong low truncates an answer
#: mid-word, so this is the pessimistic end of what Devanagari costs.
TOKENS_PER_WORD_CEILING = 3.5


def _plan(
    intent: Intent, transcript: str, hints: Sequence[str]
) -> list[tuple[str, Mapping[str, Any]]]:
    """Which tools to open with, with arguments taken from the transcript.

    Deliberately small, and it opens rather than completes: the first call is
    a lookup whose arguments are the caller's own words, and everything after
    it -- a SKU, a recommendation id -- needs a value that only the first
    result can supply. Guessing those from raw speech would be inventing tool
    arguments, which §6.3 makes a refusal.

    An unmapped intent returns nothing rather than a default lookup. Calling
    ``search_products`` on "कल बारिश होगी क्या" would spend a tool budget to
    learn nothing and put an irrelevant result in front of the model.
    """
    route = _OPENING_TOOL.get(intent)
    if route is not None:
        name, argument, limit = route
        return [(name, {argument: transcript[:limit]})]
    if "search_knowledge" in hints:
        return [("search_knowledge", {"query": transcript[:300]})]
    return []


def _any_restricted(results: Sequence[ToolResult]) -> bool:
    return any(r.ok and r.data.get("restricted") for r in results)


def _nothing_found(results: Sequence[ToolResult]) -> bool:
    """Every tool ran and none of them found anything.

    Not "no tools ran": a turn that needed no lookup is not a turn with no
    answer, and treating it as one would escalate every greeting.
    """
    if not results:
        return False
    return all(not r.ok or not r.data or r.data.get("answered") is False for r in results)


__all__ = ("HOLD_PHRASE_KEY", "HOLD_SCRIPT_HI", "Agent", "IntentClassifier", "TurnResult")
