"""Land area, weight and pack conversions.

The bigha is not a fixed unit in Uttar Pradesh -- it varies by district and even
by tehsil (KB §7). Every conversion therefore takes an explicit factor rather
than a constant, and the caller is expected to source that factor from the
``districts`` row. Where no district factor is known, :data:`DEFAULT_BIGHA_ACRES`
is used and the caller is flagged so the agent can confirm out loud:
*"आपके यहाँ बीघा कितने का होता है जी?"*

All arithmetic is :class:`~decimal.Decimal`. A dose is agrochemical guidance;
binary float drift in that path is not acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .enums import DoseBasis, LandUnit
from .errors import UnitConversionError

# --------------------------------------------------------------------------- #
# Fixed relations (KB §7)
# --------------------------------------------------------------------------- #

SQ_METRES_PER_ACRE = Decimal("4046.856")
ACRES_PER_HECTARE = Decimal("2.471054")

#: UP "pucca" bigha, the most widely prevailing value in UA Agro districts.
#: Overridden per district; see ``districts.bigha_acres``.
DEFAULT_BIGHA_ACRES = Decimal("0.625")

#: UP "kacha" bigha, retained because several eastern districts use it.
KACHA_BIGHA_ACRES = Decimal("0.208")

#: A katha is one twentieth of the local bigha, so it moves with it.
KATHAS_PER_BIGHA = Decimal("20")

KG_PER_QUINTAL = Decimal("100")

#: Pack sizes farmers speak in. Confirm against the live catalogue before
#: quoting -- these are the conventional sizes, not a price list (KB §7).
CONVENTIONAL_BAG_KG: dict[str, Decimal] = {
    "urea": Decimal("45"),
    "dap": Decimal("50"),
    "npk": Decimal("50"),
    "mop": Decimal("50"),
    "ssp": Decimal("50"),
}

_AREA_QUANT = Decimal("0.0001")
_DOSE_QUANT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class LandArea:
    """An area as the farmer stated it, plus the factor used to interpret it.

    Keeping ``bigha_acres`` on the value means a dose calculated for a Barabanki
    farmer can be audited later even if the district factor is corrected.
    """

    value: Decimal
    unit: LandUnit
    bigha_acres: Decimal = DEFAULT_BIGHA_ACRES
    #: True when no district-specific factor was available and the default was
    #: assumed. The agent asks the farmer to confirm before advising on a dose.
    bigha_assumed: bool = False

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise UnitConversionError(
                f"Land area must be positive, got {self.value}.",
                remedy="Ask the farmer to restate the plot size, then read it back "
                "for confirmation before calculating a dose.",
                context={"value": str(self.value), "unit": self.unit.value},
            )

    @property
    def acres(self) -> Decimal:
        return to_acres(self.value, self.unit, bigha_acres=self.bigha_acres)

    @property
    def hectares(self) -> Decimal:
        return (self.acres / ACRES_PER_HECTARE).quantize(_AREA_QUANT, rounding=ROUND_HALF_UP)

    def as_unit(self, unit: LandUnit) -> Decimal:
        return from_acres(self.acres, unit, bigha_acres=self.bigha_acres)


def to_acres(
    value: Decimal | int | float | str,
    unit: LandUnit,
    *,
    bigha_acres: Decimal = DEFAULT_BIGHA_ACRES,
) -> Decimal:
    """Convert any supported land unit to acres."""
    v = _as_decimal(value)
    match unit:
        case LandUnit.ACRE:
            acres = v
        case LandUnit.HECTARE:
            acres = v * ACRES_PER_HECTARE
        case LandUnit.BIGHA:
            acres = v * bigha_acres
        case LandUnit.KATHA:
            acres = v * (bigha_acres / KATHAS_PER_BIGHA)
    return acres.quantize(_AREA_QUANT, rounding=ROUND_HALF_UP)


def from_acres(
    acres: Decimal | int | float | str,
    unit: LandUnit,
    *,
    bigha_acres: Decimal = DEFAULT_BIGHA_ACRES,
) -> Decimal:
    """Convert acres back into the unit the farmer used.

    §3: never answer in metric only. The agent states the dose in the unit the
    caller spoke, so every outbound number goes through here.
    """
    a = _as_decimal(acres)
    match unit:
        case LandUnit.ACRE:
            out = a
        case LandUnit.HECTARE:
            out = a / ACRES_PER_HECTARE
        case LandUnit.BIGHA:
            out = a / bigha_acres
        case LandUnit.KATHA:
            out = a / (bigha_acres / KATHAS_PER_BIGHA)
    return out.quantize(_AREA_QUANT, rounding=ROUND_HALF_UP)


#: Which land unit each area-based dose basis is expressed in.
_BASIS_UNIT: dict[DoseBasis, LandUnit] = {
    DoseBasis.PER_ACRE: LandUnit.ACRE,
    DoseBasis.PER_BIGHA: LandUnit.BIGHA,
    DoseBasis.PER_KATHA: LandUnit.KATHA,
    DoseBasis.PER_HECTARE: LandUnit.HECTARE,
}

#: Bases that are not area-scaled and must never be multiplied by a plot size.
NON_AREA_BASES: frozenset[DoseBasis] = frozenset({DoseBasis.PER_LITRE_WATER, DoseBasis.PER_PLANT})


def scale_dose(
    dose_value: Decimal | int | float | str,
    basis: DoseBasis,
    area: LandArea,
) -> Decimal:
    """Scale an approved per-unit dose to the farmer's actual plot.

    The dose *value* always originates from an approved ``crop_recommendations``
    row (§9, §16.2). This function only does arithmetic on it -- it never
    invents, adjusts or substitutes a figure.

    Raises:
        UnitConversionError: if the basis is not area-scaled, which would make
            multiplying by a plot size meaningless and dangerous.
    """
    if basis in NON_AREA_BASES:
        raise UnitConversionError(
            f"Dose basis {basis.value} is not area-scaled and cannot be multiplied by a plot size.",
            remedy="Speak the per-litre or per-plant figure as recorded, and state the "
            "spray volume separately. Do not scale it by area.",
            context={"basis": basis.value},
        )

    dose = _as_decimal(dose_value)
    plot_in_basis_unit = area.as_unit(_BASIS_UNIT[basis])
    return (dose * plot_in_basis_unit).quantize(_DOSE_QUANT, rounding=ROUND_HALF_UP)


def bags_for_quantity(quantity_kg: Decimal | int | float | str, bag_kg: Decimal) -> Decimal:
    """Whole bags plus remainder, as a decimal count of bags.

    Farmers buy in बोरी, so a dose in kilos is followed by the pack count:
    *"एक बीघे के लिए लगभग बीस किलो -- यानी आधी बोरी।"* (KB §7)
    """
    if bag_kg <= 0:
        raise UnitConversionError(
            f"Bag size must be positive, got {bag_kg}.",
            remedy="Correct the pack size on the product variant in the catalogue.",
            context={"bag_kg": str(bag_kg)},
        )
    return (_as_decimal(quantity_kg) / bag_kg).quantize(_DOSE_QUANT, rounding=ROUND_HALF_UP)


def quintals_to_kg(quintals: Decimal | int | float | str) -> Decimal:
    return (_as_decimal(quintals) * KG_PER_QUINTAL).quantize(_DOSE_QUANT, rounding=ROUND_HALF_UP)


def kg_to_quintals(kg: Decimal | int | float | str) -> Decimal:
    return (_as_decimal(kg) / KG_PER_QUINTAL).quantize(_DOSE_QUANT, rounding=ROUND_HALF_UP)


def _as_decimal(value: Decimal | int | float | str) -> Decimal:
    """Coerce to Decimal without inheriting binary float error.

    A float is routed through ``str`` deliberately: ``Decimal(0.1)`` carries the
    full binary expansion, ``Decimal("0.1")`` does not.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)
