"""Tools: the only route to facts (§6.3).

:func:`build_registry` assembles every tool once at worker start. It is not a
per-call factory: the lexicon it needs holds the whole catalogue's spoken
vocabulary, and §7.5 requires that warm before the first call rather than
rebuilt on each one.

Which of these a given agent may actually call is decided separately, by the
``tool_allowlist`` on its ``agent_configs`` row (§10). Registering a tool here
makes it *implementable*; the config decides whether the outbound agent can
raise a ticket or the inbound one can start a campaign.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..knowledge.retrieval import HybridRetriever
from ..text.lexicon import Lexicon
from .advisory import CalculateDose, RecommendForCrop
from .base import (
    HARD_TIMEOUT_MS,
    MAX_CALLS_PER_TURN,
    P95_BUDGET_MS,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ValidationFailure,
)
from .catalogue import CheckAvailability, FindNearestCentre, GetProductDetails, SearchProducts
from .escalation import (
    NullTransferPort,
    QueueingWhatsAppPort,
    SendWhatsApp,
    TransferPort,
    TransferToHuman,
    WhatsAppPort,
)
from .farmer import CreateTicket, GetOrderStatus, LogIntent, LookupFarmer
from .knowledge import SearchKnowledge

#: Every tool name in §6.3. Asserted against the registry in the tests so a
#: tool that is specified but never built cannot pass unnoticed -- the model
#: would be offered a capability that fails on first use.
TOOL_NAMES: tuple[str, ...] = (
    "lookup_farmer",
    "search_products",
    "check_availability",
    "get_product_details",
    "recommend_for_crop",
    "calculate_dose",
    "search_knowledge",
    "find_nearest_centre",
    "get_order_status",
    "create_ticket",
    "send_whatsapp",
    "transfer_to_human",
    "log_intent",
)


def build_registry(
    *,
    lexicon: Lexicon | None = None,
    transfer_port: TransferPort | None = None,
    whatsapp_port: WhatsAppPort | None = None,
    retriever: object | None = None,
) -> ToolRegistry:
    """Assemble the tools for one worker process.

    ``retriever`` carries the loaded embedder. Passing None still registers
    ``search_knowledge``: retrieval degrades to BM25 and says so, which serves
    a caller better than a tool the model is never offered.
    """
    registry = ToolRegistry()
    tools: list[Tool] = [
        LookupFarmer(),
        SearchProducts(lexicon),
        CheckAvailability(),
        GetProductDetails(),
        RecommendForCrop(),
        CalculateDose(),
        FindNearestCentre(),
        GetOrderStatus(),
        CreateTicket(),
        SendWhatsApp(whatsapp_port),
        TransferToHuman(transfer_port),
        LogIntent(),
        SearchKnowledge(retriever if isinstance(retriever, HybridRetriever) else None),
    ]
    for tool in tools:
        registry.register(tool)
    return registry


__all__: Sequence[str] = (
    "HARD_TIMEOUT_MS",
    "MAX_CALLS_PER_TURN",
    "P95_BUDGET_MS",
    "TOOL_NAMES",
    "CalculateDose",
    "CheckAvailability",
    "CreateTicket",
    "FindNearestCentre",
    "GetOrderStatus",
    "GetProductDetails",
    "LogIntent",
    "LookupFarmer",
    "NullTransferPort",
    "QueueingWhatsAppPort",
    "RecommendForCrop",
    "SearchKnowledge",
    "SearchProducts",
    "SendWhatsApp",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "TransferToHuman",
    "ValidationFailure",
    "build_registry",
)
