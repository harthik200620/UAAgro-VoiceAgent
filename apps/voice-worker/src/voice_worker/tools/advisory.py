"""Crop advisory and dosing (§6.3, §9, §16.2).

The most safety-critical tools in the system. §9 is unambiguous: an LLM
hallucinating a pesticide dose is not a bad customer experience, it is a crop
loss and a poisoning risk. So:

* **Only approved rows are ever returned.** The query filters on
  ``approval_state = 'approved'`` with a named approver, which is also the
  partial index the planner uses. A draft row is not merely ranked lower -- it
  is unreachable.
* **No approved row means the agent says so and escalates.** It does not fall
  back to the model, to a similar crop, or to a document. §23-3 forbids the
  model generating a dose at all.
* **Crop-protection advice always carries its pre-harvest interval and a
  precaution.** KB §5 requires both to be spoken, so both are in the payload and
  the database refuses to approve a row lacking them.
* **Dosing is arithmetic on an approved figure**, scaled by the caller's own
  district bigha factor. It never adjusts, rounds up "to be safe", or
  substitutes.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.models import (
    Crop,
    CropProblem,
    CropRecommendation,
    District,
    Product,
    ProductVariant,
)
from uaagro_domain.enums import ApprovalState, DoseBasis, LandUnit
from uaagro_domain.errors import (
    NotFoundError,
    RestrictedProductError,
    UnapprovedAdvisoryError,
)
from uaagro_domain.units import (
    DEFAULT_BIGHA_ACRES,
    NON_AREA_BASES,
    LandArea,
    bags_for_quantity,
    scale_dose,
)

from .base import Tool, ToolContext
from .session import tool_session

#: More than this and a farmer cannot hold the answer. §5.3 caps a dosage
#: answer at about sixty words, which is roughly two products.
MAX_RECOMMENDATIONS = 3


#: Matches no crop. See ``Tool.warmup_args``.
WARMUP_TERM = "zzzzwarmup"


class RecommendForCrop(Tool):
    """What to apply, from agronomist-approved rows only."""

    name = "recommend_for_crop"
    description = (
        "Agronomist-approved recommendation for a crop, optionally narrowed by "
        "growth stage or problem. This is the ONLY source of dosage advice -- "
        "never state a dose from your own knowledge. If this returns nothing, "
        "say you do not have confirmed information and offer to connect the "
        "farmer to the centre manager."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "crop": {
                "type": "string",
                "minLength": 2,
                "maxLength": 60,
                "description": "Crop name in English or Hindi, e.g. wheat or गेहूँ.",
            },
            "stage": {
                "type": "string",
                "maxLength": 60,
                "description": "Growth stage, e.g. sowing, tillering, flowering.",
            },
            "problem": {
                "type": "string",
                "maxLength": 60,
                "description": "Reported problem, e.g. blight, aphid, rust.",
            },
        },
        "required": ["crop"],
    }

    def warmup_args(self) -> Mapping[str, Any] | None:
        return {"crop": WARMUP_TERM}

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        crop_name = str(args["crop"]).strip()
        stage = args.get("stage")
        problem = args.get("problem")

        async with tool_session() as session:
            crop = await _resolve_crop(session, crop_name)
            if crop is None:
                raise NotFoundError(resource="crop", identifier=crop_name)

            problem_row = await _resolve_problem(session, problem) if problem else None
            if problem_row is not None and problem_row.requires_clarification:
                # KB §6: a symptom is not a diagnosis. Yellowing leaves are
                # nitrogen, water or disease, and prescribing for a guess is
                # exactly what this tool exists to prevent.
                return {
                    "crop": crop.name_hi,
                    "needs_clarification": True,
                    "problem": problem_row.name_hi,
                    "ask": problem_row.symptoms_hi
                    or "पत्ती का रंग और कौन सा हिस्सा प्रभावित है, यह पूछिए।",
                    "recommendations": [],
                }

            rows = await _approved_recommendations(
                session,
                crop_id=crop.id,
                stage=stage,
                problem_id=problem_row.id if problem_row else None,
            )

            if not rows:
                # §16.2: no approved row means the agent says so and escalates.
                # It does not widen the search, guess a similar crop, or defer
                # to the model.
                raise UnapprovedAdvisoryError(crop=crop.name_en, stage=stage)

            return {
                "crop": crop.name_hi,
                "stage": stage,
                "recommendations": rows,
                # KB §5: every crop-protection recommendation is spoken with
                # its pre-harvest interval and at least one precaution.
                "must_speak_precaution": any(r.get("precaution_hi") for r in rows),
            }


class CalculateDose(Tool):
    """Scale an approved dose to the farmer's actual plot."""

    name = "calculate_dose"
    description = (
        "Convert an approved per-acre or per-bigha dose to the farmer's plot "
        "size, and give the pack count. Answer in the unit the farmer used. "
        "Requires a recommendation id from recommend_for_crop -- never invent a "
        "dose to pass in."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "recommendation_id": {"type": "string", "minLength": 8, "maxLength": 64},
            "area": {"type": "number", "minimum": 0.01, "maximum": 10000},
            "unit": {"type": "string", "enum": ["bigha", "acre", "katha", "hectare"]},
            "district": {
                "type": "string",
                "maxLength": 120,
                "description": "Used to pick the local bigha factor, which varies by district.",
            },
        },
        "required": ["recommendation_id", "area", "unit"],
    }

    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        unit = LandUnit(str(args["unit"]))
        area_value = Decimal(str(args["area"]))

        async with tool_session() as session:
            recommendation = await _approved_by_id(session, str(args["recommendation_id"]))
            if recommendation is None:
                # Either the id is wrong or the row is not approved. Both mean
                # the same thing to the caller: no dose is available.
                raise UnapprovedAdvisoryError(crop="the requested recommendation")

            bigha_acres, verified = await _bigha_factor(session, args.get("district"))
            area = LandArea(
                value=area_value,
                unit=unit,
                bigha_acres=bigha_acres,
                bigha_assumed=not verified,
            )

            basis = DoseBasis(recommendation["dose_basis"])
            dose_value = Decimal(recommendation["dose_value"])

            if basis in NON_AREA_BASES:
                # Per-litre and per-plant figures are not area-scaled. §units
                # refuses to multiply them, and the agent states them as-is.
                return {
                    "recommendation_id": recommendation["id"],
                    "product_hi": recommendation["product_hi"],
                    "dose": f"{_plain(dose_value)} {recommendation['dose_unit']}",
                    "basis": basis.value,
                    "area_scaled": False,
                    "note": "यह मात्रा प्रति लीटर पानी की है, रकबे के हिसाब से नहीं।",
                    "phi_days": recommendation.get("phi_days"),
                    "precaution_hi": recommendation.get("precaution_hi"),
                }

            total = scale_dose(dose_value, basis, area)
            payload: dict[str, Any] = {
                "recommendation_id": recommendation["id"],
                "product_hi": recommendation["product_hi"],
                "area": f"{_plain(area_value)} {unit.value}",
                "total_dose": f"{_plain(total)} {recommendation['dose_unit']}",
                "phi_days": recommendation.get("phi_days"),
                "precaution_hi": recommendation.get("precaution_hi"),
                "timing_hi": recommendation.get("timing_hi"),
            }

            # KB §7: give the answer in the farmer's unit, then the pack count.
            pack_kg = recommendation.get("pack_size")
            if pack_kg and recommendation["dose_unit"] in ("kg", "g"):
                packs = bags_for_quantity(total, Decimal(str(pack_kg)))
                payload["packs"] = _plain(packs)
                payload["pack_size"] = (
                    f"{_plain(Decimal(str(pack_kg)))} {recommendation['pack_unit']}"
                )

            if not verified:
                # KB §7: the bigha varies by district and even tehsil. When the
                # factor is a platform default rather than a confirmed local
                # value, the agent must confirm before the dose is acted on.
                payload["confirm_bigha"] = True
                payload["confirm_ask"] = "आपके यहाँ बीघा कितने का होता है जी?"

            return payload


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


async def _resolve_crop(session: AsyncSession, name: str) -> Crop | None:
    """Match a crop by English name, Hindi name or a spoken alias."""
    lowered = name.strip().lower()
    crop: Crop | None = await session.scalar(select(Crop).where(Crop.name_en == lowered))
    if crop is not None:
        return crop
    crop = await session.scalar(select(Crop).where(Crop.name_hi == name.strip()))
    if crop is not None:
        return crop
    by_alias: Crop | None = await session.scalar(
        select(Crop).where(Crop.aliases.contains([lowered]))
    )
    return by_alias


async def _resolve_problem(session: AsyncSession, name: str) -> CropProblem | None:
    lowered = name.strip().lower()
    problem: CropProblem | None = await session.scalar(
        select(CropProblem).where(CropProblem.aliases.contains([lowered]))
    )
    if problem is not None:
        return problem
    by_name: CropProblem | None = await session.scalar(
        select(CropProblem).where(CropProblem.name_en.ilike(f"%{name.strip()}%"))
    )
    return by_name


async def _approved_recommendations(
    session: AsyncSession,
    *,
    crop_id: Any,
    stage: str | None,
    problem_id: Any | None,
) -> list[dict[str, Any]]:
    """Approved rows only.

    The filter mirrors ``ix_crop_reco_servable`` exactly, so this reads the
    partial index rather than scanning and discarding drafts.
    """
    statement = (
        select(CropRecommendation, Product, ProductVariant)
        .join(ProductVariant, CropRecommendation.product_variant_id == ProductVariant.id)
        .join(Product, ProductVariant.product_id == Product.id)
        .where(
            CropRecommendation.crop_id == crop_id,
            CropRecommendation.approval_state == ApprovalState.APPROVED,
            CropRecommendation.approved_by_user_id.is_not(None),
            CropRecommendation.deleted_at.is_(None),
        )
        .order_by(CropRecommendation.priority.asc())
        .limit(MAX_RECOMMENDATIONS)
    )
    if stage:
        statement = statement.where(CropRecommendation.growth_stage == stage)
    if problem_id is not None:
        statement = statement.where(CropRecommendation.problem_id == problem_id)

    rows = (await session.execute(statement)).all()
    return [_recommendation_summary(r, p, v) for r, p, v in rows]


async def _approved_by_id(session: AsyncSession, recommendation_id: str) -> dict[str, Any] | None:
    import uuid as _uuid

    try:
        parsed = _uuid.UUID(recommendation_id)
    except ValueError:
        return None

    row = (
        await session.execute(
            select(CropRecommendation, Product, ProductVariant)
            .join(ProductVariant, CropRecommendation.product_variant_id == ProductVariant.id)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(
                CropRecommendation.id == parsed,
                CropRecommendation.approval_state == ApprovalState.APPROVED,
                CropRecommendation.approved_by_user_id.is_not(None),
                CropRecommendation.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        return None
    recommendation, product, variant = row
    return _recommendation_summary(recommendation, product, variant)


async def _bigha_factor(session: AsyncSession, district_name: str | None) -> tuple[Decimal, bool]:
    """The local bigha, and whether it is a confirmed value.

    KB §7: the bigha is not fixed in Uttar Pradesh -- it varies by district and
    even by tehsil. Returning the confirmation flag alongside the number is
    what lets the agent ask rather than silently dose against an assumption.
    """
    if district_name:
        district = await session.scalar(
            select(District).where(District.name.ilike(f"%{district_name.strip()}%"))
        )
        if district is not None:
            return Decimal(str(district.bigha_acres)), bool(district.bigha_verified)
    return DEFAULT_BIGHA_ACRES, False


def _recommendation_summary(
    recommendation: CropRecommendation, product: Product, variant: ProductVariant
) -> dict[str, Any]:
    if not product.agent_may_recommend:
        # §16.2: a restricted or licensed product is never recommended by the
        # agent, even when an approved row exists for it.
        raise RestrictedProductError(sku=product.sku)

    summary: dict[str, Any] = {
        "id": str(recommendation.id),
        "product_hi": product.name_hi,
        "sku": product.sku,
        "dose_value": str(recommendation.dose_value),
        "dose_unit": recommendation.dose_unit,
        "dose_basis": recommendation.dose_basis.value,
        "pack_size": str(variant.pack_size_value),
        "pack_unit": variant.pack_size_unit,
    }
    if recommendation.application_method:
        summary["method"] = recommendation.application_method
    if recommendation.timing_note_hi:
        summary["timing_hi"] = recommendation.timing_note_hi
    if recommendation.phi_days is not None:
        summary["phi_days"] = recommendation.phi_days
    if recommendation.precaution_note_hi:
        summary["precaution_hi"] = recommendation.precaution_note_hi
    return summary


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"
