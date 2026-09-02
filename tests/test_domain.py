"""Domain-layer unit tests: phone normalisation, units, money, config.

The numeric parser gets property-based coverage (§19) because it sits on the
path between what a farmer says and what gets ordered or sprayed -- an
off-by-one there is a wrong quantity of agrochemical, not a cosmetic bug.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from uaagro_domain.enums import (
    VENDOR_DECIDED_TURN_STRATEGIES,
    DoseBasis,
    LandUnit,
    QualityTier,
    TurnStrategy,
)
from uaagro_domain.errors import InvalidPhoneNumberError, UnitConversionError
from uaagro_domain.money import CostBreakdown, CostComponent, Money, RateUnit, VendorRate
from uaagro_domain.phone import (
    is_promotional_series,
    is_service_number,
    normalise_msisdn,
    redact,
    try_normalise_msisdn,
)
from uaagro_domain.settings import get_defaults
from uaagro_domain.units import (
    ACRES_PER_HECTARE,
    DEFAULT_BIGHA_ACRES,
    LandArea,
    bags_for_quantity,
    from_acres,
    scale_dose,
    to_acres,
)

# --------------------------------------------------------------------------- #
# Phone numbers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "9876543210",
        "09876543210",
        "919876543210",
        "+919876543210",
        "+91 98765 43210",
        "0091-98765-43210",
        "  +91-9876-543-210  ",
    ],
)
def test_every_written_form_of_a_number_collapses_to_one_identity(raw: str) -> None:
    """One farmer must never become two rows because of formatting (§10)."""
    assert normalise_msisdn(raw).national == "9876543210"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "12345", "1234567890", "5876543210", "98765432101", "+1 415 555 0100"],
)
def test_invalid_numbers_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidPhoneNumberError):
        normalise_msisdn(raw)


def test_rejection_message_never_echoes_the_number() -> None:
    """§23-6: errors reach the logs, so they must not carry a phone number."""
    with pytest.raises(InvalidPhoneNumberError) as caught:
        normalise_msisdn("5876543210")
    assert "5876543210" not in str(caught.value)


def test_try_normalise_returns_none_instead_of_raising() -> None:
    """CSV import rejects a row, not the whole file (§13.1)."""
    assert try_normalise_msisdn("nonsense") is None
    assert try_normalise_msisdn("9876543210") is not None


@given(st.integers(min_value=6_000_000_000, max_value=9_999_999_999))
def test_redaction_never_reveals_more_than_the_last_four(number: int) -> None:
    masked = redact(str(number))
    assert masked.endswith(str(number)[-4:])
    assert str(number)[:6] not in masked


def test_msisdn_str_is_masked_so_an_f_string_cannot_leak_it() -> None:
    msisdn = normalise_msisdn("9876543210")
    assert "9876543210" not in f"{msisdn}"
    assert "9876543210" not in repr(msisdn)
    # The real value is still reachable deliberately.
    assert msisdn.e164 == "+919876543210"


def test_promotional_and_service_series_are_recognised() -> None:
    """§18: promotional calling requires a 140-series CLI."""
    assert is_promotional_series("14012345678")
    assert not is_promotional_series("9876543210")
    assert is_service_number("18002127074")
    assert not is_service_number("9876543210")


# --------------------------------------------------------------------------- #
# Land units and dosing
# --------------------------------------------------------------------------- #


def test_bigha_conversion_uses_the_district_factor() -> None:
    """KB §7: the bigha is not fixed in UP, so the factor travels with the value."""
    pucca = LandArea(Decimal("1"), LandUnit.BIGHA, bigha_acres=Decimal("0.625"))
    kacha = LandArea(Decimal("1"), LandUnit.BIGHA, bigha_acres=Decimal("0.208"))
    assert pucca.acres == Decimal("0.6250")
    assert kacha.acres == Decimal("0.2080")
    assert pucca.acres != kacha.acres


def test_katha_moves_with_the_local_bigha() -> None:
    twenty_kathas = to_acres(20, LandUnit.KATHA, bigha_acres=Decimal("0.625"))
    one_bigha = to_acres(1, LandUnit.BIGHA, bigha_acres=Decimal("0.625"))
    assert twenty_kathas == one_bigha


def test_hectare_relation_matches_the_published_factor() -> None:
    assert to_acres(1, LandUnit.HECTARE) == ACRES_PER_HECTARE.quantize(Decimal("0.0001"))


@given(
    value=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999"), places=2),
    unit=st.sampled_from(list(LandUnit)),
)
def test_area_round_trips_through_acres(value: Decimal, unit: LandUnit) -> None:
    """Converting out and back must not drift more than the quantisation step."""
    acres = to_acres(value, unit)
    back = from_acres(acres, unit)
    assert abs(back - value) <= Decimal("0.01")


def test_zero_or_negative_area_is_rejected() -> None:
    """A farmer misheard as "zero bigha" must fail loudly, not silently dose zero."""
    with pytest.raises(UnitConversionError):
        LandArea(Decimal("0"), LandUnit.BIGHA)
    with pytest.raises(UnitConversionError):
        LandArea(Decimal("-2"), LandUnit.ACRE)


def test_dose_scales_to_the_farmers_own_unit() -> None:
    """50 kg/acre on one pucca bigha is 31.25 kg."""
    area = LandArea(Decimal("1"), LandUnit.BIGHA, bigha_acres=DEFAULT_BIGHA_ACRES)
    assert scale_dose(Decimal("50"), DoseBasis.PER_ACRE, area) == Decimal("31.25")


def test_dose_expressed_per_bigha_needs_no_conversion() -> None:
    area = LandArea(Decimal("3"), LandUnit.BIGHA)
    assert scale_dose(Decimal("10"), DoseBasis.PER_BIGHA, area) == Decimal("30.00")


@pytest.mark.parametrize("basis", [DoseBasis.PER_LITRE_WATER, DoseBasis.PER_PLANT])
def test_non_area_dose_bases_refuse_to_be_scaled_by_plot_size(basis: DoseBasis) -> None:
    """Multiplying a per-litre dose by an acreage is meaningless and dangerous."""
    area = LandArea(Decimal("2"), LandUnit.ACRE)
    with pytest.raises(UnitConversionError):
        scale_dose(Decimal("2"), basis, area)


def test_bag_count_follows_the_dose() -> None:
    """KB §7: give the answer in kilos, then the pack count."""
    assert bags_for_quantity(Decimal("31.25"), Decimal("50")) == Decimal("0.63")
    assert bags_for_quantity(Decimal("100"), Decimal("50")) == Decimal("2.00")


def test_zero_bag_size_is_rejected() -> None:
    with pytest.raises(UnitConversionError):
        bags_for_quantity(Decimal("50"), Decimal("0"))


# --------------------------------------------------------------------------- #
# Money and the cost model
# --------------------------------------------------------------------------- #


def test_cost_breakdown_reproduces_the_specification_worked_example() -> None:
    """§8: a 3-minute Hindi inbound call is about ₹6.65, or ₹2.20 a minute."""
    breakdown = CostBreakdown()
    breakdown.add(CostComponent.TELEPHONY, Decimal("1.80"))
    breakdown.add(CostComponent.STT, Decimal("1.50"))
    breakdown.add(CostComponent.TTS, Decimal("2.85"))
    breakdown.add(CostComponent.LLM, Decimal("0.20"))
    breakdown.add(CostComponent.COMPUTE, Decimal("0.30"))

    assert breakdown.total.rupees == Decimal("6.65")
    assert abs(breakdown.per_minute(180).rupees - Decimal("2.20")) < Decimal("0.05")


def test_deepgram_premium_over_sarvam_matches_the_specification() -> None:
    """§8 states the swap adds roughly ₹0.19/min. Verified, not assumed."""
    deepgram = VendorRate("deepgram", "stt", RateUnit.MINUTE, Decimal("0.0078"), "USD")
    per_minute_inr = deepgram.cost_inr(1, usd_inr=Decimal("88.0"))
    sarvam_per_minute = Decimal("0.50")
    assert abs((per_minute_inr - sarvam_per_minute) - Decimal("0.19")) < Decimal("0.01")


def test_unpriced_vendor_contributes_zero_and_is_recorded() -> None:
    """§8: metering must never crash a call. An unknown rate is noted, not guessed."""
    breakdown = CostBreakdown()
    breakdown.note_unpriced("newvendor:tts")
    assert breakdown.total.rupees == Decimal("0.00")
    assert "newvendor:tts" in breakdown.to_dict()["unpriced"]


def test_money_is_decimal_all_the_way_down() -> None:
    total = Money.of("0.1") + Money.of("0.2")
    assert total.rupees == Decimal("0.30")


def test_zero_duration_call_does_not_divide_by_zero() -> None:
    breakdown = CostBreakdown()
    breakdown.add(CostComponent.TELEPHONY, Decimal("0.06"))
    assert breakdown.per_minute(0).rupees == Decimal("0.00")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_language_routing_is_declarative_and_complete() -> None:
    """§5.1 is a config table, not a chain of if-statements."""
    defaults = get_defaults()
    for code in ("hi-IN", "en-IN", "mr-IN", "ml-IN"):
        assert code in defaults.language_routes


def test_hindi_and_english_are_tier_a_with_vendor_decided_turns() -> None:
    """§5.1's top tier means the recogniser ends the turn itself."""
    defaults = get_defaults()
    for code in ("hi-IN", "en-IN"):
        _, route = defaults.resolve_language(code)
        assert route.quality_tier is QualityTier.A
        assert route.stt is not None
        assert route.stt.provider == "soniox"
        assert route.turn is not None
        assert route.turn.strategy in VENDOR_DECIDED_TURN_STRATEGIES


def test_every_recogniser_is_paired_with_its_own_turn_strategy() -> None:
    """The mistake this guards against does not raise anywhere.

    A vendor-decided strategy on a transcribe-only recogniser means nothing
    ends the turn; a local detector in front of a self-deciding one means two
    things race and the caller is cut off mid-sentence. Neither leaves a stack
    trace, so the table is checked as a whole rather than language by language.
    """
    vendor_decided = {
        "soniox": TurnStrategy.SONIOX_ENDPOINT,
        "deepgram": TurnStrategy.FLUX_SEMANTIC,
    }
    for code, route in get_defaults().language_routes.items():
        if route.stt is None or route.turn is None:
            continue
        expected = vendor_decided.get(route.stt.provider)
        if expected is not None:
            assert route.turn.strategy is expected, code
        else:
            assert route.turn.strategy not in VENDOR_DECIDED_TURN_STRATEGIES, code


def test_marathi_no_longer_needs_a_separate_turn_model() -> None:
    """The split §5.1 was forced into is gone.

    Flux carried ten languages and Marathi was not one of them, so recognition
    and end-of-turn had to come from different vendors -- a second model and a
    second failure mode. Soniox covers it and decides the turn itself.
    """
    _, route = get_defaults().resolve_language("mr-IN")
    assert route.stt is not None
    assert route.stt.provider == "soniox"
    assert route.turn is not None
    assert route.turn.strategy is TurnStrategy.SONIOX_ENDPOINT


def test_malayalam_keeps_its_tier_until_the_quality_is_measured() -> None:
    """§5.1 tiers are a *measured* claim.

    Malayalam gained semantic endpointing with the engine change, which is a
    real improvement over VAD-only -- and not a reason to relabel it A. Nothing
    has been measured yet, so the letter holds and the note says why. Raising
    a tier on a vendor's documentation is exactly the dishonesty the tier
    exists to prevent.
    """
    _, route = get_defaults().resolve_language("ml-IN")
    assert route.quality_tier is QualityTier.C
    assert route.turn is not None
    assert route.turn.strategy is TurnStrategy.SONIOX_ENDPOINT
    assert "pending" in route.tier_note_en.lower()


def test_odia_is_not_routed_to_an_engine_on_an_assumption() -> None:
    """Soniox's Odia coverage is unconfirmed, so Odia stays where it was.

    Routing a language to an engine because the neighbouring ten are supported
    is how a helpline discovers in production that it cannot hear a caller.
    """
    _, route = get_defaults().resolve_language("or-IN")
    assert route.stt is not None
    assert route.stt.provider == "sarvam"
    assert route.quality_tier is QualityTier.C


def test_bhojpuri_rides_the_hindi_path_but_keeps_its_own_tier() -> None:
    """§5.1: route to Hindi, do not attempt a separate model -- and stay honest
    about the resulting quality rather than reporting Hindi's tier A."""
    resolved, route = get_defaults().resolve_language("bho")
    assert resolved == "hi-IN"
    assert route.stt is not None
    assert route.stt.provider == "soniox"
    assert route.quality_tier is QualityTier.B
    assert route.label_en == "Bhojpuri"


def test_awadhi_alias_resolves_through_bhojpuri() -> None:
    resolved, _ = get_defaults().resolve_language("awa")
    assert resolved == "hi-IN"


def test_unknown_language_fails_with_an_actionable_message() -> None:
    from uaagro_domain.errors import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        get_defaults().resolve_language("xx-YY")
    assert "config/defaults.yaml" in caught.value.remedy


def test_latency_budget_matches_the_specification() -> None:
    """§7 is a requirement, not an aspiration, so the numbers are asserted."""
    budget = get_defaults().latency_budget_ms
    assert budget.total_no_tool.p95 == 1200
    assert budget.total_with_tool.p95 == 1500
    assert budget.llm_ttft.p95 == 550
    assert budget.tts_ttfb.p95 == 350


def test_calling_window_is_the_regulated_one() -> None:
    """§18: 09:00-21:00, and an operator cannot widen it."""
    compliance = get_defaults().compliance
    assert compliance.calling_window_start == "09:00"
    assert compliance.calling_window_end == "21:00"
    assert compliance.promotional_cli_series == "140"
    assert compliance.consent_validity_days > 0
