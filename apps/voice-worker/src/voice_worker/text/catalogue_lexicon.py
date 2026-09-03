"""Build the spoken-vocabulary lexicon from the catalogue (§5.5).

§5.5 asks for "every brand, product, active ingredient and crop name, ~500
terms" injected as STT keyterms, with a post-ASR fuzzy match against the same
list for whatever the recogniser still gets wrong.

Kept apart from :mod:`voice_worker.text.lexicon`, which is pure text matching
with no idea where words come from. That separation is what lets the matcher be
tested against hand-written entries, and it is why the database query lives
here instead.

**Loaded once per worker, not per call.** The catalogue is ~500 rows that do
not change mid-conversation, and §7.5 wants everything warm before the first
call rather than re-read inside a turn's latency budget.

What counts as a spoken form is broader than the product name. A farmer asks
for "बायर की दवा" (the brand), "इमिडाक्लोप्रिड" (the active ingredient) or
"गेहूँ वाली खाद" (the crop it is for) at least as often as they use the
catalogue's own wording -- so brands, ingredients and crop targets are all
variants of the entry they belong to.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import Brand, Category, District, Product

from .lexicon import Lexicon, LexiconEntry, Place

log = structlog.get_logger(__name__)


async def load_lexicon(session: AsyncSession) -> Lexicon:
    """Every product's spoken forms, indexed for matching.

    One query with an outer join to brands. A per-product brand lookup would be
    500 round trips at worker start, which is not a latency problem -- nothing
    is waiting -- but is the kind of thing that turns a 2 second boot into 30.
    """
    rows = (
        await session.execute(
            select(Product, Brand.name, Brand.name_hi, Category.slug)
            .outerjoin(Brand, Product.brand_id == Brand.id)
            .outerjoin(Category, Product.category_id == Category.id)
            .where(Product.deleted_at.is_(None))
            .order_by(Product.sku)
        )
    ).all()
    districts = (
        await session.execute(select(District.name, District.name_hi).order_by(District.name))
    ).all()

    entries: list[LexiconEntry] = []
    for product, brand_en, brand_hi, category_slug in rows:
        variants: set[str] = set()

        # The brand alone, and the brand with the product. Both are said.
        for brand in (brand_en, brand_hi):
            if brand:
                variants.add(brand)
                variants.add(f"{brand} {product.name_hi}")

        # Active ingredients: what an agronomist-advised farmer asks for by
        # name, and what appears on the label they are holding.
        variants.update(i for i in product.active_ingredients if i)

        # Crops are *not* spoken forms of a product. "potato" answers to a
        # seed, a fungicide and a foliar spray at once, and as a variant it
        # resolved to whichever came first by SKU. They travel on the entry
        # instead, where the direct-answer layer narrows by kind and crop.
        entries.append(
            LexiconEntry(
                sku=product.sku,
                name_hi=product.name_hi,
                name_en=product.name_en,
                variants=tuple(sorted(variants)),
                category=str(category_slug or ""),
                crops=tuple(c for c in product.crop_targets if c),
            )
        )

    places = tuple(Place(name_en=name, name_hi=name_hi or name) for name, name_hi in districts)
    lexicon = Lexicon.from_entries(entries, places=places)
    log.info(
        "lexicon.loaded",
        products=len(entries),
        spoken_forms=len(lexicon.keyterms()),
    )
    return lexicon


__all__ = ("load_lexicon",)
