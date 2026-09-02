"""Centres, their managers, what they have in stock, and when a call is
handed to a person (§12.3, §15.1).

The centre list is what ``find_nearest_centre`` answers from and what the
warm transfer dials, so an edit here changes what the agent says on the next
call. A new centre needs a district -- created on the fly if the operator
names one the table does not have, with the state's default bigha factor and
a flag saying nobody has verified it -- and gets a code in the same shape as
the seeded ones.

Stock is per centre and per pack size (§15.1's inventory grid). The panel
shows the first few items on the centre row and the whole list behind it;
a toggle reaches the agent within the catalogue cache's five seconds.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from datetime import time as _time
from decimal import Decimal
from typing import Annotated, Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.audit import append_audit
from uaagro_db.models import Centre, District, Inventory, Organization, Product, ProductVariant
from uaagro_domain.enums import AuditAction, Role
from uaagro_domain.errors import NotFoundError, ValidationError
from uaagro_domain.phone import normalise_msisdn
from uaagro_domain.settings import get_defaults
from uaagro_domain.timezone import now_ist

from ..security.deps import DbDep, Principal, assert_centre_access, require_role

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["panel"])

#: Stock lines shown on the centre row itself. The full list is one click in.
STOCK_PREVIEW = 6
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class StockRow(BaseModel):
    inventoryId: str
    productName: str
    variantName: str
    price: float | None
    isAvailable: bool
    stockQty: int | None


class CentreRow(BaseModel):
    id: str
    code: str
    name: str
    nameHi: str | None
    district: str
    block: str | None
    state: str
    pincode: str | None
    latitude: float | None
    longitude: float | None
    managerName: str | None
    managerNumber: str | None
    phone: str | None
    openTime: str
    closeTime: str
    workingDays: list[str]
    isActive: bool
    openNow: bool
    stock: list[StockRow]


class CentreBody(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    nameHi: str | None = Field(default=None, max_length=200)
    district: str | None = Field(default=None, min_length=2, max_length=120)
    block: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=80)
    pincode: str | None = Field(default=None, max_length=6)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    managerName: str | None = Field(default=None, max_length=200)
    managerNumber: str | None = Field(default=None, max_length=20)
    phone: str | None = Field(default=None, max_length=20)
    openTime: str | None = None
    closeTime: str | None = None
    workingDays: list[str] | None = None
    isActive: bool | None = None


class StockPatch(BaseModel):
    isAvailable: bool | None = None
    price: float | None = Field(default=None, ge=0)
    stockQty: int | None = Field(default=None, ge=0)


class TransferReason(BaseModel):
    key: str
    label: str
    enabled: bool


class TransferRules(BaseModel):
    reasons: list[TransferReason]
    fallbackNumber: str | None
    ringTimeoutSeconds: int
    whisperSeconds: int


class TransferPatch(BaseModel):
    fallbackNumber: str | None = Field(default=None, max_length=20)


_REASON_LABELS = {
    "safety_emergency": (
        "A pesticide accident or poisoning — straight to the agronomist line, always"
    ),
    "explicit_request": "The farmer asks for a person or the manager",
    "abuse_or_anger": "The farmer is angry or abusive",
    "legal_or_dispute": "A complaint, a dispute or a refund",
    "repeated_misunderstanding": "The agent has not understood three times in a row",
    "low_recognition_confidence": "The line is too poor to understand the farmer",
    "negative_sentiment": "The farmer is clearly unhappy with the answers",
    "high_value_order": "A large order",
}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


async def _stock(
    db: AsyncSession, centre_ids: list[uuid.UUID], *, limit: int | None
) -> dict[uuid.UUID, list[StockRow]]:
    rows = (
        await db.execute(
            select(Inventory, ProductVariant, Product)
            .join(ProductVariant, ProductVariant.id == Inventory.variant_id)
            .join(Product, Product.id == ProductVariant.product_id)
            .where(Inventory.centre_id.in_(centre_ids), Product.deleted_at.is_(None))
            .order_by(Product.name_hi, ProductVariant.pack_size_value)
        )
    ).all()
    out: dict[uuid.UUID, list[StockRow]] = {}
    for stock, variant, product in rows:
        bucket = out.setdefault(stock.centre_id, [])
        if limit is not None and len(bucket) >= limit:
            continue
        bucket.append(
            StockRow(
                inventoryId=str(stock.id),
                productName=product.name_en or product.name_hi,
                variantName=f"{variant.pack_size_value:g} {variant.pack_size_unit}",
                price=float(stock.selling_price) if stock.selling_price is not None else None,
                isAvailable=bool(stock.is_available),
                stockQty=max(0, int(stock.qty_on_hand or 0) - int(stock.qty_reserved or 0)),
            )
        )
    return out


def _open_now(centre: Centre) -> bool:
    local = now_ist()
    day = _DAYS[local.weekday()]
    days = [d.lower()[:3] for d in (centre.working_days or [])]
    if days and day not in days:
        return False
    return centre.open_time <= local.time() <= centre.close_time


def _row(centre: Centre, district: District, stock: list[StockRow]) -> CentreRow:
    return CentreRow(
        id=str(centre.id),
        code=centre.code,
        name=centre.name,
        nameHi=centre.name_hi,
        district=district.name,
        block=centre.block,
        state=district.state,
        pincode=centre.pincode,
        latitude=float(centre.latitude) if centre.latitude is not None else None,
        longitude=float(centre.longitude) if centre.longitude is not None else None,
        managerName=centre.manager_name,
        managerNumber=centre.transfer_number,
        phone=centre.phone,
        openTime=centre.open_time.strftime("%H:%M"),
        closeTime=centre.close_time.strftime("%H:%M"),
        workingDays=[d.lower()[:3] for d in (centre.working_days or [])],
        isActive=bool(centre.is_active),
        openNow=bool(centre.is_active) and _open_now(centre),
        stock=stock,
    )


async def _centres(db: AsyncSession, centre_id: uuid.UUID | None = None) -> list[CentreRow]:
    statement = (
        select(Centre, District)
        .join(District, District.id == Centre.district_id)
        .where(Centre.deleted_at.is_(None))
        .order_by(District.name, Centre.code)
    )
    if centre_id is not None:
        statement = statement.where(Centre.id == centre_id)
    rows = (await db.execute(statement)).all()
    stock = await _stock(db, [centre.id for centre, _ in rows], limit=STOCK_PREVIEW)
    return [_row(centre, district, stock.get(centre.id, [])) for centre, district in rows]


@router.get("/centres", response_model=list[CentreRow])
async def centres(
    db: DbDep, _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)]
) -> list[CentreRow]:
    return await _centres(db)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


async def _district(db: AsyncSession, name: str, state: str | None) -> District:
    found = await db.scalar(
        select(District).where(func.lower(District.name) == name.strip().lower())
    )
    if found is not None:
        return found
    district = District(
        name=name.strip(),
        state=(state or "Uttar Pradesh").strip(),
        # The state's default until somebody measures. `bigha_verified` stays
        # false, which the dose calculator reads as "confirm with the farmer".
        bigha_acres=Decimal("0.625"),
        bigha_verified=False,
    )
    db.add(district)
    await db.flush()
    return district


async def _next_code(db: AsyncSession, district: District) -> str:
    stem = re.sub(r"[^A-Z]", "", district.name.upper())[:3] or "CTR"
    count = int(
        await db.scalar(
            select(func.count()).select_from(Centre).where(Centre.district_id == district.id)
        )
        or 0
    )
    return f"NKSK-{stem}-{count + 1:02d}"


def _parse_time(value: str | None, fallback: _time) -> _time:
    if not value:
        return fallback
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if match is None:
        raise ValidationError(
            f"{value!r} is not a time.", remedy="Use 24-hour HH:MM, for example 08:00."
        )
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ValidationError(f"{value!r} is not a time.", remedy="Use 24-hour HH:MM.")
    return _time(hours, minutes)


def _staff_number(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        return None
    try:
        return normalise_msisdn(value).e164
    except Exception:
        raise ValidationError(
            "That is not an Indian mobile number.",
            remedy="Ten digits, optionally with +91.",
        ) from None


def _days(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    cleaned = [v.strip().lower()[:3] for v in values]
    unknown = [d for d in cleaned if d not in _DAYS]
    if unknown:
        raise ValidationError(
            f"Unknown day {unknown[0]!r}.", remedy="Use mon, tue, wed, thu, fri, sat, sun."
        )
    return [d for d in _DAYS if d in cleaned]


@router.post("/centres", response_model=CentreRow, status_code=201)
async def create_centre(
    body: CentreBody,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CentreRow:
    if not body.name or not body.district:
        raise ValidationError(
            "A centre needs a name and a district.", remedy="Fill in both and save again."
        )
    district = await _district(db, body.district, body.state)
    centre = Centre(
        organization_id=principal.organization_id,
        code=await _next_code(db, district),
        name=body.name.strip(),
        name_hi=(body.nameHi or "").strip() or None,
        district_id=district.id,
        block=(body.block or "").strip() or None,
        pincode=(body.pincode or "").strip() or None,
        latitude=Decimal(str(body.latitude)) if body.latitude is not None else None,
        longitude=Decimal(str(body.longitude)) if body.longitude is not None else None,
        manager_name=(body.managerName or "").strip() or None,
        transfer_number=_staff_number(body.managerNumber),
        phone=_staff_number(body.phone),
        open_time=_parse_time(body.openTime, _time(8, 0)),
        close_time=_parse_time(body.closeTime, _time(19, 0)),
        working_days=_days(body.workingDays) or ["mon", "tue", "wed", "thu", "fri", "sat"],
        is_active=body.isActive if body.isActive is not None else True,
        created_by=principal.user_id,
    )
    db.add(centre)
    await db.flush()
    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="centre",
        resource_id=str(centre.id),
        actor_user_id=principal.user_id,
        after={"code": centre.code, "name": centre.name, "district": district.name},
    )
    return (await _centres(db, centre.id))[0]


@router.patch("/centres/{centre_id}", response_model=CentreRow)
async def update_centre(
    centre_id: uuid.UUID,
    body: CentreBody,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CentreRow:
    centre = await db.scalar(
        select(Centre).where(Centre.id == centre_id, Centre.deleted_at.is_(None))
    )
    if centre is None:
        raise NotFoundError(resource="centre", identifier=str(centre_id))
    before = {
        "name": centre.name,
        "manager_name": centre.manager_name,
        "transfer_number": centre.transfer_number,
        "is_active": centre.is_active,
        "hours": f"{centre.open_time}-{centre.close_time}",
    }
    if body.name:
        centre.name = body.name.strip()
    if body.nameHi is not None:
        centre.name_hi = body.nameHi.strip() or None
    if body.district:
        district = await _district(db, body.district, body.state)
        centre.district_id = district.id
    if body.block is not None:
        centre.block = body.block.strip() or None
    if body.pincode is not None:
        centre.pincode = body.pincode.strip() or None
    if body.latitude is not None:
        centre.latitude = Decimal(str(body.latitude))
    if body.longitude is not None:
        centre.longitude = Decimal(str(body.longitude))
    if body.managerName is not None:
        centre.manager_name = body.managerName.strip() or None
    if body.managerNumber is not None:
        centre.transfer_number = _staff_number(body.managerNumber)
    if body.phone is not None:
        centre.phone = _staff_number(body.phone)
    if body.openTime is not None:
        centre.open_time = _parse_time(body.openTime, centre.open_time)
    if body.closeTime is not None:
        centre.close_time = _parse_time(body.closeTime, centre.close_time)
    days = _days(body.workingDays)
    if days is not None:
        centre.working_days = days
    if body.isActive is not None:
        centre.is_active = body.isActive
    if centre.open_time >= centre.close_time:
        raise ValidationError("The centre would close before it opens.", remedy="Check the hours.")
    centre.updated_at = datetime.now(UTC)
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="centre",
        resource_id=str(centre.id),
        actor_user_id=principal.user_id,
        before=before,
        after={
            "name": centre.name,
            "manager_name": centre.manager_name,
            "transfer_number": centre.transfer_number,
            "is_active": centre.is_active,
            "hours": f"{centre.open_time}-{centre.close_time}",
        },
    )
    return (await _centres(db, centre.id))[0]


@router.get("/centres/{centre_id}/stock", response_model=list[StockRow])
async def centre_stock(
    centre_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> list[StockRow]:
    assert_centre_access(principal, centre_id)
    return (await _stock(db, [centre_id], limit=None)).get(centre_id, [])


@router.patch("/inventory/{inventory_id}", response_model=StockRow)
async def update_stock(
    inventory_id: uuid.UUID,
    body: StockPatch,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> StockRow:
    stock = await db.scalar(select(Inventory).where(Inventory.id == inventory_id))
    if stock is None:
        raise NotFoundError(resource="inventory", identifier=str(inventory_id))
    assert_centre_access(principal, stock.centre_id)
    before = {
        "is_available": stock.is_available,
        "selling_price": str(stock.selling_price),
        "qty_on_hand": stock.qty_on_hand,
    }
    if body.isAvailable is not None:
        stock.is_available = body.isAvailable
    if body.price is not None:
        stock.selling_price = Decimal(str(body.price)).quantize(Decimal("0.01"))
    if body.stockQty is not None:
        stock.qty_on_hand = body.stockQty
    stock.updated_by_user_id = principal.user_id
    stock.updated_at = datetime.now(UTC)
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="inventory",
        resource_id=str(stock.id),
        actor_user_id=principal.user_id,
        before=before,
        after={
            "is_available": stock.is_available,
            "selling_price": str(stock.selling_price),
            "qty_on_hand": stock.qty_on_hand,
        },
    )
    rows = (await _stock(db, [stock.centre_id], limit=None)).get(stock.centre_id, [])
    for row in rows:
        if row.inventoryId == str(stock.id):
            return row
    raise NotFoundError(resource="inventory", identifier=str(inventory_id))


# --------------------------------------------------------------------------- #
# Hand-over rules
# --------------------------------------------------------------------------- #


async def _organization(db: AsyncSession, principal: Principal) -> Organization:
    organization = await db.get(Organization, principal.organization_id)
    if organization is None:
        raise NotFoundError(resource="organization", identifier=str(principal.organization_id))
    return organization


def _rules(organization: Organization) -> TransferRules:
    escalation = get_defaults().escalation
    settings: dict[str, Any] = dict(organization.settings or {})
    return TransferRules(
        reasons=[
            TransferReason(
                key=key, label=_REASON_LABELS.get(key, key.replace("_", " ")), enabled=True
            )
            for key in escalation.reasons
        ],
        fallbackNumber=settings.get("transfer_fallback_number"),
        ringTimeoutSeconds=escalation.transfer_ring_timeout_s,
        whisperSeconds=escalation.whisper_max_s,
    )


@router.get("/transfer-rules", response_model=TransferRules)
async def transfer_rules(
    db: DbDep, principal: Annotated[Principal, require_role(Role.OPS_MANAGER)]
) -> TransferRules:
    return _rules(await _organization(db, principal))


@router.patch("/transfer-rules", response_model=TransferRules)
async def update_transfer_rules(
    body: TransferPatch,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> TransferRules:
    organization = await _organization(db, principal)
    settings = dict(organization.settings or {})
    before = settings.get("transfer_fallback_number")
    settings["transfer_fallback_number"] = _staff_number(body.fallbackNumber)
    organization.settings = settings
    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="organization",
        resource_id=str(organization.id),
        actor_user_id=principal.user_id,
        before={"transfer_fallback_number": before},
        after={"transfer_fallback_number": settings["transfer_fallback_number"]},
    )
    return _rules(organization)


__all__ = ("router",)
