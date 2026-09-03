"""Reading a shop's database on its own terms (§15.1).

These are the pure functions behind the data-source sync: what a saved
mapping must contain, and what a pack size, a price, a category or a
quantity means when it is written the way a shop writes it. No database.
"""

from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from api.services.sources import mapping as m
from uaagro_domain.enums import ProductCategory, ProductType

# --------------------------------------------------------------------------- #
# The mapping itself
# --------------------------------------------------------------------------- #


def test_the_field_lists_match_the_contract() -> None:
    assert m.TABLES == ("stores", "products", "stock")
    required = {name: [f for f, r in fields.items() if r] for name, fields in m.FIELDS.items()}
    assert required["stores"] == ["code", "name", "district"]
    assert required["products"] == ["sku", "name", "category", "pack_size", "mrp"]
    assert required["stock"] == ["store_code", "sku", "qty"]
    # The one field beyond the contract, and why it is optional.
    assert m.FIELDS["products"]["cib_registration_no"] is False


def test_an_absent_mapping_is_empty_and_serialises_with_every_key() -> None:
    mapping = m.parse_mapping(None)
    assert mapping.is_empty
    assert mapping.as_json() == {"stores": None, "products": None, "stock": None}
    assert m.parse_mapping({}).as_json() == mapping.as_json()


def test_a_complete_mapping_round_trips() -> None:
    raw: dict[str, Any] = {
        "stores": {"table": "shops", "columns": {"code": "c", "name": "n", "district": "d"}},
        "products": None,
        "stock": {
            "table": "stk",
            "columns": {"store_code": "s", "sku": "k", "qty": "q", "price": ""},
        },
    }
    mapping = m.parse_mapping(raw)
    assert not mapping.is_empty
    assert mapping.stores is not None and mapping.stores.table == "shops"
    assert mapping.products is None
    # An optional field sent empty is simply not mapped.
    assert mapping.stock is not None and "price" not in mapping.stock.columns
    assert mapping.as_json()["stock"] == {
        "table": "stk",
        "columns": {"store_code": "s", "sku": "k", "qty": "q"},
    }


def test_missing_required_fields_are_named() -> None:
    with pytest.raises(m.MappingError) as caught:
        m.parse_mapping({"products": {"table": "items", "columns": {"name": "title"}}})
    error = caught.value
    assert error.code == "validation_error"
    assert error.context == {
        "table": "products",
        "missing": ["sku", "category", "pack_size", "mrp"],
    }


def test_unknown_fields_and_sections_are_refused() -> None:
    with pytest.raises(m.MappingError) as caught:
        m.parse_mapping(
            {
                "stock": {
                    "table": "stk",
                    "columns": {"store_code": "a", "sku": "b", "qty": "c", "colour": "d"},
                }
            }
        )
    assert caught.value.context == {"table": "stock", "unknown": ["colour"]}
    with pytest.raises(m.MappingError) as caught:
        m.parse_mapping({"orders": {"table": "o", "columns": {}}})
    assert caught.value.context == {"unknown": ["orders"]}


@pytest.mark.parametrize("bad", ["", "items; drop table x", "a b", "x" * 65])
def test_a_table_name_must_be_an_identifier(bad: str) -> None:
    with pytest.raises(m.MappingError):
        m.parse_mapping({"stores": {"table": bad, "columns": {}}})


def test_a_column_name_must_be_an_identifier() -> None:
    with pytest.raises(m.MappingError) as caught:
        m.parse_mapping(
            {
                "stores": {
                    "table": "shops",
                    "columns": {"code": "c", "name": "n", "district": "d; --"},
                }
            }
        )
    assert caught.value.context == {"table": "stores", "field": "district"}


def test_devanagari_identifiers_are_identifiers() -> None:
    mapping = m.parse_mapping(
        {"stores": {"table": "दुकान", "columns": {"code": "कोड", "name": "नाम", "district": "d"}}}
    )
    assert mapping.stores is not None and mapping.stores.columns["code"] == "कोड"


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("50 kg", (Decimal("50"), "kg")),
        ("1 L", (Decimal("1"), "L")),
        ("500 ml", (Decimal("500"), "ml")),
        ("1 kg", (Decimal("1"), "kg")),
        ("100 g", (Decimal("100"), "g")),
        ("10 kg bag", (Decimal("10"), "kg")),
        ("500ML", (Decimal("500"), "ml")),
        ("2.5 ltr", (Decimal("2.5"), "L")),
        ("2,500 g", (Decimal("2500"), "g")),
        ("1 qtl", (Decimal("100"), "kg")),
        ("50 Kg.", (Decimal("50"), "kg")),
        ("1 bag", (Decimal("1"), "piece")),
        ("12", (Decimal("12"), "piece")),
        (4, (Decimal("4"), "piece")),
        ("5 किलो", (Decimal("5"), "kg")),
        ("", None),
        (None, None),
        ("bagful", None),
        ("0 kg", None),
        ("x", None),
    ],
)
def test_pack_sizes_are_split_into_a_number_and_a_unit(
    raw: Any, expected: tuple[Decimal, str] | None
) -> None:
    parsed = m.parse_pack_size(raw)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert parsed[0] == expected[0] and parsed[1] == expected[1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Seeds", ProductCategory.SEEDS),
        ("beej", ProductCategory.SEEDS),
        ("बीज", ProductCategory.SEEDS),
        ("Fertilizer", ProductCategory.FERTILISERS),
        ("khad", ProductCategory.FERTILISERS),
        ("Urea", ProductCategory.FERTILISERS),
        ("DAP", ProductCategory.FERTILISERS),
        ("NPK 12:32:16", ProductCategory.FERTILISERS),
        ("खाद", ProductCategory.FERTILISERS),
        ("Pesticide", ProductCategory.CROP_PROTECTION),
        ("Insecticide", ProductCategory.CROP_PROTECTION),
        ("Fungicide", ProductCategory.CROP_PROTECTION),
        ("Herbicide", ProductCategory.CROP_PROTECTION),
        ("Weedicide", ProductCategory.CROP_PROTECTION),
        ("crop_protection", ProductCategory.CROP_PROTECTION),
        ("Cattle feed", ProductCategory.CATTLE_FEED),
        ("pashu aahar", ProductCategory.CATTLE_FEED),
        ("पशु आहार", ProductCategory.CATTLE_FEED),
        ("Tools", ProductCategory.TOOLS_EQUIPMENT),
        ("Water pump", ProductCategory.TOOLS_EQUIPMENT),
        ("Sprayer", ProductCategory.TOOLS_EQUIPMENT),
        ("Farm equipment", ProductCategory.TOOLS_EQUIPMENT),
        # The more specific word wins over the more common one.
        ("Seed drill", ProductCategory.TOOLS_EQUIPMENT),
        ("Fungicide for seed treatment", ProductCategory.CROP_PROTECTION),
        ("Gadget", None),
        ("", None),
        (None, None),
    ],
)
def test_categories_are_mapped_by_keyword(raw: Any, expected: ProductCategory | None) -> None:
    assert m.map_category(raw) is expected


@pytest.mark.parametrize(
    ("category", "hints", "expected"),
    [
        (ProductCategory.SEEDS, ("Seeds", "Hybrid Maize"), ProductType.HYBRID_SEED),
        (ProductCategory.SEEDS, ("Seeds", "Wheat HD-2967"), ProductType.CERTIFIED_SEED),
        (
            ProductCategory.FERTILISERS,
            ("Fertilizer", "Urea 45 kg"),
            ProductType.STRAIGHT_FERTILISER,
        ),
        (ProductCategory.FERTILISERS, ("Fertilizer", "DAP 18:46"), ProductType.NPK),
        (ProductCategory.FERTILISERS, ("Fertilizer", "Zinc sulphate"), ProductType.MICRONUTRIENT),
        (ProductCategory.FERTILISERS, ("Bio fertilizer", "Rhizobium"), ProductType.BIO_FERTILISER),
        (ProductCategory.CROP_PROTECTION, ("Pesticide", "Imida 17.8 SL"), ProductType.INSECTICIDE),
        (ProductCategory.CROP_PROTECTION, ("Fungicide", "Mancozeb"), ProductType.FUNGICIDE),
        (ProductCategory.CROP_PROTECTION, ("Weedicide", "2,4-D"), ProductType.HERBICIDE),
        (ProductCategory.CATTLE_FEED, ("Cattle feed", "Mineral mix"), ProductType.CATTLE_FEED),
        (ProductCategory.TOOLS_EQUIPMENT, ("Tools", "Knapsack sprayer"), ProductType.SPRAYER),
        (ProductCategory.TOOLS_EQUIPMENT, ("Tools", "Drip pipe 16 mm"), ProductType.IRRIGATION),
        (ProductCategory.TOOLS_EQUIPMENT, ("Tools", "Khurpi"), ProductType.IMPLEMENT),
    ],
)
def test_the_finer_product_type_is_read_from_the_words(
    category: ProductCategory, hints: tuple[str, ...], expected: ProductType
) -> None:
    assert m.product_type_for(category, *hints) is expected


def test_only_agrochemicals_need_a_registration_number() -> None:
    assert m.needs_registration(ProductType.INSECTICIDE)
    assert m.needs_registration(ProductType.PGR)
    assert not m.needs_registration(ProductType.STRAIGHT_FERTILISER)
    assert not m.needs_registration(ProductType.HYBRID_SEED)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("₹1,250.00", Decimal("1250.00")),
        ("Rs. 1250/-", Decimal("1250.00")),
        ("Rs 1,00,000", Decimal("100000.00")),
        (1250, Decimal("1250.00")),
        (Decimal("12.5"), Decimal("12.50")),
        ("12.5", Decimal("12.50")),
        ("0", Decimal("0.00")),
        ("-5", None),
        ("abc", None),
        ("", None),
        (None, None),
        (True, None),
    ],
)
def test_prices_tolerate_currency_and_thousands_marks(raw: Any, expected: Decimal | None) -> None:
    assert m.parse_money(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("15 bags", 15),
        (12, 12),
        ("12.0", 12),
        (12.7, 12),
        ("1,200", 1200),
        ("-3", 0),
        ("", None),
        ("none", None),
        (None, None),
    ],
)
def test_quantities_are_whole_and_never_negative(raw: Any, expected: int | None) -> None:
    assert m.parse_quantity(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("yes", True),
        ("Y", True),
        ("1", True),
        (1, True),
        (True, True),
        ("हाँ", True),
        ("in stock", True),
        ("no", False),
        ("0", False),
        (0, False),
        ("out of stock", False),
        ("नहीं", False),
        ("maybe", None),
        (None, None),
    ],
)
def test_availability_flags_read_the_common_spellings(raw: Any, expected: bool | None) -> None:
    assert m.parse_flag(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("08:00", time(8, 0)),
        ("19:30:00", time(19, 30)),
        ("8 am", time(8, 0)),
        ("7 pm", time(19, 0)),
        ("12 am", time(0, 0)),
        ("12:15 p.m.", time(12, 15)),
        (timedelta(hours=9, minutes=30), time(9, 30)),
        (time(6, 45), time(6, 45)),
        ("25:00", None),
        ("soon", None),
        (None, None),
    ],
)
def test_opening_hours_read_clock_text_and_driver_values(raw: Any, expected: time | None) -> None:
    assert m.parse_time_of_day(raw) == expected


def test_small_fields_are_cleaned_and_keys_are_not_cut() -> None:
    assert m.clean_text("  Urea   45 kg \n", limit=240) == "Urea 45 kg"
    assert m.clean_text("", limit=10) is None
    assert m.clean_text("x" * 20, limit=10) == "x" * 10
    assert m.key_text(" ABC-1 ", limit=32) == "ABC-1"
    assert m.key_text("x" * 33, limit=32) is None
    assert m.parse_pincode("271 865") == "271865"
    assert m.parse_pincode("bad") is None
    assert m.parse_coordinate("27.87", limit=90) == Decimal("27.870000")
    assert m.parse_coordinate("181", limit=180) is None
    assert m.parse_staff_number("98765 40099") == "+919876540099"
    assert m.parse_staff_number("not a number") is None
    assert m.parse_staff_number(None) is None
