"""The panel's window on the media path: try a question, hear a line, place a test call.

Every endpoint here runs the same code a live call runs -- the retriever,
the agent, the normaliser, the synthesiser -- so what the operator sees in
the panel is what a farmer gets on the phone. They are internal: reachable
only with the token the API holds, and closed outright when no token is
configured, because a worker on a reachable port with them open would be a
free vendor account for whoever found it.
"""

from __future__ import annotations

import contextlib
import hmac
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from uaagro_db.engine import incall_session
from uaagro_domain.enums import FlowType
from uaagro_domain.phone import normalise_msisdn
from uaagro_domain.settings import get_defaults, get_settings

from ..adapters.factory import build_speech_stack
from ..adapters.llm.gateway import build_gateway
from ..adapters.telephony.control import build_adapter
from ..knowledge.retrieval import HybridRetriever
from ..runtime import audio as audio_utils
from ..runtime.assembly import Caller, build_agent, load_agent_settings
from ..text.speech import text_for_speech
from ..tools import build_registry

if TYPE_CHECKING:
    from ..main import WorkerState

log = structlog.get_logger(__name__)

#: Hindi at a helpline pace runs about this many words a second.
WORDS_PER_SECOND = 2.3


def panel_routes(state: WorkerState) -> APIRouter:
    """The router, bound to the process state it reads the retriever and cache from."""
    router = APIRouter(prefix="/internal")

    def allowed(request: Request) -> bool:
        """The shared secret between the API and this worker."""
        expected = get_settings().internal_api_token
        if not expected:
            return False
        presented = request.headers.get("x-internal-token")
        return presented is not None and hmac.compare_digest(presented, expected)

    def forbidden() -> JSONResponse:
        return JSONResponse({"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN)

    @router.post("/knowledge/search")
    async def knowledge_search(request: Request) -> JSONResponse:
        """What retrieval finds for a question, and optionally what the agent would say.

        The answer is generated only when asked, because it is a model call
        the operator pays for.
        """
        if not allowed(request):
            return forbidden()
        body = await request.json()
        question = str(body.get("question") or "").strip()
        language = str(body.get("language") or "hi")
        if not question:
            return JSONResponse({"error": "question is empty"}, status_code=422)
        # Which side of the panel is asking. The answer differs, because a
        # document can be marked for one direction, and an operator testing
        # the offer script should be shown what an offer call would find.
        direction = str(body.get("direction") or "inbound")
        retriever = state.retriever or HybridRetriever()

        started = time.perf_counter()
        async with incall_session() as db:
            found = await retriever.search(db, question, language=language, scope=direction)
        retrieval_ms = round((time.perf_counter() - started) * 1000)

        payload: dict[str, Any] = {
            "passages": [
                {
                    "documentTitle": chunk.document_title,
                    "section": chunk.section_path or None,
                    "snippet": chunk.content[:600],
                    "score": round(chunk.score, 3),
                    "containsDose": chunk.contains_dose,
                }
                for chunk in found.chunks
            ],
            "retrievalMs": retrieval_ms,
            "degraded": found.degraded,
            "degradedReason": found.degraded_reason,
            "answer": None,
        }
        if body.get("answer"):
            payload["answer"] = await _answer_for_panel(state, question, language, direction)
        return JSONResponse(payload)

    @router.post("/speech/preview")
    async def speech_preview(request: Request) -> JSONResponse:
        """How a line will be spoken, without spending a vendor call.

        The normaliser that runs before every synthesised sentence (§5.3) is
        applied to the operator's text, and each token that changed is
        reported -- "₹50" became "पचास रुपये" -- so the panel can show what
        the farmer will hear.
        """
        if not allowed(request):
            return forbidden()
        body = await request.json()
        text = " ".join(str(body.get("text") or "").split())
        language = str(body.get("language") or get_defaults().default_language)
        spoken = text_for_speech(text, language=language) if text else ""
        return JSONResponse(
            {
                "spoken": spoken,
                "words": len(spoken.split()),
                "seconds": round(len(spoken.split()) / WORDS_PER_SECOND, 1),
                "substitutions": substitutions(text, spoken),
            }
        )

    @router.post("/test-call")
    async def test_call(request: Request) -> JSONResponse:
        """Place a call to the operator with a draft script (§15.1).

        The number typed in the panel is the approved destination for this
        one call -- an operator ringing their own phone -- and the draft's id
        rides in the custom field so the media path speaks that version
        rather than the published one.
        """
        if not allowed(request):
            return forbidden()
        body = await request.json()
        phone = str(body.get("phone") or "").strip()
        config_id = str(body.get("configId") or "").strip()
        if not phone or not config_id:
            return JSONResponse({"error": "phone and configId are required"}, status_code=422)

        settings = get_settings()
        try:
            destination = normalise_msisdn(phone).e164
            # Twilio deployments have one number and it is set as the
            # promotional caller ID; asking for a separate transactional one
            # would refuse a test call on an account otherwise ready to place it.
            caller_id = (
                settings.outbound_cli_transactional
                or settings.twilio_from_number
                or settings.require("outbound_cli_promotional", needed_for="placing a test call")
            )
            adapter = build_adapter(settings, approved=frozenset({destination}))
            sid = await adapter.originate(
                to=destination,
                from_=caller_id,
                callback_url=f"{settings.public_base_url}/ws/voice",
                custom_field=f"test:{config_id}",
            )
        except Exception as exc:
            log.warning("worker.test_call_failed", error=type(exc).__name__)
            return JSONResponse(
                {
                    "error": getattr(exc, "message", str(exc)),
                    "remedy": getattr(exc, "remedy", None),
                    "code": "telephony_unconfigured",
                },
                status_code=503,
            )
        return JSONResponse({"callSid": sid})

    @router.post("/speech")
    async def speech(request: Request) -> Response:
        """Render one line in the configured voice, for the script preview.

        Returns a WAV so a browser can play it directly. Goes through the same
        normaliser and the same cache as a live call, so what the operator
        hears is what the farmer will.
        """
        if not allowed(request):
            return forbidden()
        body = await request.json()
        text = str(body.get("text") or "").strip()
        language = str(body.get("language") or get_defaults().default_language)
        if not text:
            return JSONResponse({"error": "text is empty"}, status_code=422)

        stack = build_speech_stack(language, get_settings(), get_defaults())
        try:
            spoken = text_for_speech(text, language=stack.tts_config.language)
            pcm = await state.audio_cache.get(spoken, stack.tts_config, provider=stack.tts.provider)
            if pcm is None:
                pcm = await stack.tts.synthesise_all(spoken, stack.tts_config)
                if pcm:
                    await state.audio_cache.put(
                        spoken, stack.tts_config, pcm, provider=stack.tts.provider
                    )
        finally:
            with contextlib.suppress(Exception):
                await stack.tts.close()
        if not pcm:
            return JSONResponse({"error": "the voice returned no audio"}, status_code=502)
        return Response(
            content=audio_utils.write_wav(pcm),
            media_type="audio/wav",
            headers={"x-spoken-text": spoken.encode("ascii", "backslashreplace").decode("ascii")},
        )

    return router


async def _answer_for_panel(
    state: WorkerState, question: str, language: str, direction: str
) -> dict[str, Any]:
    """Run one agent turn the way a call would, and time it."""
    settings = get_settings()
    defaults = get_defaults()
    registry = state.registry or build_registry(lexicon=state.lexicon)
    organization_id = await state.organization()
    if organization_id is None:
        return {"text": None, "totalMs": None, "note": "No organisation is seeded."}
    started = time.perf_counter()
    async with incall_session() as db:
        try:
            agent_settings = await load_agent_settings(
                db, organization_id=organization_id, flow_type=FlowType.INBOUND
            )
        except Exception as exc:
            return {"text": None, "totalMs": None, "note": str(getattr(exc, "message", exc))}
    agent = build_agent(
        registry=registry,
        persona=agent_settings.system_prompt,
        gateway=build_gateway(settings),
        caller=Caller(language=language or defaults.default_language),
        call_id=uuid.uuid4(),
        direction=direction,
    )
    try:
        result = await agent.handle(question)
    except Exception as exc:
        log.warning("worker.panel_answer_failed", error=type(exc).__name__)
        return {
            "text": None,
            "totalMs": None,
            "note": f"The model did not answer ({type(exc).__name__}).",
        }
    note = None
    if result.escalation is not None and getattr(result.escalation, "should_transfer", False):
        note = "On a call this would be handed to a person."
    return {
        "text": result.text,
        "totalMs": round((time.perf_counter() - started) * 1000),
        "note": note,
    }


def substitutions(original: str, spoken: str) -> list[dict[str, str]]:
    """Tokens the normaliser rewrote, paired with what replaced them.

    A token-level comparison rather than a diff library: the normaliser only
    ever expands a token into more words, so walking both lists and pairing
    an unchanged token with itself is enough to attribute every change.
    """
    pairs: list[dict[str, str]] = []
    before = original.split()
    after = spoken.split()
    j = 0
    for i, token in enumerate(before):
        if j < len(after) and after[j] == token:
            j += 1
            continue
        # Find where the original resumes; everything in between replaced it.
        resume = None
        for k in range(i + 1, len(before)):
            if before[k] in after[j:]:
                resume = after.index(before[k], j)
                break
        replacement = " ".join(after[j:resume] if resume is not None else after[j:])
        if replacement and replacement != token:
            pairs.append({"from": token, "to": replacement})
        j = resume if resume is not None else len(after)
    return pairs


__all__ = ("panel_routes", "substitutions")
