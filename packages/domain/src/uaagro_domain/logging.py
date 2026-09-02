"""Structured logging with PII redacted at the logger (§19).

§23-6 forbids logging a full phone number, a recording URL or a vendor key.
Relying on every call site to remember that does not work, so redaction happens
in a processor: anything that looks like a phone number, an object key or a
credential is masked on its way out, whatever the call site passed.

``call_id`` is bound to a context variable so every line emitted during a call
carries it without being threaded through each function (§19).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.typing import EventDict, WrappedLogger

#: Keys whose values are replaced wholesale.
_SECRET_KEYS = frozenset(
    {
        "password",
        "token",
        "api_key",
        "access_token",
        "authorization",
        "secret",
        "key_hash",
        "mfa_secret",
        "phone_enc",
        "pepper",
        "dek",
        "recording_url",
        "signed_url",
    }
)

#: Keys holding a phone number, which is masked to its last four digits.
_PHONE_KEYS = frozenset({"phone", "from_number", "to_number", "msisdn", "caller", "cli"})

#: Any 10-digit Indian mobile appearing inside free text, with or without a
#: country code. Caught even when it was never passed as a phone field.
_PHONE_IN_TEXT = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{9})(?!\d)")

#: A link to a call recording. §23-6 forbids logging one: it is a bearer
#: reference to a farmer's voice, and log aggregators are widely readable.
_RECORDING_URL = re.compile(r"https?://\S*/(?:recordings?|calls?)/\S+", re.IGNORECASE)

_REDACTED = "[redacted]"


def _mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def _redact_value(key: str, value: Any) -> Any:
    """One value, masked according to its key and its content.

    Recursive, because a number nested one level down is still a number in the
    log aggregator. ``log.info("call.failed", context={"from": "+9198..."})``
    is an ordinary thing to write and would otherwise walk straight past a
    processor that only inspected the top level.
    """
    lowered = key.lower()
    if lowered in _SECRET_KEYS or lowered.endswith(("_secret", "_token", "_password", "_key")):
        return _REDACTED
    if isinstance(value, str):
        if lowered in _PHONE_KEYS:
            return _mask_phone(value)
        if _PHONE_IN_TEXT.search(value):
            return _PHONE_IN_TEXT.sub(lambda match: _mask_phone(match.group(1)), value)
        if _RECORDING_URL.search(value):
            # §23-6 treats a recording URL like a phone number: it is a
            # bearer link to a farmer's voice.
            return _RECORDING_URL.sub(_REDACTED, value)
        return value
    if isinstance(value, Mapping):
        return {inner: _redact_value(str(inner), item) for inner, item in value.items()}
    if isinstance(value, list | tuple):
        # The key travels down: a list under "phone_numbers" is still phones.
        return type(value)(_redact_value(key, item) for item in value)
    return value


def redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask secrets, phone numbers and recording URLs before any handler.

    §23-6 makes all three unloggable, and §19 puts the redaction here rather
    than at the call sites: there are hundreds of call sites and one processor,
    and only one of those numbers stays right as the code grows.
    """
    for key, value in list(event_dict.items()):
        event_dict[key] = _redact_value(key, value)
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = False) -> None:
    """Install the shared logging configuration.

    Called once at process start by each service's ``main``.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=not json_output)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Last before rendering, so nothing added by an earlier processor
            # can slip past it.
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def bind_call(call_id: str, **extra: object) -> None:
    """Bind call context for the current task (§19 correlation key)."""
    bind_contextvars(call_id=call_id, **extra)


def clear_call() -> None:
    clear_contextvars()
