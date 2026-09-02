"""``search_knowledge`` -- Tier 2 and Tier 3 behind one tool (§6.3, §9).

The tool tries the curated answer cache first and hybrid retrieval second, which
is the order §9 puts them in: roughly half of an agri helpline's inbound volume
is the same forty questions, and a cache hit answers in under 100 ms without an
LLM call or a TTS round trip.

Two refusals are built in, and both are the tool declining to be useful in a
situation where being useful is the failure:

**Prices and stock never come from here.** §9 sends them to Tier 1 on every
turn, because a nearest-neighbour search over documents will confidently return
last month's price. The cache schema forbids storing one; this tool additionally
refuses the *question*, and names the tool that can answer it.

**Doses never come from prose.** §23-3 forbids the agent generating a dose and
§16.2 makes an unapproved one unservable. A retrieved chunk that carries a
quantity is returned as context but flagged, and the result tells the agent to
call ``recommend_for_crop`` instead of reading the number out.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import AnswerCache

from ..knowledge.retrieval import HybridRetriever
from ..text.script import whole_word
from .base import Tool, ToolContext
from .session import tool_session

log = structlog.get_logger(__name__)

#: Questions that must not be answered from documents (§9 Tier 1).
#:
#: Built with :func:`whole_word`, not ``\b``. Most of these Hindi keywords end
#: in a matra -- ``दाम`` is fine, ``कितनी`` and ``मात्रा`` are not -- and ``\b``
#: matches none of the latter. A guard written the obvious way would pass the
#: English price questions and let the Hindi ones straight through to
#: retrieval, which is exactly the population this system is built for.
_PRICE_QUESTION = re.compile(
    whole_word(
        "price|cost|rate|stock|available|कीमत|दाम|भाव|रेट|मूल्य|स्टॉक|उपलब्ध"
    )
    + r"|"
    + whole_word("कितने|कितना")
    + r"\s*"
    + whole_word("का|की|के"),
    re.IGNORECASE,
)
_DOSE_QUESTION = re.compile(
    whole_word("dose|dosage|कितना|कितनी|कितने|मात्रा|डोज़|खुराक")
    + r"|how\s+much|"
    + whole_word("प्रति")
    + r"\s*"
    + whole_word("एकड़|बीघा|लीटर"),
    re.IGNORECASE,
)


#: Matches no chunk, and forces the first ONNX inference off the call path.
WARMUP_TERM = "zzzzwarmup"

class SearchKnowledge(Tool):
    """Advisory prose, FAQs and scheme explanations."""

    name = "search_knowledge"
    description = (
        "Search advisory notes, product labels, government scheme explanations "
        "and FAQs. Use for explanations and guidance, never for price, stock or "
        "dosage -- those have their own tools. Every result carries a source; "
        "cite it, and do not state anything the results do not say."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "minLength": 2,
                "maxLength": 300,
                "description": "The farmer's question, in their own words.",
            },
            "language": {
                "type": "string",
                "maxLength": 12,
                "description": (
                    "Caller's language code, e.g. hi-IN. English is always searched too."
                ),
            },
            "crop": {"type": "string", "maxLength": 60},
        },
        "required": ["query"],
    }

    def __init__(self, retriever: HybridRetriever | None = None) -> None:
        self._retriever = retriever or HybridRetriever()

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"query": WARMUP_TERM}

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        query = str(args["query"]).strip()
        language = str(args.get("language") or context.language)
        crop = args.get("crop")

        if _PRICE_QUESTION.search(query):
            # Not an error: the agent is told where the answer actually lives.
            return {
                "answered": False,
                "reason": "price_and_stock_are_not_retrievable",
                "use_instead": "check_availability",
                "note": (
                    "Price and stock change hourly and are never served from documents."
                ),
                "results": [],
            }

        async with tool_session() as session:
            cached = await _answer_cache(session, query, language)
            if cached is not None:
                log.info("knowledge.cache_hit", intent_key=cached["intent_key"])
                return cached

            result = await self._retriever.search(
                session, query, language=language, crop=str(crop) if crop else None
            )

        if not result.chunks:
            return {
                "answered": False,
                "reason": "nothing_relevant_found",
                "results": [],
                "degraded": result.degraded,
                # §11.4: no dead ends. The agent says it does not know and
                # offers a human rather than filling the silence.
                "next_step": "offer_escalation",
            }

        dosage_present = any(c.contains_dose for c in result.chunks)
        payload: dict[str, Any] = {
            "answered": True,
            "tier": 2,
            # §16.3: the caller passes this to the model as untrusted reference
            # material, wrapped in delimiters, never as instruction.
            "context": result.as_context(),
            "results": [
                {
                    "chunk_id": chunk.chunk_id,
                    "title": chunk.document_title,
                    "section": chunk.section_path,
                    "source": chunk.source,
                    "language": chunk.language,
                }
                for chunk in result.chunks
            ],
            # §9: recorded on the turn. An uncitable claim is a test failure.
            "chunk_ids": list(result.chunk_ids),
            "languages_searched": list(result.languages_searched),
        }
        if result.degraded:
            payload["degraded"] = True
            payload["degraded_reason"] = result.degraded_reason

        if result.weak:
            # Nothing corroborated the vector index's pick. A vector search
            # always returns its nearest neighbours, however far away they are,
            # so "nothing relevant exists" and "here are four passages" look
            # identical from the outside. Saying so is the honest option: the
            # measurements behind it are in `retrieval._is_weak`, and they show
            # no score -- bi-encoder or cross-encoder -- separates the two cases
            # on this corpus. So the judgement is passed to the model with the
            # uncertainty attached rather than silently resolved here.
            payload["weak_match"] = True
            payload["must_verify_relevance"] = True
            payload["instruction"] = (
                "These passages are the closest available, not necessarily "
                "relevant. If they do not actually answer the question, say you "
                "do not know and offer to connect the caller to a person."
            )
            payload["fallback_step"] = "offer_escalation"

        if dosage_present or _DOSE_QUESTION.search(query):
            # §23-3. The chunk stays -- it is legitimate context for explaining
            # *how* to apply something -- but the number in it is not an answer.
            payload["dose_must_come_from_tool"] = True
            payload["use_instead"] = "recommend_for_crop"

        return payload


async def _answer_cache(
    session: AsyncSession, query: str, language: str
) -> dict[str, Any] | None:
    """Tier 3: the curated head of the distribution (§9).

    Matched on the stored question variants rather than on similarity. A cache
    that answered on approximate match would answer the wrong forty questions
    at exactly the confidence that makes the mistake invisible.
    """
    normalised = _normalise(query)
    base = language.split("-")[0].lower()

    rows = (
        await session.scalars(
            select(AnswerCache).where(
                AnswerCache.is_active.is_(True),
                AnswerCache.language.startswith(base),
            )
        )
    ).all()

    for row in rows:
        variants = {_normalise(v) for v in row.question_variants}
        if normalised in variants:
            return {
                "answered": True,
                "tier": 3,
                "intent_key": row.intent_key,
                "answer": row.answer_text,
                # Pre-synthesised audio, so a hit can skip TTS entirely (§9).
                "audio_key": row.audio_object_key,
                "results": [],
                "chunk_ids": [],
            }
    return None


def _normalise(text: str) -> str:
    """Casefold and collapse whitespace and the Devanagari danda.

    ``।`` is a sentence terminator, not a word character, and a variant stored
    with one must still match a transcript without one.
    """
    return re.sub(r"[\s।?!.,]+", " ", text.strip().casefold()).strip()


__all__ = ("SearchKnowledge",)
