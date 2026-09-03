"""How the client's tables become ours: the field lists, and what a value means.

The mapping screen offers a fixed set of our fields per table, each pointing
at one of their columns. This module is the authority on that list, on what
a saved mapping must contain before a sync can run, and on reading the
values a shop's database actually holds: a pack size written as ``"50 kg
bag"``, a price written as ``"₹1,250.00"``, a category that says
``"Fertilizer"`` or ``"खाद"``. Every parser here is tolerant of format and
strict about meaning -- it returns ``None`` rather than guess, and the sync
reports the row instead of writing something it made up.

Nothing here touches a database, so all of it is tested without one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from uaagro_domain.enums import CROP_PROTECTION_TYPES, ProductCategory, ProductType
from uaagro_domain.errors import ValidationError
from uaagro_domain.phone import normalise_msisdn

#: Our fields per table, and whether the mapping must supply them. This is the
#: contract's list (docs/ADMIN_API.md, "Data -- the client's MySQL database as
#: a source") plus one optional product field the catalogue's own rules need:
#: an insecticide, fungicide, herbicide or growth regulator cannot be stored
#: without its CIB&RC registration number (§16.2), so a source that has the
#: number can map it, and a source that does not sees those rows reported
#: rather than silently dropped.
FIELDS: dict[str, dict[str, bool]] = {
    "stores": {
        "code": True,
        "name": True,
        "name_hi": False,
        "district": True,
        "block": False,
        "address": False,
        "pincode": False,
        "latitude": False,
        "longitude": False,
        "phone": False,
        "manager_name": False,
        "manager_phone": False,
        "open_time": False,
        "close_time": False,
    },
    "products": {
        "sku": True,
        "name": True,
        "name_hi": False,
        "category": True,
        "brand": False,
        "pack_size": True,
        "mrp": True,
        "description": False,
        "cib_registration_no": False,
    },
    "stock": {
        "store_code": True,
        "sku": True,
        "qty": True,
        "price": False,
        "is_available": False,
    },
}

TABLES: tuple[str, ...] = tuple(FIELDS)

#: A MySQL identifier as the operator may type it: ASCII letters, digits,
#: underscore and dollar, or anything from U+0080 up -- MySQL's own rule,
#: which is what lets a Devanagari column name through with its vowel
#: signs -- up to 64 characters. Anything else is refused before it can
#: reach a query, and the real names are checked against
#: ``information_schema`` again at fetch time.
_IDENTIFIER = re.compile(r"^[\w$\u0080-\uffff]{1,64}$")


class MappingError(ValidationError):
    """A mapping the sync could not run from. ``context`` names the fields."""


@dataclass(frozen=True, slots=True)
class TableMap:
    """One of their tables and which of its columns carry our fields."""

    table: str
    #: our field -> their column
    columns: dict[str, str]

    def as_json(self) -> dict[str, Any]:
        return {"table": self.table, "columns": dict(self.columns)}


@dataclass(frozen=True, slots=True)
class SourceMapping:
    stores: TableMap | None
    products: TableMap | None
    stock: TableMap | None

    @property
    def is_empty(self) -> bool:
        return self.stores is None and self.products is None and self.stock is None

    def as_json(self) -> dict[str, Any]:
        """Exactly the contract's ``SourceMapping`` shape, every key present."""
        return {
            name: (table_map.as_json() if table_map is not None else None)
            for name, table_map in (
                ("stores", self.stores),
                ("products", self.products),
                ("stock", self.stock),
            )
        }


def parse_mapping(raw: Mapping[str, Any] | None) -> SourceMapping:
    """Validate a mapping as the panel sends it and as the row stores it.

    A table may be absent or ``null`` (not mapped yet); one that is present
    must name a table and every required field, and may name nothing we do
    not have a field for. The error says which table and which fields.
    """
    if raw is None:
        return SourceMapping(stores=None, products=None, stock=None)
    if not isinstance(raw, Mapping):
        raise MappingError(
            "The mapping is not an object.", remedy="Send {stores, products, stock}."
        )
    unknown = sorted(str(key) for key in raw if key not in TABLES)
    if unknown:
        raise MappingError(
            f"Unknown mapping section {unknown[0]!r}.",
            remedy="The sections are stores, products and stock.",
            context={"unknown": unknown},
        )
    parsed = {name: _parse_table_map(name, raw.get(name)) for name in TABLES}
    return SourceMapping(
        stores=parsed["stores"], products=parsed["products"], stock=parsed["stock"]
    )


def _parse_table_map(name: str, raw: Any) -> TableMap | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise MappingError(
            f"The {name} mapping is not an object.",
            remedy="Send {table, columns}.",
            context={"table": name},
        )
    table = raw.get("table")
    if not isinstance(table, str) or not table.strip():
        raise MappingError(
            f"The {name} mapping does not name a table.",
            remedy="Pick the table from the list the connection test shows.",
            context={"table": name, "missing": ["table"]},
        )
    table = table.strip()
    if _IDENTIFIER.match(table) is None:
        raise MappingError(
            f"{table!r} is not a table name.",
            remedy="Pick the table from the list the connection test shows.",
            context={"table": name},
        )
    columns_raw = raw.get("columns")
    if columns_raw is None:
        columns_raw = {}
    if not isinstance(columns_raw, Mapping):
        raise MappingError(
            f"The {name} columns are not an object.",
            remedy="Send {ourField: theirColumn}.",
            context={"table": name},
        )
    fields = FIELDS[name]
    unknown = sorted(str(key) for key in columns_raw if key not in fields)
    if unknown:
        raise MappingError(
            f"{unknown[0]!r} is not a {name} field.",
            remedy=f"The {name} fields are: {', '.join(fields)}.",
            context={"table": name, "unknown": unknown},
        )
    columns: dict[str, str] = {}
    for field, column in columns_raw.items():
        if column is None or (isinstance(column, str) and not column.strip()):
            continue  # an optional field left unmapped, sent as empty
        if not isinstance(column, str) or _IDENTIFIER.match(column.strip()) is None:
            raise MappingError(
                f"The column for {field!r} is not a column name.",
                remedy="Pick a column from the table's column list.",
                context={"table": name, "field": str(field)},
            )
        columns[str(field)] = column.strip()
    missing = [field for field, required in fields.items() if required and field not in columns]
    if missing:
        raise MappingError(
            f"The {name} mapping needs {', '.join(missing)}.",
            remedy="Map every required field before saving.",
            context={"table": name, "missing": missing},
        )
    return TableMap(table=table, columns=columns)


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #

_WHITESPACE = re.compile(r"\s+")


def clean_text(value: Any, *, limit: int) -> str | None:
    """A trimmed, single-spaced string cut to the column's width, or None."""
    if value is None:
        return None
    if isinstance(value, bytes | bytearray):
        value = bytes(value).decode("utf-8", errors="replace")
    text = _WHITESPACE.sub(" ", str(value)).strip()
    if not text:
        return None
    return text[:limit]


def key_text(value: Any, *, limit: int) -> str | None:
    """A natural key: trimmed, and refused rather than cut when too long.

    A code clipped to fit would silently merge two different stores or
    products into one row, so a long key is a row to report, not to write.
    """
    text = clean_text(value, limit=limit + 1)
    if text is None or len(text) > limit:
        return None
    return text


def _unit_table(*groups: tuple[tuple[str, ...], str, int]) -> dict[str, tuple[str, Decimal]]:
    table: dict[str, tuple[str, Decimal]] = {}
    for spellings, unit, factor in groups:
        for spelling in spellings:
            table[spelling] = (unit, Decimal(factor))
    return table


#: Spellings of the pack-size units the catalogue stores, as they appear on
#: labels and in shop databases. Quintals and tonnes become kilograms.
_UNITS = _unit_table(
    (("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "किलो", "किग्रा"), "kg", 1),
    (("q", "qtl", "quintal", "quintals", "क्विंटल"), "kg", 100),
    (("t", "ton", "tons", "tonne", "tonnes", "mt"), "kg", 1000),
    (("g", "gm", "gms", "gram", "grams", "gramme", "ग्राम"), "g", 1),
    (("l", "lt", "ltr", "ltrs", "litre", "litres", "liter", "liters", "लीटर"), "L", 1),
    (("ml", "mls", "millilitre", "millilitres", "milliliter", "milliliters", "एमएल"), "ml", 1),
    (
        (
            "pc",
            "pcs",
            "piece",
            "pieces",
            "unit",
            "units",
            "no",
            "nos",
            "pkt",
            "packet",
            "packets",
            "bag",
            "bags",
            "bottle",
            "bottles",
            "box",
            "boxes",
            "set",
            "sets",
            "pair",
            "pairs",
            "नग",
            "पीस",
        ),
        "piece",
        1,
    ),
)

_PACK = re.compile(
    r"^\s*(?P<number>\d+(?:,\d{3})*(?:\.\d+)?)?"  # "50", "2.5", "2,500"
    r"\s*(?P<unit>[^\s\d.,()/x\u00d7-]+)?",  # the first word after it
    re.IGNORECASE,
)


def parse_pack_size(value: Any) -> tuple[Decimal, str] | None:
    """``"50 kg"`` -> ``(50, "kg")``, ``"500ml"`` -> ``(500, "ml")``, ``"1 bag"`` -> a piece.

    The number comes first and the first word after it is the unit; anything
    after that ("bag", "pack") is the container and is ignored. A bare unit
    word means one of them. No number and no known unit is not a pack size.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        number = Decimal(str(value))
        return (number.quantize(Decimal("0.001")), "piece") if number > 0 else None
    text = str(value).strip().lower()
    if not text:
        return None
    match = _PACK.match(text)
    if match is None:
        return None
    number_text, unit_text = match.group("number"), match.group("unit")
    unit = _UNITS.get((unit_text or "").strip(".").lower())
    if number_text is None and unit is None:
        return None
    if number_text is None:
        quantity = Decimal(1)
    else:
        try:
            quantity = Decimal(number_text.replace(",", ""))
        except InvalidOperation:
            return None
    if unit is None:
        # A number alone: "1", "10". The thing itself, counted.
        unit = ("piece", Decimal(1))
    scaled = (quantity * unit[1]).quantize(Decimal("0.001"))
    if scaled <= 0 or scaled >= Decimal("10000000"):
        return None
    return scaled, unit[0]


#: Category keywords, most specific first: a "seed drill" is a tool and a
#: "fungicide for seed treatment" is crop protection, so those are tried
#: before the word "seed" decides. Short keywords match whole words only.
_CATEGORY_KEYWORDS: tuple[tuple[ProductCategory, tuple[str, ...]], ...] = (
    (
        ProductCategory.TOOLS_EQUIPMENT,
        (
            "tool",
            "pump",
            "sprayer",
            "equipment",
            "implement",
            "drill",
            "machine",
            "औज़ार",
            "औजार",
            "यंत्र",
            "पंप",
            "स्प्रेयर",
        ),
    ),
    (
        ProductCategory.CROP_PROTECTION,
        (
            "pesticide",
            "insect",
            "fungi",
            "herbi",
            "weed",
            "crop protection",
            "crop_protection",
            "agrochem",
            "कीट",
            "फफूंद",
            "खरपतवार",
            "दवा",
        ),
    ),
    (ProductCategory.CATTLE_FEED, ("feed", "cattle", "pashu", "chara", "पशु", "चारा")),
    (
        ProductCategory.FERTILISERS,
        ("fertil", "khad", "urea", "dap", "npk", "manure", "nutrient", "खाद", "उर्वरक"),
    ),
    (ProductCategory.SEEDS, ("seed", "beej", "बीज")),
)


def _mentions(text: str, keyword: str) -> bool:
    if len(keyword) <= 3 and keyword.isascii():
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


def map_category(value: Any) -> ProductCategory | None:
    """Our category for whatever their category column says, or None."""
    text = clean_text(value, limit=200)
    if text is None:
        return None
    lowered = text.lower()
    for category in ProductCategory:
        if lowered in (category.value, category.value.replace("_", " ")):
            return category
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(_mentions(lowered, keyword) for keyword in keywords):
            return category
    return None


_TYPE_KEYWORDS: dict[ProductCategory, tuple[tuple[ProductType, tuple[str, ...]], ...]] = {
    ProductCategory.SEEDS: ((ProductType.HYBRID_SEED, ("hybrid", "हाइब्रिड")),),
    ProductCategory.FERTILISERS: (
        (ProductType.BIO_FERTILISER, ("bio", "जैव")),
        (ProductType.ORGANIC_MANURE, ("organic", "manure", "compost", "vermi", "गोबर")),
        (ProductType.MICRONUTRIENT, ("micro", "zinc", "boron", "sulphur", "sulfur", "iron")),
        (ProductType.NPK, ("npk", "dap", "complex", "10:26", "12:32", "20:20")),
    ),
    ProductCategory.CROP_PROTECTION: (
        (ProductType.FUNGICIDE, ("fungi", "फफूंद")),
        (ProductType.HERBICIDE, ("herbi", "weed", "खरपतवार")),
        (ProductType.PGR, ("pgr", "growth regulator", "growth promoter")),
    ),
    ProductCategory.TOOLS_EQUIPMENT: (
        (ProductType.SPRAYER, ("spray", "स्प्रेयर")),
        (ProductType.IRRIGATION, ("pump", "drip", "irrigat", "sprinkler", "pipe", "पंप")),
    ),
    ProductCategory.CATTLE_FEED: (),
}

_DEFAULT_TYPE: dict[ProductCategory, ProductType] = {
    ProductCategory.SEEDS: ProductType.CERTIFIED_SEED,
    ProductCategory.FERTILISERS: ProductType.STRAIGHT_FERTILISER,
    ProductCategory.CROP_PROTECTION: ProductType.INSECTICIDE,
    ProductCategory.CATTLE_FEED: ProductType.CATTLE_FEED,
    ProductCategory.TOOLS_EQUIPMENT: ProductType.IMPLEMENT,
}


def product_type_for(category: ProductCategory, *hints: Any) -> ProductType:
    """The catalogue's finer type, read from the category text and the name.

    The source only has to say "fertiliser"; ``"DAP 18:46"`` in the name is
    enough to file it as a complex fertiliser rather than a straight one.
    """
    text = " ".join(clean_text(hint, limit=300) or "" for hint in hints).lower()
    for product_type, keywords in _TYPE_KEYWORDS[category]:
        if any(_mentions(text, keyword) for keyword in keywords):
            return product_type
    return _DEFAULT_TYPE[category]


def needs_registration(product_type: ProductType) -> bool:
    """Whether the catalogue refuses this type without a CIB&RC number (§16.2)."""
    return product_type in CROP_PROTECTION_TYPES


_MONEY = re.compile(r"(-?)\s*(\d[\d,]*(?:\.\d+)?)")


def parse_money(value: Any) -> Decimal | None:
    """``"₹1,250.00"``, ``"Rs. 1250/-"``, ``1250`` -> ``Decimal("1250.00")``. Never negative."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        text = str(value)
        negative = text.startswith("-")
    else:
        match = _MONEY.search(str(value))
        if match is None:
            return None
        negative = bool(match.group(1))
        text = match.group(2).replace(",", "")
    if negative:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0 or amount >= Decimal("10000000000"):
        return None
    return amount.quantize(Decimal("0.01"))


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_quantity(value: Any) -> int | None:
    """A whole number of units on hand. Negative stock -- an unposted sale --
    reads as none on hand rather than as an error."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        text = str(value)
    else:
        match = _NUMBER.search(str(value).replace(",", ""))
        if match is None:
            return None
        text = match.group(0)
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return max(0, int(number))


_TRUE = {"1", "y", "yes", "true", "t", "available", "in stock", "instock", "हाँ", "हां", "उपलब्ध"}
_FALSE = {"0", "n", "no", "false", "f", "out", "out of stock", "unavailable", "नहीं", "नही"}


def parse_flag(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


_CLOCK = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.IGNORECASE
)


def parse_time_of_day(value: Any) -> time | None:
    """``"08:00"``, ``"8 am"``, ``"19:30:00"``, or the TIME a MySQL driver hands back."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        if seconds < 0 or seconds >= 86400:
            return None
        return time(seconds // 3600, (seconds % 3600) // 60)
    match = _CLOCK.match(str(value))
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()
    if meridiem == "pm" and hours < 12:
        hours += 12
    if meridiem == "am" and hours == 12:
        hours = 0
    if hours > 23 or minutes > 59:
        return None
    return time(hours, minutes)


def parse_coordinate(value: Any, *, limit: int) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or abs(number) > limit:
        return None
    return number.quantize(Decimal("0.000001"))


def parse_pincode(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 6 else None


def parse_staff_number(value: Any) -> str | None:
    """A centre or manager number in E.164, or None when it is not a mobile."""
    text = clean_text(value, limit=40)
    if text is None:
        return None
    try:
        return normalise_msisdn(text).e164
    except ValidationError:
        return None


__all__ = (
    "FIELDS",
    "TABLES",
    "MappingError",
    "SourceMapping",
    "TableMap",
    "clean_text",
    "key_text",
    "map_category",
    "needs_registration",
    "parse_coordinate",
    "parse_flag",
    "parse_mapping",
    "parse_money",
    "parse_pack_size",
    "parse_pincode",
    "parse_quantity",
    "parse_staff_number",
    "parse_time_of_day",
    "product_type_for",
)
