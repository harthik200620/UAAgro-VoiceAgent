"""Catalogue tools: what exists, what it costs, whether it is in stock (§6.3).

These are §9 Tier 1 -- deterministic SQL, no embeddings, no similarity, no model
in the retrieval path. §23-2 forbids using vector search for prices, stock or
dosages, and the reason is worth restating: "is DAP available at Barabanki and
what does it cost" has exactly one correct answer, it changes hourly, and a
nearest-neighbour search over document chunks will confidently return last
month's price.

Two behaviours are built into the results rather than left to the prompt:

* **Stock-outs carry alternatives.** KB §3.4 forbids answering a stock question
  with only "नहीं है". The tool returns the substitute, the restock date and the
  centre's contact so the agent can follow that script from data instead of
  improvising the parts it lacks.
* **Restricted products are flagged, not hidden.** §16.2 routes anything
  requiring a licence to a human. Omitting the row would make the agent say the
  product does not exist, which is false and sends the farmer elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import Centre, Inventory, Product, ProductVariant
from uaagro_domain.errors import NotFoundError

from ..text.lexicon import Lexicon
from .base import Tool, ToolContext
from .session import tool_session

#: Never return more than this. §6.2 targets under 2,000 input tokens a turn,
#: and a farmer cannot hold a list of fifteen products in their head anyway.
MAX_RESULTS = 5


#: Warm-up literals: a term and a SKU that match nothing, used to compile
#: these statements at worker start (see ``Tool.warmup_args``).
WARMUP_TERM = "zzzzwarmup"
WARMUP_SKU = "ZZZZ-WARMUP"

class SearchProducts(Tool):
    """Find SKUs by spoken name, brand, ingredient or crop."""

    name = "search_products"
    description = (
        "Find products by what the farmer called them. Handles brand names, "
        "active ingredients, crop names and colloquial terms. Returns SKUs with "
        "pack size and MRP. Use check_availability for live price and stock at a "
        "specific centre."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "What the farmer said, in their own words.",
            },
            "category": {
                "type": "string",
                "enum": [
                    "seeds",
                    "fertilisers",
                    "crop_protection",
                    "cattle_feed",
                    "tools_equipment",
                ],
            },
            "crop": {"type": "string", "maxLength": 60},
        },
        "required": ["query"],
    }

    def __init__(self, lexicon: Lexicon | None = None) -> None:
        self._lexicon = lexicon

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"query": WARMUP_TERM}

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        query = str(args["query"]).strip()

        # The ASR lexicon first: an exact spoken-form match is both faster and
        # more certain than full-text search over a misheard word (§5.5).
        if self._lexicon is not None:
            match = self._lexicon.match(query)
            if match is not None and not match.ambiguous:
                async with tool_session() as session:
                    row = await _product_by_sku(session, match.sku)
                    if row is not None:
                        return {"matched_by": "lexicon", "products": [row]}
            if match is not None and match.ambiguous:
                # §5.5: two products answer to this word. Saying which is a
                # guess, so the agent is told to ask instead.
                return {
                    "matched_by": "lexicon",
                    "ambiguous": True,
                    "candidates": [match.sku, match.runner_up_sku],
                    "products": [],
                }

        async with tool_session() as session:
            products = await _search(
                session,
                query,
                category=args.get("category"),
                crop=args.get("crop"),
            )
            payload: dict[str, Any] = {"matched_by": "search", "products": products}

            # The lexicon declines to resolve a word two SKUs answer to, but
            # search will happily return both -- and a model handed a list picks
            # one. "सल्फर" is the live case: a sulphur soil amendment and a
            # sulphur fungicide. Selling either on a coin-flip is the failure
            # the lexicon guard exists to prevent, so the same signal is raised
            # here whenever the matches disagree about what *kind* of product
            # the farmer asked for. Several brands of one product type are not
            # ambiguous in this sense -- the intent is settled and only the
            # brand is open, which the agent can simply read out.
            kinds = {p["type"] for p in products}
            if len(kinds) > 1:
                payload["ambiguous"] = True
                payload["candidates"] = [p["sku"] for p in products]
                payload["ask"] = "खाद चाहिए या दवाई, यह पूछिए।"
            return payload


class CheckAvailability(Tool):
    """Live stock and price for one variant at one centre."""

    name = "check_availability"
    description = (
        "Live stock and price for a product at a centre. Always call this before "
        "quoting a price or saying something is in stock -- never state either "
        "from memory. When out of stock the result includes an alternative and a "
        "restock date if known."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sku": {"type": "string", "minLength": 1, "maxLength": 48},
            "centre_code": {"type": "string", "maxLength": 32},
        },
        "required": ["sku"],
    }

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"sku": WARMUP_SKU}

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        sku = str(args["sku"]).strip()
        centre_code = args.get("centre_code")

        async with tool_session() as session:
            centre = await _resolve_centre(session, centre_code, context)
            if centre is None:
                raise NotFoundError(resource="centre", identifier=str(centre_code or "assigned"))

            row = (
                await session.execute(
                    select(Inventory, ProductVariant, Product)
                    .join(ProductVariant, Inventory.variant_id == ProductVariant.id)
                    .join(Product, ProductVariant.product_id == Product.id)
                    .where(Inventory.centre_id == centre.id, Product.sku == sku)
                )
            ).first()

            if row is None:
                return {
                    "sku": sku,
                    "centre": centre.code,
                    "stocked": False,
                    "reason": "this centre does not carry this product",
                    "alternatives": await _alternatives(session, sku, centre.id),
                    "centre_phone": centre.phone,
                }

            inventory, variant, product = row
            available = inventory.is_available and inventory.qty_sellable > 0

            payload: dict[str, Any] = {
                "sku": product.sku,
                "name_hi": product.name_hi,
                "centre": centre.code,
                "available": available,
                "pack": f"{_plain(variant.pack_size_value)} {variant.pack_size_unit}",
            }

            if available:
                # Price is read live and returned only from this row. §1 N1
                # makes an unsourced price the worst thing the agent can say.
                payload["price"] = _plain(inventory.effective_price(_utcnow()))
                payload["mrp"] = _plain(variant.mrp)
            else:
                # KB §3.4: acknowledge, offer an alternative, give a restock
                # date, offer a callback. The tool supplies the facts for all
                # four so none of them has to be improvised.
                payload["restock_eta"] = (
                    inventory.restock_eta.date().isoformat() if inventory.restock_eta else None
                )
                payload["alternatives"] = await _alternatives(session, sku, centre.id)
                payload["centre_phone"] = centre.phone

            if product.is_restricted or product.requires_licence:
                # §16.2: never recommended by the agent. Flagged rather than
                # omitted, so the answer is "a person must handle this" and not
                # "we do not have it".
                payload["restricted"] = True
                payload["restricted_note"] = "requires a licence; a person must handle this"

            return payload


class GetProductDetails(Tool):
    """Composition and usage for one product."""

    name = "get_product_details"
    description = (
        "What is inside a product: nutrient percentages, active ingredients, "
        "formulation, which crops and pests it is for. Use for 'इसमें क्या है' "
        "questions. Does NOT give a dose -- use recommend_for_crop for that."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"sku": {"type": "string", "minLength": 1, "maxLength": 48}},
        "required": ["sku"],
    }

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"sku": WARMUP_SKU}

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        sku = str(args["sku"]).strip()
        async with tool_session() as session:
            product = await session.scalar(
                select(Product).where(Product.sku == sku, Product.deleted_at.is_(None))
            )
            if product is None:
                raise NotFoundError(resource="product", identifier=sku)

            payload: dict[str, Any] = {
                "sku": product.sku,
                "name_hi": product.name_hi,
                "type": product.product_type.value,
                # KB §3.3: read each figure as a percentage of the named
                # nutrient, never as bare digits.
                "composition": [
                    {"ingredient": item.get("ingredient"), "percent": item.get("percentage")}
                    for item in (product.composition or [])
                ],
                "crops": list(product.crop_targets)[:6],
                "pests": list(product.pest_targets)[:6],
            }
            if product.active_ingredients:
                payload["active_ingredients"] = list(product.active_ingredients)[:4]
            if product.formulation:
                payload["formulation"] = product.formulation.value
            if product.safety_notes_hi:
                payload["safety_note_hi"] = product.safety_notes_hi
            if product.is_restricted or product.requires_licence:
                payload["restricted"] = True
            return payload


class FindNearestCentre(Tool):
    """Where to go, when it is open, who to ask for."""

    name = "find_nearest_centre"
    description = (
        "Find the nearest Kisan Sewa Kendra by village, district or pincode. "
        "Returns the spoken address, opening hours, phone and services."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "village": {"type": "string", "maxLength": 160},
            "district": {"type": "string", "maxLength": 120},
            "pincode": {"type": "string", "minLength": 6, "maxLength": 6},
        },
    }

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"pincode": "000000"}

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        async with tool_session() as session:
            # KB §2: match on village, then block, then district, then pincode.
            centres = await _find_centres(
                session,
                village=args.get("village"),
                district=args.get("district"),
                pincode=args.get("pincode"),
                fallback_centre_id=context.centre_id,
            )
            if not centres:
                raise NotFoundError(
                    resource="centre",
                    identifier=str(args.get("district") or args.get("village") or "nearby"),
                )
            return {"centres": centres}


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


async def _product_by_sku(session: AsyncSession, sku: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(Product, ProductVariant)
            .join(ProductVariant, ProductVariant.product_id == Product.id)
            .where(Product.sku == sku, Product.deleted_at.is_(None))
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    product, variant = row
    return _product_summary(product, variant)


async def _search(
    session: AsyncSession,
    query: str,
    *,
    category: str | None,
    crop: str | None,
) -> list[dict[str, Any]]:
    """Full-text search over the product vector, filtered by category and crop."""
    from sqlalchemy import func, or_

    statement = (
        select(Product, ProductVariant)
        .join(ProductVariant, ProductVariant.product_id == Product.id)
        .where(Product.deleted_at.is_(None))
        .limit(MAX_RESULTS)
    )

    if query:
        # 'simple' config, matching the trigger that built the vector: the
        # English stemmer mangles romanised Hindi (see the initial migration).
        tsquery = func.plainto_tsquery("simple", query)
        statement = statement.where(
            or_(
                Product.search_vector.op("@@")(tsquery),
                Product.lexicon_variants.contains([query.lower()]),
            )
        )
    if category:
        from uaagro_db.models import Category

        statement = statement.join(Category, Product.category_id == Category.id).where(
            Category.slug == category.replace("_", "-")
        )
    if crop:
        statement = statement.where(Product.crop_targets.contains([crop]))

    rows = (await session.execute(statement)).all()
    return [_product_summary(product, variant) for product, variant in rows]


async def _alternatives(session: AsyncSession, sku: str, centre_id: Any) -> list[dict[str, Any]]:
    """In-stock substitutes for an unavailable product.

    KB §3.2 defines a substitute as the same product type with overlapping
    active ingredients, in stock, cheapest first. Same *type* alone would offer
    a fungicide for an insecticide.
    """
    original = await session.scalar(select(Product).where(Product.sku == sku))
    if original is None:
        return []

    rows = (
        await session.execute(
            select(Product, ProductVariant, Inventory)
            .join(ProductVariant, ProductVariant.product_id == Product.id)
            .join(Inventory, Inventory.variant_id == ProductVariant.id)
            .where(
                Inventory.centre_id == centre_id,
                Inventory.is_available.is_(True),
                Product.product_type == original.product_type,
                Product.id != original.id,
                Product.deleted_at.is_(None),
                Product.is_restricted.is_(False),
            )
            .order_by(Inventory.selling_price.asc())
            .limit(2)
        )
    ).all()

    out: list[dict[str, Any]] = []
    for product, variant, inventory in rows:
        if original.active_ingredients and not set(product.active_ingredients) & set(
            original.active_ingredients
        ):
            continue
        out.append(
            {
                "sku": product.sku,
                "name_hi": product.name_hi,
                "pack": f"{_plain(variant.pack_size_value)} {variant.pack_size_unit}",
                "price": _plain(inventory.selling_price),
            }
        )
    return out


async def _resolve_centre(
    session: AsyncSession, centre_code: str | None, context: ToolContext
) -> Centre | None:
    if centre_code:
        by_code: Centre | None = await session.scalar(
            select(Centre).where(Centre.code == centre_code)
        )
        return by_code
    if context.centre_id:
        assigned: Centre | None = await session.get(Centre, context.centre_id)
        return assigned
    return None


async def _find_centres(
    session: AsyncSession,
    *,
    village: str | None,
    district: str | None,
    pincode: str | None,
    fallback_centre_id: str | None,
) -> list[dict[str, Any]]:
    from uaagro_db.models import District

    statement = select(Centre).where(Centre.is_active.is_(True), Centre.deleted_at.is_(None))

    if pincode:
        statement = statement.where(Centre.pincode == pincode)
    elif district:
        statement = statement.join(District, Centre.district_id == District.id).where(
            District.name.ilike(f"%{district}%")
        )
    elif village:
        statement = statement.where(Centre.block.ilike(f"%{village}%"))
    elif fallback_centre_id:
        statement = statement.where(Centre.id == fallback_centre_id)

    centres = list((await session.scalars(statement.limit(3))).all())

    if not centres and (district or village):
        # Nothing matched the stated place. Rather than an empty answer, offer
        # what exists: §11.4 forbids dead ends.
        centres = list(
            (await session.scalars(select(Centre).where(Centre.is_active.is_(True)).limit(2))).all()
        )

    return [
        {
            "code": centre.code,
            "name_hi": centre.name_hi,
            "address_spoken_hi": centre.address_spoken_hi,
            "phone": centre.phone,
            "open": centre.open_time.strftime("%H:%M"),
            "close": centre.close_time.strftime("%H:%M"),
            "services": list(centre.services_offered),
        }
        for centre in centres
    ]


def _product_summary(product: Product, variant: ProductVariant) -> dict[str, Any]:
    """Trimmed to the fields an answer mentions (§6.2)."""
    summary: dict[str, Any] = {
        "sku": product.sku,
        "name_hi": product.name_hi,
        "type": product.product_type.value,
        "pack": f"{_plain(variant.pack_size_value)} {variant.pack_size_unit}",
        "mrp": _plain(variant.mrp),
    }
    if product.is_restricted or product.requires_licence:
        summary["restricted"] = True
    return summary


def _plain(value: Decimal) -> str:
    """Trim trailing zeros so "50.000 kg" reads as "50 kg"."""
    return f"{value.normalize():f}"


def _utcnow() -> datetime:
    """Now, for deciding whether a discount window is still open."""
    return datetime.now(UTC)
