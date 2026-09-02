"""Twilio: the TwiML that opens the media stream, and the frames that come back.

Twilio is the only provider that is told what to do rather than where to
connect, so the document `originate` sends is the whole integration. It is
asserted here as text, because a misplaced attribute is a call that rings, is
answered by nobody, and bills.

Nothing here reaches Twilio. The REST client is replaced with one that records
the request; what a real account does with these parameters is a question for
`docs/VERIFICATION.md`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from uaagro_domain.enums import CallDirection, TelephonyProvider
from uaagro_domain.errors import AuthorizationError
from uaagro_domain.settings import Settings
from voice_worker.adapters.telephony.control import TwilioAdapter, build_adapter, websocket_url
from voice_worker.adapters.telephony.mulaw_providers import TwilioSerializer
from voice_worker.runtime.direction import OurNumbers, classify_direction

FARMER = "+919876543210"
OURS = "+911140001234"


def settings(**overrides: Any) -> Settings:
    base = {
        "telephony_provider": TelephonyProvider.TWILIO,
        "twilio_account_sid": "AC" + "0" * 30,
        "twilio_api_key_sid": "SK" + "1" * 30,
        "twilio_api_key_secret": "secret-value",
        "telephony_ws_token": "ws-token",
        "public_base_url": "https://voice.example.com",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _Recorder:
    """Stands in for the REST client and keeps what it was asked to send."""

    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.body = body or {"sid": "CA" + "9" * 30}

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append((path, dict(data)))
        return _Response(self.body)


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def adapter(**overrides: Any) -> tuple[TwilioAdapter, _Recorder]:
    recorder = _Recorder()
    made = TwilioAdapter(settings=settings(**overrides), approved_destinations=frozenset({FARMER}))
    made._client = recorder
    return made, recorder


# --------------------------------------------------------------------------- #
# Where the media stream is opened
# --------------------------------------------------------------------------- #


def test_a_secure_origin_gives_a_secure_socket() -> None:
    assert websocket_url(settings()) == "wss://voice.example.com/ws/voice?token=ws-token"


def test_a_plain_origin_gives_a_plain_socket() -> None:
    """Local development runs without TLS; the scheme follows the origin."""
    url = websocket_url(settings(public_base_url="http://localhost:8080"))
    assert url == "ws://localhost:8080/ws/voice?token=ws-token"


def test_no_token_means_no_query_string() -> None:
    assert websocket_url(settings(telephony_ws_token=None)) == "wss://voice.example.com/ws/voice"


# --------------------------------------------------------------------------- #
# The TwiML
# --------------------------------------------------------------------------- #


async def test_the_call_is_told_to_connect_the_stream_to_this_worker() -> None:
    twilio, recorder = adapter()
    contact = f"contact:{uuid.uuid4()}"

    sid = await twilio.originate(
        to=FARMER, from_=OURS, callback_url="https://ignored.example", custom_field=contact
    )

    assert sid.startswith("CA")
    path, data = recorder.calls[0]
    assert path == "/Calls.json"
    assert data["To"] == FARMER
    assert data["From"] == OURS
    twiml = data["Twiml"]
    assert "<Connect>" in twiml and "<Stream " in twiml
    assert 'url="wss://voice.example.com/ws/voice?token=ws-token"' in twiml
    # The contact rides as a parameter, which is where the start frame carries
    # it back and where `runtime.direction` looks for it.
    assert f'<Parameter name="contact" value="{contact}"/>' in twiml


async def test_a_call_with_no_contact_carries_no_parameter() -> None:
    twilio, recorder = adapter()
    await twilio.originate(to=FARMER, from_=OURS, callback_url="", custom_field=None)
    assert "<Parameter" not in recorder.calls[0][1]["Twiml"]


async def test_the_stream_url_is_quoted_as_an_attribute() -> None:
    """A raw ampersand in an attribute is not well-formed XML, and Twilio
    rejects the document rather than the parameter."""
    twilio, recorder = adapter(telephony_ws_token="a&b")
    await twilio.originate(to=FARMER, from_=OURS, callback_url="", custom_field=None)
    twiml = recorder.calls[0][1]["Twiml"]
    assert "token=a%26b" in twiml
    assert "&&" not in twiml


async def test_an_unapproved_destination_is_never_dialled() -> None:
    twilio, recorder = adapter()
    with pytest.raises(AuthorizationError):
        await twilio.originate(to="+919999999999", from_=OURS, callback_url="", custom_field=None)
    assert recorder.calls == []


# --------------------------------------------------------------------------- #
# Transfer and hang-up
# --------------------------------------------------------------------------- #


async def test_a_transfer_redirects_the_live_call_to_the_manager() -> None:
    twilio, recorder = adapter()
    await twilio.transfer(call_sid="CA123", to=FARMER, whisper_text="context")
    path, data = recorder.calls[0]
    assert path == "/Calls/CA123.json"
    assert f"<Number>{FARMER}</Number>" in data["Twiml"]


async def test_hanging_up_completes_the_call() -> None:
    twilio, recorder = adapter()
    await twilio.hangup(call_sid="CA123")
    assert recorder.calls[0] == ("/Calls/CA123.json", {"Status": "completed"})


def test_the_factory_builds_a_twilio_adapter_for_a_twilio_deployment() -> None:
    built = build_adapter(settings(), frozenset({FARMER}))
    assert isinstance(built, TwilioAdapter)
    assert built.provider is TelephonyProvider.TWILIO


# --------------------------------------------------------------------------- #
# The frames that come back
# --------------------------------------------------------------------------- #


def start_frame(custom: dict[str, str] | None = None) -> str:
    start: dict[str, Any] = {
        "streamSid": "MZ" + "0" * 30,
        "callSid": "CA" + "9" * 30,
        "accountSid": "AC" + "0" * 30,
    }
    if custom is not None:
        start["customParameters"] = custom
    return json.dumps({"event": "start", "sequenceNumber": "1", "start": start})


def test_the_start_frame_is_understood() -> None:
    event = TwilioSerializer().decode(start_frame({"contact": "contact:abc"}))
    assert event.metadata is not None
    assert event.metadata.call_sid.startswith("CA")
    assert event.metadata.stream_sid.startswith("MZ")


def test_the_contact_parameter_makes_the_call_outbound() -> None:
    """Twilio nests custom parameters inside `start`, one level lower than
    Exotel. Reading only the top level greeted every campaign call as if the
    farmer had rung the helpline."""
    contact = uuid.uuid4()
    event = TwilioSerializer().decode(start_frame({"contact": f"contact:{contact}"}))
    assert event.metadata is not None

    decided = classify_direction(
        event.metadata, OurNumbers(inbound=frozenset(), outbound=frozenset())
    )
    assert decided.direction is CallDirection.OUTBOUND
    assert decided.contact_id == contact


def test_a_call_with_no_parameters_is_treated_as_inbound() -> None:
    event = TwilioSerializer().decode(start_frame(None))
    assert event.metadata is not None
    decided = classify_direction(
        event.metadata, OurNumbers(inbound=frozenset(), outbound=frozenset())
    )
    assert decided.direction is CallDirection.INBOUND
    assert decided.contact_id is None
