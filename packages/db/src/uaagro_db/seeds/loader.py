"""Idempotent, deterministic seed loader.

Two properties this file exists to guarantee:

**Idempotent.** Running it twice produces the same database as running it once.
Every insert is keyed on a natural key and skipped if present, so `make db-seed`
is safe to run on every `make dev`.

**Deterministic.** The 200 generated farmers come from a fixed-seed RNG, so a
test that asserts on "the farmer in Barabanki with two bighas" keeps passing
across machines and across runs.

The loader refuses to run when ``APP_ENV`` is staging or production. This is
demo content with fictional prices and unapproved doses (KB §11); it has no
business anywhere near a real deployment.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_domain.enums import (
    ApprovalState,
    ConsentChannel,
    ConsentType,
    FlowType,
    FulfilmentMode,
    LandUnit,
    OrderStatus,
    ProductType,
    SpamAction,
    SpamRuleType,
)
from uaagro_domain.errors import ConfigurationError
from uaagro_domain.settings import Settings, get_defaults, get_settings

from ..crypto import PhoneCipher, build_cipher
from ..models import (
    AgentConfig,
    AnswerCache,
    Base,
    Brand,
    Category,
    Centre,
    ConsentRecord,
    Crop,
    CropProblem,
    CropRecommendation,
    District,
    Farmer,
    Inventory,
    Order,
    OrderItem,
    Organization,
    Product,
    ProductVariant,
    SpamRule,
    User,
    UserCentreAccess,
    VendorRate,
)
from ..passwords import hash_password
from . import data as seed_data
from . import prompts as seed_prompts

#: Fixed so generated farmers are identical on every machine and every run.
RNG_SEED = 20260831

#: 200 farmers (§21 Phase 1).
FARMER_COUNT = 200

#: Farmers given an order history. A slice rather than all 200: the point is to
#: have every order state represented, not to simulate a year of trade.
ORDER_FARMER_COUNT = 40

#: Walked in order so each state appears, rather than sampled -- a random draw
#: can leave a state unrepresented, and the untested state is the one that
#: breaks on a call.
ORDER_STATUS_CYCLE = (
    OrderStatus.PLACED,
    OrderStatus.CONFIRMED,
    OrderStatus.PACKED,
    OrderStatus.READY_FOR_PICKUP,
    OrderStatus.DISPATCHED,
    OrderStatus.DELIVERED,
    OrderStatus.CANCELLED,
)

#: Reserved block for generated demo numbers. Real Indian mobile numbers are
#: 10 digits starting 6-9; 9999xxxxxx is used here so a seeded number can never
#: collide with a genuine subscriber if a demo database is ever dialled from.
DEMO_PHONE_PREFIX = "9999"

T = TypeVar("T", bound=Base)


@dataclass(slots=True)
class SeedReport:
    """What the loader did, per table. Printed by ``make db-seed``."""

    created: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def note(self, table: str, *, created: bool) -> None:
        target = self.created if created else self.skipped
        target[table] = target.get(table, 0) + 1

    def summary(self) -> str:
        rows = []
        for table in sorted(set(self.created) | set(self.skipped)):
            rows.append(
                f"  {table:<24} created={self.created.get(table, 0):<5} "
                f"existing={self.skipped.get(table, 0)}"
            )
        return "\n".join(rows)

    @property
    def total_created(self) -> int:
        return sum(self.created.values())


async def seed_all(session: AsyncSession, *, settings: Settings | None = None) -> SeedReport:
    """Load every fixture. Safe to re-run."""
    settings = settings or get_settings()
    if settings.is_production:
        raise ConfigurationError(
            f"Refusing to seed demo data with APP_ENV={settings.app_env!r}.",
            remedy="Seeds carry fictional prices and unapproved doses. Import real "
            "catalogue and advisory data instead, and never run the seeder in a "
            "deployed environment.",
        )

    cipher = build_cipher(settings)
    report = SeedReport()

    org = await _seed_organization(session, report)
    districts = await _seed_districts(session, report)
    centres = await _seed_centres(session, report, org=org, districts=districts)
    users = await _seed_users(session, report, org=org, centres=centres)
    await _assign_centre_managers(session, centres=centres, users=users)

    brands = await _seed_brands(session, report)
    categories = await _seed_categories(session, report)
    variants = await _seed_products(session, report, org=org, brands=brands, categories=categories)
    await _seed_inventory(session, report, centres=centres, variants=variants)

    crops = await _seed_crops(session, report)
    problems = await _seed_problems(session, report, crops=crops)
    await _seed_recommendations(session, report, crops=crops, problems=problems, variants=variants)

    await _seed_vendor_rates(session, report)
    await _seed_spam_rules(session, report, org=org)
    await _seed_agent_configs(session, report, org=org)
    await _seed_answer_cache(session, report, org=org)
    await _seed_farmers(
        session, report, org=org, districts=districts, centres=centres, cipher=cipher
    )
    await _seed_orders(session, report, org=org, variants=variants)

    await session.flush()
    return report


# --------------------------------------------------------------------------- #
# Organisation, geography, staff
# --------------------------------------------------------------------------- #


async def _seed_organization(session: AsyncSession, report: SeedReport) -> Organization:
    existing = await session.scalar(
        select(Organization).where(Organization.name == seed_data.ORG_NAME)
    )
    if existing is not None:
        report.note("organizations", created=False)
        return existing
    org = Organization(
        name=seed_data.ORG_NAME,
        brand_name=seed_data.BRAND_NAME,
        timezone="Asia/Kolkata",
        settings={
            "seeded": True,
            # The last rung of the §12.3 transfer chain. Without it the chain
            # is only as deep as the centre's own data, and one busy line sends
            # every caller to a callback ticket.
            "helpline_transfer_number": f"+91{DEMO_PHONE_PREFIX}009999",
        },
    )
    session.add(org)
    await session.flush()
    report.note("organizations", created=True)
    return org


async def _seed_districts(session: AsyncSession, report: SeedReport) -> dict[str, District]:
    out: dict[str, District] = {}
    for entry in seed_data.DISTRICTS:
        existing = await session.scalar(
            select(District).where(District.name == entry.name, District.state == "Uttar Pradesh")
        )
        if existing is None:
            existing = District(
                name=entry.name,
                name_hi=entry.name_hi,
                state="Uttar Pradesh",
                code=entry.code,
                bigha_acres=entry.bigha_acres,
                bigha_verified=entry.verified,
            )
            session.add(existing)
            await session.flush()
            report.note("districts", created=True)
        else:
            report.note("districts", created=False)
        out[entry.name] = existing
    return out


async def _seed_centres(
    session: AsyncSession,
    report: SeedReport,
    *,
    org: Organization,
    districts: dict[str, District],
) -> dict[str, Centre]:
    out: dict[str, Centre] = {}
    for entry in seed_data.CENTRES:
        existing = await session.scalar(select(Centre).where(Centre.code == entry.code))
        if existing is None:
            district = districts[entry.district]
            existing = Centre(
                organization_id=org.id,
                code=entry.code,
                name=f"{seed_data.BRAND_NAME} - {entry.district}",
                name_hi=f"नवीन खुशहाली किसान सेवा केंद्र - {district.name_hi}",
                district_id=district.id,
                block=entry.block,
                address=f"{entry.block}, {entry.district}, Uttar Pradesh {entry.pincode}",
                address_spoken_hi=(
                    f"{district.name_hi} ज़िले में {entry.block} ब्लॉक में, मुख्य बाज़ार के पास"
                ),
                pincode=entry.pincode,
                latitude=entry.latitude,
                longitude=entry.longitude,
                open_time=time(8, 0),
                close_time=time(19, 0),
                working_days=["mon", "tue", "wed", "thu", "fri", "sat"],
                services_offered=list(entry.services),
                is_active=True,
            )
            session.add(existing)
            await session.flush()
            report.note("centres", created=True)
        else:
            report.note("centres", created=False)
        out[entry.code] = existing
    await _ensure_primary_centre(session, org=org, centres=out)
    return out


#: The head office. The helpline answers stock and price for this centre when
#: the caller's own centre is unknown (migration 0009).
PRIMARY_CENTRE_CODE = "NKSK-LKO-01"


async def _ensure_primary_centre(
    session: AsyncSession, *, org: Organization, centres: dict[str, Centre]
) -> None:
    """Mark the seed's head office primary when nothing is yet.

    Only when nothing is: an operator who moved the flag to another centre in
    the panel keeps their choice on the next ``make dev``, and the partial
    unique index would refuse a second primary anyway.
    """
    current = await session.scalar(
        select(Centre.id).where(
            Centre.organization_id == org.id,
            Centre.is_primary.is_(True),
            Centre.deleted_at.is_(None),
        )
    )
    head_office = centres.get(PRIMARY_CENTRE_CODE)
    if current is not None or head_office is None:
        return
    head_office.is_primary = True
    await session.flush()


async def _seed_users(
    session: AsyncSession,
    report: SeedReport,
    *,
    org: Organization,
    centres: dict[str, Centre],
) -> dict[str, User]:
    import os

    password = os.environ.get("UAAGRO_SEED_PASSWORD", seed_data.DEFAULT_SEED_PASSWORD)
    password_hash = hash_password(password)

    out: dict[str, User] = {}
    for entry in seed_data.USERS:
        existing = await session.scalar(select(User).where(User.email == entry.email))
        if existing is None:
            existing = User(
                organization_id=org.id,
                email=entry.email,
                full_name=entry.full_name,
                role=entry.role,
                password_hash=password_hash,
                # Staff need a reachable number or the manager rung of the
                # §12.3 chain is skipped in silence -- the transfer still
                # "works", one rung lower, and nobody notices until a manager
                # asks why they are never called.
                phone=f"+91{DEMO_PHONE_PREFIX}01{seed_data.USERS.index(entry):02d}",
                is_active=True,
            )
            session.add(existing)
            await session.flush()
            report.note("users", created=True)

            for code in entry.centres:
                session.add(
                    UserCentreAccess(
                        user_id=existing.id, centre_id=centres[code].id, access_level="write"
                    )
                )
                report.note("user_centre_access", created=True)
            await session.flush()
        else:
            report.note("users", created=False)
        out[entry.email] = existing
    return out


async def _assign_centre_managers(
    session: AsyncSession, *, centres: dict[str, Centre], users: dict[str, User]
) -> None:
    """Point each centre at a manager so the §12.3 transfer chain resolves."""
    assignment = {
        "NKSK-BBK-01": "barabanki.manager@uaagro.in",
        "NKSK-BBK-02": "barabanki.manager@uaagro.in",
        "NKSK-STP-01": "sitapur.manager@uaagro.in",
    }
    fallback = users["ops@uaagro.in"]
    for code, centre in centres.items():
        if centre.manager_user_id is not None:
            continue
        manager = users.get(assignment.get(code, ""), fallback)
        centre.manager_user_id = manager.id
        # A demo transfer target in the reserved range; never a real number.
        centre.transfer_number = f"+91{DEMO_PHONE_PREFIX}00{list(centres).index(code):02d}"
    await session.flush()


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


async def _seed_brands(session: AsyncSession, report: SeedReport) -> dict[str, Brand]:
    out: dict[str, Brand] = {}
    for entry in seed_data.BRANDS:
        existing = await session.scalar(select(Brand).where(Brand.name == entry.name))
        if existing is None:
            existing = Brand(
                name=entry.name, manufacturer=entry.manufacturer, is_partner=entry.is_partner
            )
            session.add(existing)
            await session.flush()
            report.note("brands", created=True)
        else:
            report.note("brands", created=False)
        out[entry.name] = existing
    return out


async def _seed_categories(session: AsyncSession, report: SeedReport) -> dict[str, Category]:
    out: dict[str, Category] = {}
    for category, name_hi, slug in seed_data.CATEGORIES:
        existing = await session.scalar(select(Category).where(Category.slug == slug))
        if existing is None:
            existing = Category(name=category, name_hi=name_hi, slug=slug)
            session.add(existing)
            await session.flush()
            report.note("categories", created=True)
        else:
            report.note("categories", created=False)
        out[category.value] = existing
    return out


async def _seed_products(
    session: AsyncSession,
    report: SeedReport,
    *,
    org: Organization,
    brands: dict[str, Brand],
    categories: dict[str, Category],
) -> dict[str, ProductVariant]:
    """Create products and one variant each. Returns variants keyed by SKU."""
    out: dict[str, ProductVariant] = {}
    for index, entry in enumerate(seed_data.PRODUCTS, start=1):
        product = await session.scalar(select(Product).where(Product.sku == entry.sku))
        if product is None:
            product = Product(
                organization_id=org.id,
                sku=entry.sku,
                name_en=entry.name_en,
                name_hi=entry.name_hi,
                brand_id=brands[entry.brand].id,
                category_id=categories[entry.category.value].id,
                product_type=entry.product_type,
                composition=[
                    {"ingredient": name, "percentage": pct} for name, pct in entry.composition
                ],
                active_ingredients=list(entry.active_ingredients),
                formulation=entry.formulation,
                crop_targets=list(entry.crop_targets),
                pest_targets=list(entry.pest_targets),
                cib_registration_no=seed_data.cib_for(entry, index),
                is_restricted=entry.is_restricted,
                requires_licence=entry.requires_licence,
                lexicon_variants=list(entry.lexicon),
                safety_notes_hi=(
                    "छिड़काव के समय दस्ताने और मास्क पहनें। खाली डिब्बा दोबारा इस्तेमाल न करें।"
                    if entry.product_type
                    in {
                        ProductType.INSECTICIDE,
                        ProductType.FUNGICIDE,
                        ProductType.HERBICIDE,
                        ProductType.PGR,
                    }
                    else None
                ),
            )
            session.add(product)
            await session.flush()
            report.note("products", created=True)
        else:
            report.note("products", created=False)

        variant = await session.scalar(
            select(ProductVariant).where(
                ProductVariant.product_id == product.id,
                ProductVariant.pack_size_value == entry.pack_value,
                ProductVariant.pack_size_unit == entry.pack_unit,
            )
        )
        if variant is None:
            variant = ProductVariant(
                product_id=product.id,
                pack_size_value=entry.pack_value,
                pack_size_unit=entry.pack_unit,
                mrp=entry.mrp,
                gst_rate=Decimal("5.00")
                if entry.category.value != "tools_equipment"
                else Decimal("18.00"),
            )
            session.add(variant)
            await session.flush()
            report.note("product_variants", created=True)
        else:
            report.note("product_variants", created=False)
        out[entry.sku] = variant
    return out


async def _seed_inventory(
    session: AsyncSession,
    report: SeedReport,
    *,
    centres: dict[str, Centre],
    variants: dict[str, ProductVariant],
) -> None:
    """Stock every product at every centre, with deterministic variation.

    A few deliberate stock-outs exist so the §KB 3.4 stock-out path (acknowledge,
    offer an alternative, give a restock date, offer a callback) has something
    to exercise in development.
    """
    rng = random.Random(RNG_SEED + 1)
    now = datetime.now(UTC)
    for centre in centres.values():
        for variant in variants.values():
            existing = await session.scalar(
                select(Inventory).where(
                    Inventory.centre_id == centre.id, Inventory.variant_id == variant.id
                )
            )
            if existing is not None:
                report.note("inventory", created=False)
                continue

            in_stock = rng.random() > 0.12
            qty = rng.randint(20, 400) if in_stock else 0
            # Selling price sits a little under MRP, as it does in practice.
            price = (variant.mrp * Decimal(str(rng.uniform(0.94, 1.0)))).quantize(Decimal("0.01"))
            session.add(
                Inventory(
                    centre_id=centre.id,
                    variant_id=variant.id,
                    qty_on_hand=qty,
                    qty_reserved=0,
                    selling_price=price,
                    is_available=in_stock,
                    restock_eta=None if in_stock else now + timedelta(days=rng.randint(3, 12)),
                )
            )
            report.note("inventory", created=True)
        await session.flush()


# --------------------------------------------------------------------------- #
# Crops and advisory
# --------------------------------------------------------------------------- #


async def _seed_crops(session: AsyncSession, report: SeedReport) -> dict[str, Crop]:
    out: dict[str, Crop] = {}
    for entry in seed_data.CROPS:
        existing = await session.scalar(select(Crop).where(Crop.name_en == entry.name_en))
        if existing is None:
            existing = Crop(
                name_en=entry.name_en,
                name_hi=entry.name_hi,
                season=entry.season,
                growth_stages=[
                    {"key": key, "name_hi": name_hi, "order": order}
                    for order, (key, name_hi) in enumerate(entry.stages)
                ],
                aliases=list(entry.aliases),
            )
            session.add(existing)
            await session.flush()
            report.note("crops", created=True)
        else:
            report.note("crops", created=False)
        out[entry.name_en] = existing
    return out


async def _seed_problems(
    session: AsyncSession, report: SeedReport, *, crops: dict[str, Crop]
) -> dict[str, CropProblem]:
    out: dict[str, CropProblem] = {}
    for entry in seed_data.PROBLEMS:
        existing = await session.scalar(
            select(CropProblem).where(
                CropProblem.name_en == entry.name_en, CropProblem.crop_id.is_(None)
            )
        )
        if existing is None:
            existing = CropProblem(
                crop_id=None,  # generic across crops; crop-specific rows come later
                problem_type=entry.problem_type,
                name_en=entry.name_en,
                name_hi=entry.name_hi,
                aliases=list(entry.aliases),
                requires_clarification=entry.requires_clarification,
            )
            session.add(existing)
            await session.flush()
            report.note("crop_problems", created=True)
        else:
            report.note("crop_problems", created=False)
        out[entry.key] = existing
    return out


async def _seed_recommendations(
    session: AsyncSession,
    report: SeedReport,
    *,
    crops: dict[str, Crop],
    problems: dict[str, CropProblem],
    variants: dict[str, ProductVariant],
) -> None:
    """Load the 40 recommendations **as drafts**.

    KB §5 and §9: no dose is served until an agronomist approves it. Seeding
    these as ``approved`` would defeat the single most important safety control
    in the platform, so they land in the approval queue instead -- and
    ``test_unapproved_advisory_is_unservable`` proves the agent cannot reach
    them.
    """
    crop_protection_skus = {
        entry.sku
        for entry in seed_data.PRODUCTS
        if entry.product_type
        in {ProductType.INSECTICIDE, ProductType.FUNGICIDE, ProductType.HERBICIDE, ProductType.PGR}
    }

    for entry in seed_data.RECOMMENDATIONS:
        crop = crops[entry.crop]
        variant = variants[entry.sku]
        existing = await session.scalar(
            select(CropRecommendation).where(
                CropRecommendation.crop_id == crop.id,
                CropRecommendation.product_variant_id == variant.id,
                CropRecommendation.growth_stage == entry.stage,
            )
        )
        if existing is not None:
            report.note("crop_recommendations", created=False)
            continue

        session.add(
            CropRecommendation(
                crop_id=crop.id,
                growth_stage=entry.stage,
                problem_type=problems[entry.problem].problem_type if entry.problem else None,
                problem_id=problems[entry.problem].id if entry.problem else None,
                product_variant_id=variant.id,
                dose_value=entry.dose_value,
                dose_unit=entry.dose_unit,
                dose_basis=entry.dose_basis,
                application_method=entry.method,
                timing_note_hi=entry.timing_hi,
                interval_days=entry.interval_days,
                max_applications=entry.max_applications,
                phi_days=entry.phi_days,
                precaution_note_hi=entry.precaution_hi,
                is_crop_protection=entry.sku in crop_protection_skus,
                season=crop.season,
                # Draft. Never approved by a seeder -- approval is an act by a
                # named agronomist, and the column records who and when.
                approval_state=ApprovalState.DRAFT,
                approved_by_user_id=None,
                approved_at=None,
            )
        )
        report.note("crop_recommendations", created=True)
    await session.flush()


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


async def _seed_vendor_rates(session: AsyncSession, report: SeedReport) -> None:
    """Seed the rate card from ``config/defaults.yaml`` (§8).

    ``verified_at`` is deliberately NULL: these come from the specification's
    published figures, not from a check against a vendor's price page. The §24
    verification job sets it, and the compliance dashboard shows the gap.
    """
    defaults = get_defaults()
    effective = date(2026, 1, 1)
    for entry in defaults.cost.seed_rates:
        existing = await session.scalar(
            select(VendorRate).where(
                VendorRate.vendor == entry.vendor,
                VendorRate.service == entry.service,
                VendorRate.unit == entry.unit,
                VendorRate.effective_from == effective,
            )
        )
        if existing is not None:
            report.note("vendor_rates", created=False)
            continue
        session.add(
            VendorRate(
                vendor=entry.vendor,
                service=entry.service,
                unit=entry.unit,
                rate=entry.rate,
                currency=entry.currency,
                effective_from=effective,
                verified_at=None,
            )
        )
        report.note("vendor_rates", created=True)
    await session.flush()


async def _seed_spam_rules(session: AsyncSession, report: SeedReport, *, org: Organization) -> None:
    for entry in seed_data.SPAM_RULES:
        rule_type = SpamRuleType(entry["rule_type"])
        existing = await session.scalar(
            select(SpamRule).where(
                SpamRule.organization_id == org.id, SpamRule.rule_type == rule_type
            )
        )
        if existing is not None:
            report.note("spam_rules", created=False)
            continue
        session.add(
            SpamRule(
                organization_id=org.id,
                rule_type=rule_type,
                action=SpamAction(entry["action"]),
                threshold=entry["threshold"],
                window_seconds=entry["window_seconds"],
                is_active=True,
            )
        )
        report.note("spam_rules", created=True)
    await session.flush()


async def _seed_agent_configs(
    session: AsyncSession, report: SeedReport, *, org: Organization
) -> None:
    defaults = get_defaults()
    language_routes: dict[str, Any] = {
        code: route.model_dump(mode="json") for code, route in defaults.language_routes.items()
    }
    llm_settings = defaults.llm.model_dump(mode="json")
    tts_settings = defaults.tts.model_dump(mode="json")

    configs = (
        (
            FlowType.INBOUND,
            "Inbound helpline v1",
            seed_prompts.INBOUND_SYSTEM_PROMPT,
            seed_prompts.INBOUND_GREETING,
            seed_prompts.INBOUND_CLOSING,
            list(seed_prompts.INBOUND_TOOL_ALLOWLIST),
        ),
        (
            FlowType.OUTBOUND,
            "Outbound offer campaign v1",
            seed_prompts.OUTBOUND_SYSTEM_PROMPT,
            seed_prompts.OUTBOUND_DISCLOSURE,
            seed_prompts.OUTBOUND_CLOSING,
            list(seed_prompts.OUTBOUND_TOOL_ALLOWLIST),
        ),
    )

    for flow, name, system_prompt, greeting, closing, allowlist in configs:
        existing = await session.scalar(
            select(AgentConfig).where(
                AgentConfig.organization_id == org.id,
                AgentConfig.flow_type == flow,
                AgentConfig.version == 1,
            )
        )
        if existing is not None:
            report.note("agent_configs", created=False)
            continue
        session.add(
            AgentConfig(
                organization_id=org.id,
                name=name,
                flow_type=flow,
                version=1,
                is_published=False,  # publishing is an audited act, not a seed
                system_prompt=system_prompt,
                greeting_template=greeting,
                closing_template=closing,
                tool_allowlist=allowlist,
                escalation_rules=seed_prompts.ESCALATION_RULES,
                language_routes=language_routes,
                llm_settings=llm_settings,
                tts_settings=tts_settings,
                guardrails=seed_prompts.GUARDRAILS,
                changelog="Seeded fixture. Edit and publish from the admin panel.",
            )
        )
        report.note("agent_configs", created=True)
    await session.flush()


async def _seed_answer_cache(
    session: AsyncSession, report: SeedReport, *, org: Organization
) -> None:
    for entry in seed_data.ANSWERS:
        existing = await session.scalar(
            select(AnswerCache).where(
                AnswerCache.organization_id == org.id,
                AnswerCache.intent_key == entry.intent_key,
                AnswerCache.language == "hi-IN",
            )
        )
        if existing is not None:
            report.note("answer_cache", created=False)
            continue
        session.add(
            AnswerCache(
                organization_id=org.id,
                intent_key=entry.intent_key,
                language="hi-IN",
                question_variants=list(entry.variants),
                answer_text=entry.answer_hi,
                # Audio is synthesised in Phase 2 and the key set then.
                audio_object_key=None,
                contains_volatile_data=entry.volatile,
                is_active=not entry.volatile,
            )
        )
        report.note("answer_cache", created=True)
    await session.flush()


# --------------------------------------------------------------------------- #
# Farmers
# --------------------------------------------------------------------------- #


async def _seed_farmers(
    session: AsyncSession,
    report: SeedReport,
    *,
    org: Organization,
    districts: dict[str, District],
    centres: dict[str, Centre],
    cipher: PhoneCipher,
) -> None:
    """Generate 200 deterministic farmers with encrypted phone numbers."""
    rng = random.Random(RNG_SEED)
    centre_list = list(centres.values())
    now = datetime.now(UTC)

    for index in range(FARMER_COUNT):
        # 9999xxxxxx: reserved demo range, never a real subscriber.
        phone = f"{DEMO_PHONE_PREFIX}{index:06d}"
        phone_hash = cipher.hash(phone)

        existing = await session.scalar(select(Farmer).where(Farmer.phone_hash == phone_hash))
        if existing is not None:
            report.note("farmers", created=False)
            continue

        # The English spelling, because the panel is read in English and a
        # farmer's name is data an operator types, searches and reads back --
        # not something the agent composes. The Hindi spelling stays in the
        # seed tables for anything that needs to speak it.
        first_en, _ = rng.choice(seed_data.FARMER_FIRST_NAMES)
        last_en, _ = rng.choice(seed_data.FARMER_SURNAMES)
        centre = rng.choice(centre_list)
        crops = rng.sample([c.name_en for c in seed_data.CROPS], k=rng.randint(1, 3))
        land_value = Decimal(str(round(rng.uniform(0.5, 12.0), 2)))
        slow_speaker = rng.random() < 0.15

        farmer = Farmer(
            organization_id=org.id,
            phone_hash=phone_hash,
            phone_enc=cipher.encrypt(phone),
            phone_last4=phone[-4:],
            full_name=f"{first_en} {last_en}",
            village=rng.choice(seed_data.VILLAGE_NAMES),
            district_id=centre.district_id,
            pincode=None,
            preferred_language="hi-IN",
            land_area_value=land_value,
            land_area_unit=rng.choice([LandUnit.BIGHA, LandUnit.ACRE]),
            primary_crops=crops,
            irrigation_type=rng.choice(seed_data.IRRIGATION_TYPES),
            soil_type=rng.choice(seed_data.SOIL_TYPES),
            assigned_centre_id=centre.id,
            farmer_segment=rng.choice(seed_data.FARMER_SEGMENTS),
            speech_profile={"slow_speaker": slow_speaker, "avg_asr_conf": None},
            first_seen_at=now - timedelta(days=rng.randint(30, 900)),
            last_contact_at=now - timedelta(days=rng.randint(0, 120)),
            tags=[],
        )
        session.add(farmer)
        await session.flush()
        report.note("farmers", created=True)

        # Roughly two thirds carry a live promotional-voice consent, so the
        # §13.1 compliance gate has both eligible and excluded contacts to
        # separate. Consent always has an expiry -- §18 makes it non-permanent.
        if rng.random() < 0.65:
            granted = now - timedelta(days=rng.randint(1, 80))
            session.add(
                ConsentRecord(
                    farmer_id=farmer.id,
                    consent_type=ConsentType.PROMOTIONAL_VOICE,
                    channel=ConsentChannel.IN_STORE,
                    granted_at=granted,
                    expires_at=granted + timedelta(days=90),
                    evidence={"source": "seed", "note": "fictional demo consent"},
                )
            )
            report.note("consent_records", created=True)

            # WhatsApp is a separate consent, not implied by the voice one
            # (§18): a farmer who agreed to a call has not agreed to a message.
            # Seeded on a subset so `send_whatsapp` has both a permitted and a
            # refused caller to exercise.
            if rng.random() < 0.7:
                session.add(
                    ConsentRecord(
                        farmer_id=farmer.id,
                        consent_type=ConsentType.PROMOTIONAL_WHATSAPP,
                        channel=ConsentChannel.WHATSAPP,
                        granted_at=granted,
                        expires_at=granted + timedelta(days=90),
                        evidence={"source": "seed", "note": "fictional demo consent"},
                    )
                )
                report.note("consent_records", created=True)

    await session.flush()


async def _seed_orders(
    session: AsyncSession,
    report: SeedReport,
    *,
    org: Organization,
    variants: dict[str, ProductVariant],
) -> None:
    """Recent orders for a slice of the farmer base.

    Enough to exercise ``get_order_status`` and §6.2's "last 3 orders" context
    block against real rows, and deliberately spread across the lifecycle: a
    dispatched order with a promised date, one waiting for pickup, one
    delivered, one still owing money. Every state the agent has to speak about
    differently is present, so a template that only handles the happy path
    fails here rather than on a call.
    """
    rng = random.Random(RNG_SEED + 1)
    now = datetime.now(UTC)
    variant_list = list(variants.values())

    farmers = list(
        (
            await session.scalars(
                select(Farmer).order_by(Farmer.phone_last4).limit(ORDER_FARMER_COUNT)
            )
        ).all()
    )

    for index, farmer in enumerate(farmers):
        if farmer.assigned_centre_id is None:
            continue
        for sequence in range(rng.randint(1, 3)):
            # Every random draw happens before the existence check, never after.
            # Skipping draws on the second run would shift the RNG stream for
            # every later farmer, so a re-seed would invent fresh order refs and
            # `make dev` would grow the table on each boot.
            status = ORDER_STATUS_CYCLE[(index + sequence) % len(ORDER_STATUS_CYCLE)]
            placed = now - timedelta(days=rng.randint(1, 60))
            lines = rng.sample(variant_list, k=rng.randint(1, 3))
            quantities = [rng.randint(1, 4) for _ in lines]
            promised_offset = rng.randint(2, 7)
            delivered_offset = rng.randint(2, 9)

            order_ref = f"UA-{index:04d}-{sequence}"
            existing = await session.scalar(select(Order).where(Order.order_ref == order_ref))
            if existing is not None:
                report.note("orders", created=False)
                for _ in lines:
                    report.note("order_items", created=False)
                continue

            priced = zip(lines, quantities, strict=True)
            total = sum((v.mrp * q for v, q in priced), start=Decimal("0"))

            order = Order(
                organization_id=org.id,
                order_ref=order_ref,
                farmer_id=farmer.id,
                centre_id=farmer.assigned_centre_id,
                status=status,
                mode=(
                    FulfilmentMode.DELIVERY
                    if status in (OrderStatus.DISPATCHED, OrderStatus.DELIVERED)
                    else FulfilmentMode.PICKUP
                ),
                placed_at=placed,
                # The constraints refuse a dispatched order with no promised
                # date and a delivered one with no timestamp, so both are set
                # from the status rather than sprinkled at random.
                promised_date=(
                    (placed + timedelta(days=promised_offset)).date()
                    if status
                    in (
                        OrderStatus.DISPATCHED,
                        OrderStatus.CONFIRMED,
                        OrderStatus.PACKED,
                        OrderStatus.READY_FOR_PICKUP,
                    )
                    else None
                ),
                delivered_at=(
                    placed + timedelta(days=delivered_offset)
                    if status is OrderStatus.DELIVERED
                    else None
                ),
                total_amount=total,
                amount_due=total if (index + sequence) % 4 == 0 else Decimal("0"),
                delivery_note=None,
            )
            session.add(order)
            await session.flush()
            report.note("orders", created=True)

            for variant, quantity in zip(lines, quantities, strict=True):
                session.add(
                    OrderItem(
                        order_id=order.id,
                        variant_id=variant.id,
                        quantity=quantity,
                        unit_price=variant.mrp,
                    )
                )
                report.note("order_items", created=True)

    await session.flush()
