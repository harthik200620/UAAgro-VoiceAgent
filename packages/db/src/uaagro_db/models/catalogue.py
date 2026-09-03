"""Product catalogue, per-centre inventory and price history (§10).

Everything the agent says about a product's availability or price comes from
here through a tool call, never from the model's own knowledge and never from a
vector index (§9 Tier 1, §23-1, §23-2).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uaagro_domain.enums import Formulation, ProductCategory, ProductType

from ..base import Base, StandardMixin, UserFacingMixin, enum_column, uuid_fk, uuid_pk


class Brand(Base, StandardMixin):
    __tablename__ = "brands"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    name_hi: Mapped[str | None] = mapped_column(String(160), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_partner: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class Category(Base, StandardMixin):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[ProductCategory] = enum_column(
        ProductCategory, constraint_name="category_name_valid"
    )
    name_hi: Mapped[str | None] = mapped_column(String(120), nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)


class Product(Base, UserFacingMixin):
    """A sellable product.

    ``lexicon_variants`` is the highest-leverage column in the catalogue
    (KB §3.1): it carries every way a farmer might *say* the product, so the
    post-ASR correction pass resolves यूरिया, urea, यूरीया and uria to one SKU.
    """

    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_products_name_hi_trgm",
            "name_hi",
            postgresql_using="gin",
            postgresql_ops={"name_hi": "gin_trgm_ops"},
        ),
        Index(
            "ix_products_name_en_trgm",
            "name_en",
            postgresql_using="gin",
            postgresql_ops={"name_en": "gin_trgm_ops"},
        ),
        Index("ix_products_category_type", "category_id", "product_type"),
        Index("ix_products_lexicon", "lexicon_variants", postgresql_using="gin"),
        # An agrochemical without a CIB&RC registration must not be sellable:
        # §16.2 forbids recommending an unregistered product, and it is cheaper
        # to refuse the row than to filter it on every query.
        CheckConstraint(
            "product_type NOT IN ('insecticide','fungicide','herbicide','pgr') "
            "OR cib_registration_no IS NOT NULL",
            name="agrochemical_needs_cib_registration",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = uuid_fk("organizations.id")
    sku: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    name_en: Mapped[str] = mapped_column(String(240), nullable=False)
    name_hi: Mapped[str] = mapped_column(String(240), nullable=False)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    category_id: Mapped[uuid.UUID] = uuid_fk("categories.id")
    product_type: Mapped[ProductType] = enum_column(
        ProductType, constraint_name="product_type_valid", index=True
    )

    #: ``[{ingredient, percentage, cas_no}]`` -- read as percentages of the
    #: named nutrient, never as bare digits (KB §3.3).
    composition: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    active_ingredients: Mapped[list[str]] = mapped_column(
        ARRAY(String(120)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    formulation: Mapped[Formulation | None] = enum_column(
        Formulation, constraint_name="formulation_valid", nullable=True, length=16
    )
    crop_targets: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )
    pest_targets: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )

    cib_registration_no: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), index=True
    )
    requires_licence: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    description_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_notes_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    safety_notes_hi: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Maintained by a trigger over name_en, name_hi, brand and ingredients.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    #: ASR spelling and pronunciation variants (KB §3.1, §10).
    lexicon_variants: Mapped[list[str]] = mapped_column(
        ARRAY(String(120)), nullable=False, server_default=text("ARRAY[]::varchar[]")
    )

    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    @property
    def agent_may_recommend(self) -> bool:
        """§16.2: restricted and licensed products route to a human instead."""
        return not (self.is_restricted or self.requires_licence)


class ProductVariant(Base, UserFacingMixin):
    """A pack size. Farmers buy बोरी and बोतल, not abstract products."""

    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("product_id", "pack_size_value", "pack_size_unit", name="uq_variant_pack"),
        CheckConstraint("pack_size_value > 0", name="pack_size_positive"),
        CheckConstraint("mrp >= 0", name="mrp_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = uuid_fk("products.id", ondelete="CASCADE")
    pack_size_value: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    pack_size_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gst_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("0")
    )

    product: Mapped[Product] = relationship(back_populates="variants")
    inventory: Mapped[list[Inventory]] = relationship(
        back_populates="variant", cascade="all, delete-orphan"
    )


class Inventory(Base, StandardMixin):
    """Live stock and price at one centre.

    §9: never cached, never retrieved by similarity. Read fresh on every turn
    that mentions availability or price.
    """

    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint("centre_id", "variant_id", name="uq_inventory_centre_variant"),
        Index(
            "ix_inventory_available",
            "centre_id",
            "variant_id",
            postgresql_where=text("is_available"),
        ),
        CheckConstraint("qty_on_hand >= 0", name="qty_on_hand_non_negative"),
        CheckConstraint("qty_reserved >= 0", name="qty_reserved_non_negative"),
        CheckConstraint("selling_price >= 0", name="selling_price_non_negative"),
        CheckConstraint(
            "discount_price IS NULL OR discount_price <= selling_price",
            name="discount_not_above_price",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    centre_id: Mapped[uuid.UUID] = uuid_fk("centres.id", ondelete="CASCADE")
    variant_id: Mapped[uuid.UUID] = uuid_fk("product_variants.id", ondelete="CASCADE")

    qty_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    qty_reserved: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    discount_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    restock_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    variant: Mapped[ProductVariant] = relationship(back_populates="inventory")

    @property
    def qty_sellable(self) -> int:
        return max(0, self.qty_on_hand - self.qty_reserved)

    def effective_price(self, at: datetime) -> Decimal:
        """Discount if it is live at ``at``, otherwise the selling price."""
        if self.discount_price is None:
            return self.selling_price
        if self.discount_valid_until is not None and self.discount_valid_until <= at:
            return self.selling_price
        return self.discount_price


class PriceHistory(Base, StandardMixin):
    """Append-only price trail, so a disputed quote can be reconstructed."""

    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_variant_time", "variant_id", "effective_from"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    variant_id: Mapped[uuid.UUID] = uuid_fk("product_variants.id", ondelete="CASCADE")
    centre_id: Mapped[uuid.UUID] = uuid_fk("centres.id", ondelete="CASCADE")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
