"""Exotel: the dial request, and the flow an answered call is connected to.

The thing worth pinning is the `Url`. Exotel's connect API does not take the
address of a media socket -- it takes the address of a *flow* in the account,
and the flow is what holds the Voicebot applet that knows the socket. A call
given a socket address is accepted, rings, connects to nothing and bills, so
the shape of that parameter is asserted rather than assumed.

Nothing here reaches Exotel.
"""

from __future__ import annotations

from typing import Any

import pytest

from uaagro_domain.enums import TelephonyProvider
from uaagro_domain.errors import AuthorizationError, MissingCredentialError
from uaagro_domain.settings import Settings
from voice_worker.adapters.telephony.control import (
    RING_TIMEOUT_S,
    ExotelAdapter,
    build_adapter,
    flow_url,
)

FARMER = "+919993338278"
EXOPHONE = "+911140001234"


def settings(**overrides: Any) -> Settings:
    base = {
        "telephony_provider": TelephonyProvider.EXOTEL,
        "exotel_sid": "uaagro",
        "exotel_api_key": "key-value",
        "exotel_api_token": "token-value",
        "exotel_app_id": "926",
        "public_base_url": "https://voice.example.com",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append((path, dict(data)))
        return _Response({"Call": {"Sid": "abc123"}})


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def adapter(**overrides: Any) -> tuple[ExotelAdapter, _Recorder]:
    recorder = _Recorder()
    made = ExotelAdapter(settings=settings(**overrides), approved_destinations=frozenset({FARMER}))
    made._client = recorder
    return made, recorder


# --------------------------------------------------------------------------- #
# The flow address
# --------------------------------------------------------------------------- #


def test_the_flow_address_is_built_from_the_account_and_the_app() -> None:
    assert flow_url(settings()) == "http://my.exotel.com/uaagro/exoml/start_voice/926"


def test_a_missing_app_id_is_refused_by_name() -> None:
    """A dial that silently used the wrong address would ring, connect to
    nothing and bill; the variable is named instead."""
    with pytest.raises(MissingCredentialError) as raised:
        flow_url(settings(exotel_app_id=None))
    assert "EXOTEL_APP_ID" in str(raised.value)


def test_a_missing_account_is_refused_by_name() -> None:
    with pytest.raises(MissingCredentialError) as raised:
        flow_url(settings(exotel_sid=None))
    assert "EXOTEL_SID" in str(raised.value)


# --------------------------------------------------------------------------- #
# The dial request
# --------------------------------------------------------------------------- #


async def test_the_call_reaches_the_farmer_first_and_then_the_flow() -> None:
    """Exotel's `connect` rings `From` and joins them to `Url` on answer, so
    `From` is the farmer and `CallerId` is our number -- the opposite of what
    the names suggest, and worth stating once."""
    exotel, recorder = adapter()

    sid = await exotel.originate(
        to=FARMER,
        from_=EXOPHONE,
        callback_url="https://ignored.example/ws/voice",
        custom_field="contact:abc",
    )

    assert sid == "abc123"
    path, data = recorder.calls[0]
    assert path == "/Calls/connect.json"
    assert data["From"] == FARMER
    assert data["CallerId"] == EXOPHONE
    assert data["Url"] == "http://my.exotel.com/uaagro/exoml/start_voice/926"
    assert data["CustomField"] == "contact:abc"
    assert data["TimeOut"] == RING_TIMEOUT_S


async def test_the_stream_address_is_never_sent_as_the_flow() -> None:
    """The bug this guards: passing the worker's socket where Exotel wants a
    flow. It was what the dialer did, and the call would have connected to
    silence."""
    exotel, recorder = adapter()
    await exotel.originate(
        to=FARMER,
        from_=EXOPHONE,
        callback_url="https://voice.example.com/ws/voice",
        custom_field=None,
    )
    assert "ws/voice" not in recorder.calls[0][1]["Url"]
    assert "CustomField" not in recorder.calls[0][1]


async def test_an_unapproved_destination_is_never_dialled() -> None:
    exotel, recorder = adapter()
    with pytest.raises(AuthorizationError):
        await exotel.originate(
            to="+919999999999", from_=EXOPHONE, callback_url="", custom_field=None
        )
    assert recorder.calls == []


def test_the_factory_builds_an_exotel_adapter() -> None:
    built = build_adapter(settings(), frozenset({FARMER}))
    assert isinstance(built, ExotelAdapter)
    assert built.provider is TelephonyProvider.EXOTEL


def test_the_rest_address_carries_the_account_and_the_region() -> None:
    exotel, _ = adapter()
    assert exotel._base_url() == "https://api.exotel.com/v1/Accounts/uaagro"
    other, _ = adapter(exotel_subdomain="api.in.exotel.com")
    assert other._base_url() == "https://api.in.exotel.com/v1/Accounts/uaagro"
