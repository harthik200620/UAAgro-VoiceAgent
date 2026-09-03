"""SQLAlchemy models for the UA Agro voice platform (§10).

Importing this module registers every table on :data:`uaagro_db.base.Base`,
which is what Alembic autogenerate reflects against.
"""

from __future__ import annotations

from ..base import Base
from .advisory import (
    AnswerCache,
    Crop,
    CropProblem,
    CropRecommendation,
    KbChunk,
    KbDocument,
)
from .call import PARTITIONED_TABLES, Call, CallEvent, CallTurn, DtmfEvent
from .catalogue import Brand, Category, Inventory, PriceHistory, Product, ProductVariant
from .farmer import ConsentRecord, DndStatus, Farmer
from .ops import (
    AgentConfig,
    ApiKey,
    AuditLog,
    IdempotencyKey,
    NumberBlocklist,
    SpamRule,
    Ticket,
    VendorRate,
)
from .order import OPEN_STATES, Order, OrderItem
from .org import Centre, District, Organization, RefreshToken, User, UserCentreAccess
from .outbound import Campaign, CampaignContact, Offer, OfferVersion, WhatsAppMessage
from .sources import DataSource, DataSourceRun

#: Tables carrying Row-Level Security (§10). A centre manager sees only rows
#: for centres in their user_centre_access, enforced in Postgres rather than
#: only in the API -- application-layer scoping is one forgotten WHERE clause
#: away from a breach.
RLS_TABLES: tuple[str, ...] = (
    "calls",
    "call_turns",
    "farmers",
    "tickets",
    "inventory",
    # Not in §10 -- added with the orders tables, and scoped for the same
    # reason: an order carries a farmer's name, phone and purchase history.
    "orders",
)

__all__ = [
    "OPEN_STATES",
    "PARTITIONED_TABLES",
    "RLS_TABLES",
    "AgentConfig",
    "AnswerCache",
    "ApiKey",
    "AuditLog",
    "Base",
    "Brand",
    "Call",
    "CallEvent",
    "CallTurn",
    "Campaign",
    "CampaignContact",
    "Category",
    "Centre",
    "ConsentRecord",
    "Crop",
    "CropProblem",
    "CropRecommendation",
    "DataSource",
    "DataSourceRun",
    "District",
    "DndStatus",
    "DtmfEvent",
    "Farmer",
    "IdempotencyKey",
    "Inventory",
    "KbChunk",
    "KbDocument",
    "NumberBlocklist",
    "Offer",
    "OfferVersion",
    "Order",
    "OrderItem",
    "Organization",
    "PriceHistory",
    "Product",
    "ProductVariant",
    "RefreshToken",
    "SpamRule",
    "Ticket",
    "User",
    "UserCentreAccess",
    "VendorRate",
    "WhatsAppMessage",
]
