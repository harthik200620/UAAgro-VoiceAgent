"""WhatsApp messaging (§14).

Meta Cloud API behind a ``MessagingAdapter``, with a BSP implementation
selectable by config -- §14 notes a BSP is often faster to onboard in India, and
the choice should not be a rewrite.

The part of §14 that costs real money is the **category**. A marketing template
is roughly 7.5x a utility template in India -- ₹0.8631 against ₹0.115 at the
rates verified 31 August 2026 -- and the categories are not interchangeable.
A dosage card is utility: the farmer asked, it is service, and sending it as
marketing costs 7.5x for nothing. Sending a promotional offer as utility is the
error in the other direction and is a policy violation rather than an overspend.

So the category lives on the template registry rather than being passed at the
call site: it is a property of what the message *is*, not of who is sending it,
and a caller that could choose would eventually choose wrong.

**The free window is not free forever.** §14 says replies inside the 24-hour
customer-initiated service window cost nothing, and that is true at the time of
writing -- but Meta's free service window ends **1 October 2026**, about a month
from this build. :func:`estimate_cost` takes the date rather than assuming
today, so a cost projection for a campaign scheduled in October is right.
"""

from __future__ import annotations

import hashlib
import hmac
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog

from uaagro_domain.errors import VendorError
from uaagro_domain.settings import Settings

log = structlog.get_logger(__name__)


class TemplateCategory(StrEnum):
    """Meta's billing categories. The expensive distinction in §14."""

    MARKETING = "marketing"
    UTILITY = "utility"
    AUTHENTICATION = "authentication"
    #: Free-form reply inside an open service window.
    SERVICE = "service"


#: Verified 31 August 2026 against Meta's India price list.
RATES: dict[TemplateCategory, Decimal] = {
    TemplateCategory.MARKETING: Decimal("0.8631"),
    TemplateCategory.UTILITY: Decimal("0.1150"),
    TemplateCategory.AUTHENTICATION: Decimal("0.1250"),
    TemplateCategory.SERVICE: Decimal("0.0000"),
}

#: §14: a customer-initiated reply opens a 24-hour window in which service
#: messages are free.
SERVICE_WINDOW = timedelta(hours=24)

#: Meta's free service window ends on this date.
#:
#: Strongly corroborated across industry sources but **not** stated on Meta's
#: own published price list as of 31 August 2026, so it is recorded here as an
#: expected change with its provenance rather than as a documented fact. A cost
#: projection for a campaign after this date should be treated as a floor.
FREE_SERVICE_WINDOW_ENDS = date(2026, 10, 1)

#: Rate applied to service messages once the free window ends. Meta has not
#: published the India figure; utility is the conservative stand-in and is
#: marked as an estimate wherever it is shown.
POST_FREE_SERVICE_RATE = RATES[TemplateCategory.UTILITY]


@dataclass(frozen=True, slots=True)
class Template:
    """One approved template (§14).

    ``category`` is fixed here, not chosen by the caller. A dosage card is
    utility because of what it is; a caller free to pick would eventually send
    it as marketing and pay 7.5x for the privilege.
    """

    name: str
    category: TemplateCategory
    #: Meta's template id, assigned on approval. Absent means unapproved, and
    #: an unapproved template cannot be sent.
    template_id: str | None = None
    language: str = "hi"
    #: Named placeholders, in order. Checked before dispatch so a missing one
    #: fails locally rather than as a rejected send.
    parameters: tuple[str, ...] = ()

    @property
    def is_approved(self) -> bool:
        return bool(self.template_id)


#: §14's seven templates.
REGISTRY: dict[str, Template] = {
    t.name: t
    for t in (
        Template("offer_details", TemplateCategory.MARKETING,
                 parameters=("name", "offer", "saving", "valid_until", "centre")),
        Template("product_price_list", TemplateCategory.UTILITY,
                 parameters=("name", "products")),
        # Utility, not marketing. The farmer asked, it is service, and the
        # 7.5x difference is the single largest avoidable cost in §8.
        Template("dosage_instructions", TemplateCategory.UTILITY,
                 parameters=("crop", "product", "dose", "phi_days", "precaution")),
        Template("centre_location", TemplateCategory.UTILITY,
                 parameters=("centre", "address", "timings", "phone")),
        Template("callback_confirmation", TemplateCategory.UTILITY,
                 parameters=("name", "ticket_ref", "window")),
        Template("order_status_update", TemplateCategory.UTILITY,
                 parameters=("order_ref", "status", "eta")),
        Template("ticket_ack", TemplateCategory.UTILITY,
                 parameters=("ticket_ref", "subject")),
    )
}


def estimate_cost(
    template_name: str,
    *,
    count: int = 1,
    on: date | None = None,
    inside_service_window: bool = False,
) -> Decimal:
    """What sending this will cost (§8, §14).

    §13.1 requires the admin panel to show a projected cost before a campaign
    is approved, and the projection has to be right about the category or the
    number is off by 7.5x.

    Args:
        on: The date the send would happen. Not "today" by default in spirit --
            a campaign scheduled for October is priced under October's rules,
            and the free service window ends on 1 October 2026.
    """
    template = REGISTRY.get(template_name)
    if template is None:
        raise KeyError(f"no template named {template_name!r}")

    when = on or datetime.now(UTC).date()
    if inside_service_window:
        rate = (
            RATES[TemplateCategory.SERVICE]
            if when < FREE_SERVICE_WINDOW_ENDS
            else POST_FREE_SERVICE_RATE
        )
    else:
        rate = RATES[template.category]
    return (rate * count).quantize(Decimal("0.0001"))


class MessagingAdapter(ABC):
    """§14's provider seam."""

    @abstractmethod
    async def send_template(
        self,
        *,
        to: str,
        template: Template,
        parameters: Mapping[str, str],
        language: str,
    ) -> str:
        """Send one template. Returns the provider message id."""

    @abstractmethod
    async def send_service_reply(self, *, to: str, text: str) -> str:
        """Free-form reply inside an open 24-hour window."""


def validate_parameters(
    template: Template, parameters: Mapping[str, str]
) -> list[str]:
    """Missing or unknown placeholders.

    Checked before dispatch. Meta rejects a mismatched template at the API, so
    the alternative is discovering it as a failed send on a live campaign -- at
    which point the farmer has been told a message is coming.
    """
    problems: list[str] = []
    for name in template.parameters:
        if not parameters.get(name):
            problems.append(f"missing parameter {name!r}")
    for name in parameters:
        if name not in template.parameters:
            problems.append(f"unknown parameter {name!r}")
    return problems


@dataclass
class CloudApiAdapter(MessagingAdapter):
    """Meta Cloud API (§14's default)."""

    settings: Settings
    _client: Any = field(default=None, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            token = self.settings.require(
                "wa_access_token", needed_for="WhatsApp dispatch (§14)"
            )
            self._client = httpx.AsyncClient(
                base_url="https://graph.facebook.com/v21.0",
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(10.0, connect=3.0),
            )
        return self._client

    async def send_template(
        self,
        *,
        to: str,
        template: Template,
        parameters: Mapping[str, str],
        language: str,
    ) -> str:
        if not template.is_approved:
            # §14: templates are submitted for approval and stored with their
            # ids. Sending an unapproved one fails at Meta, after the farmer has
            # been told it is coming.
            raise VendorError(
                f"Template {template.name!r} has no approved template id.",
                remedy="Submit it in the WhatsApp Manager and record the id.",
                context={"template": template.name},
            )
        problems = validate_parameters(template, parameters)
        if problems:
            raise VendorError(
                f"Template {template.name!r}: {'; '.join(problems)}",
                remedy="Fix the parameters before dispatch.",
                context={"template": template.name},
            )

        phone_id = self.settings.require(
            "wa_phone_number_id", needed_for="WhatsApp dispatch (§14)"
        )
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template.name,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": parameters[name]}
                            for name in template.parameters
                        ],
                    }
                ],
            },
        }
        client = self._ensure_client()
        response = await client.post(f"/{phone_id}/messages", json=body)
        if response.status_code >= 400:
            # The body is not logged: Meta echoes the destination number.
            log.error("whatsapp.send_failed", status=response.status_code,
                      template=template.name)
            raise VendorError(
                f"WhatsApp returned {response.status_code}.",
                remedy="§13.2: the agent reads the offer aloud and raises a "
                "ticket. It never claims a message was sent.",
                context={"template": template.name},
            )
        payload = response.json()
        messages = payload.get("messages") or [{}]
        message_id = str(messages[0].get("id", ""))
        log.info(
            "whatsapp.sent",
            template=template.name,
            category=template.category.value,
            message_id=message_id,
        )
        return message_id

    async def send_service_reply(self, *, to: str, text: str) -> str:
        phone_id = self.settings.require(
            "wa_phone_number_id", needed_for="WhatsApp dispatch (§14)"
        )
        client = self._ensure_client()
        response = await client.post(
            f"/{phone_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )
        response.raise_for_status()
        payload = response.json()
        return str((payload.get("messages") or [{}])[0].get("id", ""))


def verify_webhook(*, body: bytes, signature: str, app_secret: str) -> bool:
    """Whether a Meta webhook is authentic (§17).

    Meta signs with ``sha256=<hex>`` in ``X-Hub-Signature-256``. Compared with
    :func:`hmac.compare_digest` -- a forged delivery receipt on this system can
    mark a message delivered that never arrived, which is what §13.2's "never
    claim a message was sent" depends on being true.
    """
    if not signature or not app_secret:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    candidate = signature.strip()
    if candidate.startswith("sha256="):
        candidate = candidate[7:]
    return hmac.compare_digest(expected, candidate.lower())


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A farmer's reply, from the webhook.

    §14: a reply opens the 24-hour service window, so it is routed into a
    ``tickets`` row for a human to answer inside it -- rather than being
    answered with a fresh billable template.
    """

    from_number: str
    text: str
    received_at: datetime
    message_id: str

    def window_closes_at(self) -> datetime:
        return self.received_at + SERVICE_WINDOW

    def window_is_open(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) < self.window_closes_at()


def parse_webhook(payload: Mapping[str, Any]) -> list[InboundMessage]:
    """Extract inbound messages from a Meta webhook body.

    Defensive throughout: the payload shape is deeply nested and Meta sends
    status callbacks through the same endpoint, so anything unrecognised yields
    nothing rather than raising. A webhook handler that raises on an unexpected
    shape is one Meta will eventually disable for failing.
    """
    messages: list[InboundMessage] = []
    for entry in _list(payload.get("entry")):
        for change in _list(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in _list(value.get("messages")):
                text = message.get("text")
                body = text.get("body") if isinstance(text, dict) else None
                if not isinstance(body, str):
                    continue
                try:
                    received = datetime.fromtimestamp(
                        int(message.get("timestamp", 0)), tz=UTC
                    )
                except (TypeError, ValueError):
                    received = datetime.now(UTC)
                messages.append(
                    InboundMessage(
                        from_number=str(message.get("from", "")),
                        text=body,
                        received_at=received,
                        message_id=str(message.get("id", "")),
                    )
                )
    return messages


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


__all__ = (
    "FREE_SERVICE_WINDOW_ENDS",
    "POST_FREE_SERVICE_RATE",
    "RATES",
    "REGISTRY",
    "SERVICE_WINDOW",
    "CloudApiAdapter",
    "InboundMessage",
    "MessagingAdapter",
    "Template",
    "TemplateCategory",
    "estimate_cost",
    "parse_webhook",
    "validate_parameters",
    "verify_webhook",
)
