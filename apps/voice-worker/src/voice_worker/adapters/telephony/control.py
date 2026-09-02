"""Call control: dial, transfer, hang up (§4.3, §12.3, §17).

The control plane, separate from the media path because the two have different
lifetimes -- a serializer lives for one call, an adapter for the process -- and
because they fail differently. A control-API outage delays a transfer; a media
outage ends a conversation.

**The destination is never caller-supplied.** §17 is explicit, and this is the
one place in the system where getting it wrong costs money rather than trust:
a number that reached :meth:`originate` from a transcript, a retrieved document
or a tool argument is how toll fraud happens. :func:`assert_approved_destination`
is called on every dial and every transfer, and it takes the allowed set as an
argument rather than reading it from anywhere -- so a caller cannot widen it.

**A warm transfer is not a redirect.** §12.3 requires the manager to hear six
seconds of context before the bridge, because a blind transfer makes the farmer
repeat everything and is the reason people hate IVRs. The whisper is passed
through the provider's own mechanism where one exists and is spoken by the agent
into the manager's leg where it does not.
"""

from __future__ import annotations

import hashlib
import hmac
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

import structlog

from uaagro_domain.enums import TelephonyProvider
from uaagro_domain.errors import (
    AuthorizationError,
    InvalidPhoneNumberError,
    MissingCredentialError,
    VendorError,
)
from uaagro_domain.phone import normalise_msisdn, redact
from uaagro_domain.settings import Settings

from .base import TelephonyAdapter

log = structlog.get_logger(__name__)

#: §12.3-6: how long a manager's phone rings before the chain moves on.
RING_TIMEOUT_S = 20


def assert_approved_destination(number: str, approved: frozenset[str]) -> str:
    """Refuse to dial anything not on the operator-approved list (§17).

    Args:
        number: The destination, in any common Indian format.
        approved: Normalised numbers from ``centres``, ``users`` or an approved
            campaign list. Passed in rather than looked up here, so this
            function cannot be talked into widening its own allowlist.

    Raises:
        AuthorizationError: For anything not in ``approved``, **including a
            number that will not parse**. One exception type, on purpose: a
            caller catching a refusal must not have to also handle a validation
            error to be safe, and a malformed destination reaching a dial API is
            the same failure as an unapproved one. Not a warning and not a log
            line -- continuing past this is never right.
    """
    try:
        normalised = str(normalise_msisdn(number).e164)
    except InvalidPhoneNumberError:
        log.error("telephony.unparseable_destination")
        raise AuthorizationError(
            action="dial an unparseable destination",
            resource="telephony",
        ) from None

    if normalised not in approved:
        # The number is redacted even here. §23-6 has no exception for error
        # paths, and an audit log full of unapproved numbers is itself a leak.
        log.error("telephony.unapproved_destination", destination=redact(normalised))
        raise AuthorizationError(
            action="dial an unapproved destination",
            resource="telephony",
        )
    return normalised


@dataclass
class HttpTelephonyAdapter(TelephonyAdapter):
    """Base for the REST control APIs.

    The three providers differ in URL shape, auth scheme and parameter names,
    and in nothing else that matters -- so the HTTP mechanics live here and each
    subclass supplies its own request.
    """

    settings: Settings
    approved_destinations: frozenset[str] = frozenset()
    _client: Any = field(default=None, repr=False)

    @abstractmethod
    def _auth(self) -> tuple[str, str]:
        """``(user, password)`` for HTTP basic auth."""

    @abstractmethod
    def _base_url(self) -> str: ...

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            user, password = self._auth()
            self._client = httpx.AsyncClient(
                base_url=self._base_url(),
                auth=(user, password),
                timeout=httpx.Timeout(10.0, connect=3.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        response = await client.post(path, data=data)
        if response.status_code >= 400:
            # The body is not logged: provider error responses echo the
            # parameters, which include the destination number.
            log.error(
                "telephony.control_failed",
                provider=self.provider.value,
                status=response.status_code,
            )
            raise VendorError(
                f"{self.provider.value} returned {response.status_code}.",
                remedy="Check the telephony dashboard and the credentials in "
                "the environment. The call falls back to a callback ticket.",
                context={"provider": self.provider.value},
            )
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}


@dataclass
class ExotelAdapter(HttpTelephonyAdapter):
    """Exotel's call-control REST API (§4.3)."""

    provider: TelephonyProvider = TelephonyProvider.EXOTEL

    def _auth(self) -> tuple[str, str]:
        return (
            self.settings.require("exotel_api_key", needed_for="Exotel call control"),
            self.settings.require("exotel_api_token", needed_for="Exotel call control"),
        )

    def _base_url(self) -> str:
        sid = self.settings.require("exotel_sid", needed_for="Exotel call control")
        return f"https://{self.settings.exotel_subdomain}/v1/Accounts/{sid}"

    async def originate(
        self, *, to: str, from_: str, callback_url: str, custom_field: str | None = None
    ) -> str:
        destination = assert_approved_destination(to, self.approved_destinations)
        request: dict[str, Any] = {
            "From": destination,
            "CallerId": from_,
            "Url": callback_url,
            "TimeLimit": 900,
            "TimeOut": RING_TIMEOUT_S,
        }
        if custom_field:
            # Echoed back as `custom_parameters` in the Voicebot start frame.
            request["CustomField"] = custom_field
        body = await self._post("/Calls/connect.json", request)
        call = body.get("Call", {})
        sid = str(call.get("Sid", "")) if isinstance(call, dict) else ""
        log.info("telephony.originated", provider=self.provider.value, sid=sid)
        return sid

    async def transfer(self, *, call_sid: str, to: str, whisper_text: str | None = None) -> None:
        destination = assert_approved_destination(to, self.approved_destinations)
        await self._post(
            f"/Calls/{call_sid}.json",
            {
                "Url": _whisper_url(self.settings, whisper_text),
                "Transfer": destination,
                "TimeOut": RING_TIMEOUT_S,
            },
        )
        # The whisper text is not logged: §12.3-4 puts the farmer's name and
        # what they want into it, which makes it caller PII.
        log.info(
            "telephony.transferred",
            provider=self.provider.value,
            target=redact(destination),
            whispered=whisper_text is not None,
        )

    async def hangup(self, *, call_sid: str) -> None:
        await self._post(f"/Calls/{call_sid}.json", {"Status": "completed"})


@dataclass
class PlivoAdapter(HttpTelephonyAdapter):
    """Plivo, the §21 portability proof.

    Written to demonstrate that swapping providers is one adapter file. It is
    not exercised against a live account -- no Plivo credentials exist for this
    project -- so its request shapes are from the published API and are marked
    unverified in ``docs/ARCHITECTURE.md`` rather than presented as tested.
    """

    provider: TelephonyProvider = TelephonyProvider.PLIVO

    def _auth(self) -> tuple[str, str]:
        return (
            self.settings.require("plivo_auth_id", needed_for="Plivo call control"),
            self.settings.require("plivo_auth_token", needed_for="Plivo call control"),
        )

    def _base_url(self) -> str:
        auth_id = self.settings.require("plivo_auth_id", needed_for="Plivo call control")
        return f"https://api.plivo.com/v1/Account/{auth_id}"

    async def originate(
        self, *, to: str, from_: str, callback_url: str, custom_field: str | None = None
    ) -> str:
        destination = assert_approved_destination(to, self.approved_destinations)
        # Plivo carries no custom field on the call; the worker matches the
        # dialled number against the contact being dialled instead.
        del custom_field
        body = await self._post(
            "/Call/",
            {
                "to": destination,
                "from": from_,
                "answer_url": callback_url,
                "answer_method": "POST",
                "ring_timeout": RING_TIMEOUT_S,
            },
        )
        return str(body.get("request_uuid", ""))

    async def transfer(self, *, call_sid: str, to: str, whisper_text: str | None = None) -> None:
        assert_approved_destination(to, self.approved_destinations)
        await self._post(
            f"/Call/{call_sid}/",
            {"legs": "aleg", "aleg_url": _whisper_url(self.settings, whisper_text)},
        )

    async def hangup(self, *, call_sid: str) -> None:
        client = self._ensure_client()
        await client.delete(f"/Call/{call_sid}/")


@dataclass
class TwilioAdapter(HttpTelephonyAdapter):
    """Twilio Programmable Voice.

    Different from the other two in one way that shapes everything here:
    Twilio does not take "connect this call to a websocket" as a dial
    parameter. It takes TwiML -- a document describing what the call should
    do -- and `<Connect><Stream>` is the verb that opens a two-way media
    stream. So `originate` sends the document inline rather than pointing
    Twilio at a webhook, which means there is no TwiML endpoint to host,
    nothing to keep in sync with the dialer, and no way for a request that
    reaches that endpoint out of order to connect a call to the wrong farmer.

    The stream URL carries the same token the inbound socket checks, and the
    campaign contact rides along as a `<Parameter>`, which Twilio echoes in
    the start frame's `customParameters` -- where `runtime.direction` reads
    it.
    """

    provider: TelephonyProvider = TelephonyProvider.TWILIO

    def _auth(self) -> tuple[str, str]:
        return (
            self.settings.require("twilio_api_key_sid", needed_for="Twilio call control"),
            self.settings.require("twilio_api_key_secret", needed_for="Twilio call control"),
        )

    def _base_url(self) -> str:
        account = self.settings.require("twilio_account_sid", needed_for="Twilio call control")
        return f"https://api.twilio.com/2010-04-01/Accounts/{account}"

    async def originate(
        self, *, to: str, from_: str, callback_url: str, custom_field: str | None = None
    ) -> str:
        destination = assert_approved_destination(to, self.approved_destinations)
        # `callback_url` is the answer webhook the other providers are given.
        # Twilio is pointed at this worker's own TwiML route instead, so that
        # one address serves every call and the dial request carries nothing
        # but the three parameters a trial account is allowed to send.
        del callback_url
        body = await self._post(
            "/Calls.json",
            {
                "To": destination,
                "From": from_,
                "Url": twiml_url(self.settings, custom_field),
                "Timeout": RING_TIMEOUT_S,
                # A call that somehow outlives the conversation is stopped by
                # the provider rather than billed until somebody notices.
                "TimeLimit": 900,
            },
        )
        sid = str(body.get("sid", ""))
        log.info("telephony.originated", provider=self.provider.value, sid=sid)
        return sid

    async def transfer(self, *, call_sid: str, to: str, whisper_text: str | None = None) -> None:
        destination = assert_approved_destination(to, self.approved_destinations)
        # The whisper is not sent. Twilio plays one by fetching TwiML when the
        # manager answers (`<Number url="...">`), and nothing serves that
        # document yet -- see the note on `_whisper_url`. Dialling without it
        # connects the farmer, which is the part that matters; the manager
        # simply gets no preamble.
        if whisper_text is not None:
            log.info("telephony.whisper_skipped", provider=self.provider.value)
        await self._post(
            f"/Calls/{call_sid}.json",
            {
                "Twiml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f"<Response><Dial timeout={quoteattr(str(RING_TIMEOUT_S))}>"
                    f"<Number>{escape(destination)}</Number></Dial></Response>"
                )
            },
        )
        log.info(
            "telephony.transferred",
            provider=self.provider.value,
            target=redact(destination),
            whispered=False,
        )

    async def hangup(self, *, call_sid: str) -> None:
        await self._post(f"/Calls/{call_sid}.json", {"Status": "completed"})


def stream_twiml(settings: Settings, reference: str | None) -> str:
    """`<Connect><Stream>` pointed at this worker's voice socket.

    Served by the worker's `/telephony/twiml` route rather than sent inline on
    the dial request: a Twilio trial account refuses a request carrying
    `Twiml`, and one address that every call fetches is also one place to fix
    when the stream host changes.

    ``reference`` is the campaign contact or the test configuration --
    `contact:<uuid>`, `test:<uuid>` -- which Twilio returns in the start
    frame's `customParameters`, and which is how a call finds its contact card.
    """
    parameter = ""
    if reference:
        parameter = f"<Parameter name={quoteattr('contact')} value={quoteattr(reference)}/>"
    stream = f"<Stream url={quoteattr(websocket_url(settings))}>{parameter}</Stream>"
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect>{stream}</Connect></Response>'


def twiml_url(settings: Settings, reference: str | None) -> str:
    """Where Twilio fetches that document, carrying the socket's shared secret.

    The reference is an identifier of ours, never a phone number: §23-6 does
    not stop applying because the value is in a query string.
    """
    base = settings.public_base_url.rstrip("/")
    query: list[str] = []
    if settings.telephony_ws_token:
        query.append(f"token={quote(settings.telephony_ws_token, safe='')}")
    if reference:
        query.append(f"contact={quote(reference, safe='')}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return f"{base}/telephony/twiml{suffix}"


def websocket_url(settings: Settings) -> str:
    """Where the provider should open the media stream.

    Derived from ``PUBLIC_BASE_URL`` because that is the one address which is
    already required to be the externally-resolvable one; a second variable
    for the same host is a second thing to get wrong. http becomes ws and
    https becomes wss, so a deployment that has TLS gets a secure stream
    without saying so twice.

    The token is the same one the socket checks on every connection. It is in
    a query string, which is where Twilio accepts it -- and it is a shared
    secret for one endpoint, not a credential for a person, so it is not
    subject to §17's rule about identifiers in URLs.
    """
    base = settings.public_base_url.rstrip("/")
    scheme = "wss" if base.startswith("https://") else "ws"
    host = base.split("://", 1)[-1]
    url = f"{scheme}://{host}/ws/voice"
    token = settings.telephony_ws_token
    return f"{url}?token={quote(token, safe='')}" if token else url


def _whisper_url(settings: Settings, whisper_text: str | None) -> str:
    """Where the provider fetches the whisper instruction from.

    A URL, not the text: the whisper carries the farmer's name and what they
    want (§12.3-4), and putting that in a query string would write caller PII
    into the provider's request logs -- which §17 forbids putting in a URL at
    all. The worker serves the text from a short-lived token instead.
    """
    base = settings.public_base_url.rstrip("/")
    if whisper_text is None:
        return f"{base}/telephony/bridge"
    token = hashlib.sha256(whisper_text.encode("utf-8")).hexdigest()[:32]
    return f"{base}/telephony/whisper/{token}"


# --------------------------------------------------------------------------- #
# Webhook authentication (§17)
# --------------------------------------------------------------------------- #


def verify_signature(
    *, body: bytes, signature: str, secret: str, algorithm: str = "sha256"
) -> bool:
    """Whether a provider webhook is authentic.

    Compared with :func:`hmac.compare_digest`, not ``==``. A byte-by-byte
    comparison leaks the position of the first mismatch through timing, which
    over enough requests is enough to forge a signature -- and a forged webhook
    on this system can mark a call answered, or a message delivered, or a
    consent granted.

    A malformed or absent signature is ``False``, never an exception: a webhook
    endpoint that raises on bad input is a denial-of-service target.
    """
    if not signature or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, algorithm).hexdigest()
    candidate = signature.strip().lower()
    if candidate.startswith(f"{algorithm}="):
        candidate = candidate.split("=", 1)[1]
    return hmac.compare_digest(digest, candidate)


def build_adapter(settings: Settings, approved: frozenset[str] = frozenset()) -> TelephonyAdapter:
    """The adapter for the configured provider (§4.1: a config change)."""
    provider = TelephonyProvider(settings.telephony_provider)
    if provider is TelephonyProvider.EXOTEL:
        return ExotelAdapter(settings=settings, approved_destinations=approved)
    if provider is TelephonyProvider.PLIVO:
        return PlivoAdapter(settings=settings, approved_destinations=approved)
    if provider is TelephonyProvider.TWILIO:
        return TwilioAdapter(settings=settings, approved_destinations=approved)
    raise MissingCredentialError(
        variable="TELEPHONY_PROVIDER",
        needed_for=f"call control -- {provider.value} has no adapter",
    )


__all__ = (
    "RING_TIMEOUT_S",
    "ExotelAdapter",
    "HttpTelephonyAdapter",
    "PlivoAdapter",
    "TwilioAdapter",
    "assert_approved_destination",
    "build_adapter",
    "stream_twiml",
    "twiml_url",
    "verify_signature",
    "websocket_url",
)
