"""Indian phone-number normalisation.

Every phone number entering the system -- from a telephony ``start`` event, a
CSV upload, or speech -- passes through :func:`normalise_msisdn` before it is
hashed, stored or dialled. Two representations of the same number must never
produce two farmer rows, because the farmer record is keyed on a hash of the
canonical form (§10).

Nothing here logs or formats a full number: §23-6 forbids it. Use
:func:`redact` wherever a number would otherwise reach a log line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import InvalidPhoneNumberError

#: India: 10 significant digits, first digit 6-9 for mobile.
_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")

#: Landline and service numbers we accept as *dialable* but not as a farmer
#: identity: toll-free (1800...), the short 140 promotional series, and 1600.
_SERVICE_PREFIXES = ("1800", "1600", "140")

_NON_DIGIT = re.compile(r"[^\d]")

COUNTRY_CODE = "91"


@dataclass(frozen=True, slots=True)
class Msisdn:
    """A validated Indian mobile number.

    ``national`` is the 10-digit form and is the canonical value that gets
    hashed. ``e164`` is what the telephony adapter dials.
    """

    national: str

    @property
    def e164(self) -> str:
        return f"+{COUNTRY_CODE}{self.national}"

    @property
    def last4(self) -> str:
        return self.national[-4:]

    def __str__(self) -> str:
        # Guard against a number reaching a log through a stray f-string.
        return redact(self.national)

    def __repr__(self) -> str:
        return f"Msisdn({redact(self.national)})"


def normalise_msisdn(raw: str) -> Msisdn:
    """Reduce any spoken or written form to the canonical 10-digit number.

    Accepts ``+91 98765 43210``, ``0098765-43210``, ``09876543210``,
    ``919876543210`` and ``9876543210``.

    Raises:
        InvalidPhoneNumberError: with a reason that never echoes the number.
    """
    if not raw or not raw.strip():
        raise InvalidPhoneNumberError("empty")

    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        raise InvalidPhoneNumberError("no digits present")

    # Strip international access prefixes, then the country code, then a
    # domestic trunk zero. Order matters: 0091... is all three.
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) > 10 and digits.startswith(COUNTRY_CODE):
        digits = digits[len(COUNTRY_CODE) :]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) != 10:
        raise InvalidPhoneNumberError(f"expected 10 significant digits, got {len(digits)}")
    if not _MOBILE_RE.match(digits):
        raise InvalidPhoneNumberError("Indian mobile numbers start with 6, 7, 8 or 9")

    return Msisdn(national=digits)


def try_normalise_msisdn(raw: str) -> Msisdn | None:
    """Non-raising variant, for bulk CSV validation where a row is rejected
    rather than the whole import (§13.1)."""
    try:
        return normalise_msisdn(raw)
    except InvalidPhoneNumberError:
        return None


def is_service_number(raw: str) -> bool:
    """True for toll-free and designated commercial series.

    A campaign CLI is checked against this rather than :func:`normalise_msisdn`,
    since a 140-series CLI is not a mobile number (§18).
    """
    digits = _NON_DIGIT.sub("", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith(COUNTRY_CODE) and len(digits) > 10:
        digits = digits[len(COUNTRY_CODE) :]
    return digits.startswith(_SERVICE_PREFIXES)


def is_promotional_series(raw: str, *, series: str = "140") -> bool:
    """§18: promotional and robo-calls must originate from the 140 series.

    The campaign compliance gate blocks approval when this returns False.
    """
    digits = _NON_DIGIT.sub("", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith(COUNTRY_CODE) and len(digits) > 10:
        digits = digits[len(COUNTRY_CODE) :]
    return digits.startswith(series)


def redact(raw: str) -> str:
    """Render a number safe for logs: ``******3210``.

    §17 ships logs with PII redacted at the logger; this is the function that
    layer calls, and it is also what ``Msisdn.__str__`` uses so an accidental
    interpolation cannot leak a full number.
    """
    digits = _NON_DIGIT.sub("", raw or "")
    if len(digits) < 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]
