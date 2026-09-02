"""Money and per-call cost accounting (§8).

Rupees are :class:`~decimal.Decimal` throughout. The cost breakdown is a
first-class value rather than a loose dict because §8 requires the admin panel
to show per-call cost split by component, and §1 N8 requires it stored on every
call record -- including failed and rejected ones, where the whole point is to
show that a 6-second spam rejection cost ₹0.06 and not ₹6.65.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

PAISE = Decimal("0.01")
#: Vendor rates are quoted to more places than a rupee (₹0.0078/min), so
#: intermediate accumulation keeps four places and only the total rounds.
RATE_PRECISION = Decimal("0.0001")


class CostComponent(StrEnum):
    """The lines the admin panel breaks a call into (§8)."""

    TELEPHONY = "telephony"
    STT = "stt"
    TTS = "tts"
    LLM = "llm"
    COMPUTE = "compute"
    WHATSAPP = "whatsapp"


class RateUnit(StrEnum):
    MINUTE = "minute"
    KILOCHAR = "kilochar"
    MESSAGE = "message"
    CALL = "call"
    KILOTOKEN_INPUT = "kilotoken_input"
    KILOTOKEN_OUTPUT = "kilotoken_output"
    KILOTOKEN_CACHED = "kilotoken_cached"


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in Indian rupees."""

    amount: Decimal

    @classmethod
    def zero(cls) -> Money:
        return cls(Decimal("0.00"))

    @classmethod
    def of(cls, value: Decimal | int | float | str) -> Money:
        return cls(_dec(value).quantize(PAISE, rounding=ROUND_HALF_UP))

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        return Money(self.amount - other.amount)

    def __mul__(self, factor: Decimal | int) -> Money:
        return Money.of(self.amount * _dec(factor))

    def __lt__(self, other: Money) -> bool:
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        return self.amount <= other.amount

    @property
    def rupees(self) -> Decimal:
        return self.amount.quantize(PAISE, rounding=ROUND_HALF_UP)

    def __str__(self) -> str:
        return f"₹{self.rupees}"


@dataclass(slots=True)
class CostBreakdown:
    """Per-call cost, accumulated live during the call.

    Written to ``calls.cost_breakdown`` at finalisation. Accumulation never
    raises: §8 makes cost a reporting concern, and a metering bug must not be
    able to drop a call. An unknown vendor rate contributes zero and is logged.
    """

    components: dict[CostComponent, Decimal] = field(default_factory=dict)
    unpriced: list[str] = field(default_factory=list)

    def add(self, component: CostComponent, amount_inr: Decimal | int | float | str) -> None:
        value = _dec(amount_inr).quantize(RATE_PRECISION, rounding=ROUND_HALF_UP)
        self.components[component] = self.components.get(component, Decimal("0")) + value

    def note_unpriced(self, what: str) -> None:
        """Record a vendor/service with no rate card entry rather than guessing."""
        if what not in self.unpriced:
            self.unpriced.append(what)

    @property
    def total(self) -> Money:
        return Money.of(sum(self.components.values(), Decimal("0")))

    def per_minute(self, duration_seconds: int) -> Money:
        if duration_seconds <= 0:
            return Money.zero()
        return Money.of(self.total.amount * Decimal("60") / Decimal(duration_seconds))

    def to_dict(self) -> dict[str, str | list[str]]:
        out: dict[str, str | list[str]] = {
            component.value: str(amount.quantize(RATE_PRECISION, rounding=ROUND_HALF_UP))
            for component, amount in self.components.items()
        }
        out["total"] = str(self.total.rupees)
        if self.unpriced:
            out["unpriced"] = self.unpriced
        return out


@dataclass(frozen=True, slots=True)
class VendorRate:
    """One row of the live rate card (``vendor_rates``).

    Rates are data, not constants: §8 requires a daily job to re-verify them and
    the dashboard to show a cost regression as plainly as a latency regression.
    """

    vendor: str
    service: str
    unit: RateUnit
    rate: Decimal
    currency: str = "INR"

    def cost_inr(self, quantity: Decimal | int | float | str, *, usd_inr: Decimal) -> Decimal:
        """Cost of ``quantity`` units, converted to rupees when quoted in USD."""
        amount = _dec(quantity) * self.rate
        if self.currency.upper() == "USD":
            amount *= usd_inr
        return amount.quantize(RATE_PRECISION, rounding=ROUND_HALF_UP)


def _dec(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)
