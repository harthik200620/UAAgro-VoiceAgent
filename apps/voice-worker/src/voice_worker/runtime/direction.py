"""Which way a call is going, from the provider's ``start`` frame (§4.3, §13.2).

Every call reaches the same WebSocket. The provider does not say "this is one
you asked for"; it sends two numbers and, when the originate request carried
one, a custom field. Deciding the direction wrongly is expensive in both
directions: an outbound call greeted as a helpline confuses the farmer and
never delivers the message, and an inbound caller greeted with "यह एक
ऑटोमैटिक कॉल है, क्या मैं राम जी से बात कर रहा हूँ?" hangs up.

Two signals, in order of trust:

1. **The custom field the dialer set.** ``contact:<uuid>`` names the campaign
   contact being dialled. When it is present there is no ambiguity, and it is
   also how the call is linked to its contact card.
2. **Our own numbers.** A call in which either party is one of our outbound
   caller IDs is one we placed. Which side the provider reports us on differs
   between providers and, in Exotel's case, between documentation versions --
   so both sides are checked and the farmer is whichever number is not ours.

The second signal is an assumption about the provider's start frame that a
real call must confirm; the first is under our control, which is why the
dialer always sets it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from uaagro_domain.enums import CallDirection
from uaagro_domain.settings import Settings

from ..adapters.telephony.base import CallMetadata

_CONTACT_REF = re.compile(r"contact:([0-9a-fA-F-]{36})")
#: A test call from the panel names the draft it should speak (§15.1's
#: "test on my phone") rather than a contact.
_TEST_REF = re.compile(r"test:([0-9a-fA-F-]{36})")


@dataclass(frozen=True, slots=True)
class OurNumbers:
    """The numbers this deployment answers on and calls from, ten digits each."""

    inbound: frozenset[str]
    outbound: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> OurNumbers:
        inbound = {n for n in (national(settings.inbound_did),) if n}
        outbound = {
            n
            for n in (
                national(settings.outbound_cli_promotional),
                national(settings.outbound_cli_transactional),
            )
            if n
        }
        return cls(inbound=frozenset(inbound), outbound=frozenset(outbound))

    @property
    def all(self) -> frozenset[str]:
        return self.inbound | self.outbound


@dataclass(frozen=True, slots=True)
class Direction:
    """The decision, plus who the farmer is and which contact this serves."""

    direction: CallDirection
    #: The farmer's number as the provider sent it. Hashed by the caller and
    #: never stored or logged in this form.
    farmer_number: str | None
    contact_id: uuid.UUID | None
    #: The script version a panel test call should speak, when it is one.
    config_id: uuid.UUID | None = None


def national(number: str | None) -> str | None:
    """The ten-digit form of an Indian number, or None if it is not one.

    Providers send ``+919876543210``, ``09876543210`` and ``9876543210`` for
    the same phone; the last ten digits are the identity.
    """
    if not number:
        return None
    digits = re.sub(r"\D", "", number)
    if len(digits) < 10:
        return None
    return digits[-10:]


def _custom_values(extra: dict[str, Any]) -> list[str]:
    """Every string the provider echoed back from the originate request.

    Exotel echoes the originate request's ``CustomField`` under
    ``custom_parameters`` in the start frame; the flattened spellings are kept
    for providers that pass the field through as-is.
    """
    candidates: list[str] = []
    custom = extra.get("custom_parameters")
    if isinstance(custom, dict):
        candidates.extend(str(v) for v in custom.values())
    elif isinstance(custom, str):
        candidates.append(custom)
    for key in ("CustomField", "custom_field", "customField"):
        value = extra.get(key)
        if isinstance(value, str):
            candidates.append(value)
    return candidates


def _reference(extra: dict[str, Any], pattern: re.Pattern[str]) -> uuid.UUID | None:
    for candidate in _custom_values(extra):
        match = pattern.search(candidate)
        if match:
            try:
                return uuid.UUID(match.group(1))
            except ValueError:
                continue
    return None


def contact_reference(extra: dict[str, Any]) -> uuid.UUID | None:
    """The contact id the dialer put in the originate request, if any."""
    return _reference(extra, _CONTACT_REF)


def test_reference(extra: dict[str, Any]) -> uuid.UUID | None:
    """The draft script a panel test call named, if any."""
    return _reference(extra, _TEST_REF)


def classify_direction(metadata: CallMetadata, ours: OurNumbers | None) -> Direction:
    """Decide inbound or outbound, and which number belongs to the farmer."""
    contact_id = contact_reference(metadata.extra)
    config_id = test_reference(metadata.extra)
    from_national = national(metadata.from_number)
    to_national = national(metadata.to_number)
    known = ours or OurNumbers(frozenset(), frozenset())

    from_is_ours = from_national in known.all if from_national else False
    to_is_ours = to_national in known.all if to_national else False

    # Whichever side is not us is the farmer. When neither is recognised --
    # a simulator, or a deployment with no numbers configured -- the caller
    # is the farmer, which is the inbound reading and the safe default.
    if from_is_ours and not to_is_ours:
        farmer = metadata.to_number
    elif to_is_ours and not from_is_ours:
        farmer = metadata.from_number
    else:
        farmer = metadata.from_number

    placed_by_us = (
        contact_id is not None
        or config_id is not None
        or (from_national in known.outbound if from_national else False)
        or (to_national in known.outbound if to_national else False)
    )
    direction = CallDirection.OUTBOUND if placed_by_us else CallDirection.INBOUND
    return Direction(
        direction=direction, farmer_number=farmer, contact_id=contact_id, config_id=config_id
    )


__all__ = (
    "Direction",
    "OurNumbers",
    "classify_direction",
    "contact_reference",
    "national",
    "test_reference",
)
