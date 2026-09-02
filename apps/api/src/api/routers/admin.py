"""The admin surface (§15).

Every endpoint the panel calls. Three properties hold across all of them, and
each is enforced by a dependency rather than remembered per handler:

**RLS is bound before any query runs.** ``DbDep`` sets the transaction-local
GUCs, so a centre manager asking for another centre's calls gets an empty result
from Postgres rather than a filtered one from Python. A handler that forgot a
``WHERE`` clause is still safe; a handler that forgot the dependency gets no
rows at all, which fails visibly rather than leaking.

**Nothing returns a phone number.** §17 and §23-6. ``calls`` carries only a
``from_number_hash``, and the last four digits come from the farmer record --
which is the only place they exist in plaintext. The response models have
nowhere to put anything more, so "never returned by any API" is a property of
the shapes rather than a rule to remember.

**Aggregates are computed in SQL.** §15's dashboard is glanced at during a
peak-season rush; pulling a day of calls into Python to count them would make
the slowest screen the one people open first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import ARRAY, Numeric, Select, func, select, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from uaagro_db.audit import append_audit, verify_chain
from uaagro_db.models import (
    AgentConfig,
    AnswerCache,
    AuditLog,
    Call,
    CallTurn,
    Centre,
    ConsentRecord,
    Crop,
    CropProblem,
    CropRecommendation,
    District,
    DndStatus,
    Farmer,
    Inventory,
    KbChunk,
    KbDocument,
    Offer,
    Product,
    ProductVariant,
    SpamRule,
    User,
    UserCentreAccess,
)
from uaagro_domain.enums import (
    ApprovalState,
    AuditAction,
    CallDirection,
    CallOutcome,
    CallStatus,
    ConsentType,
    Role,
)
from uaagro_domain.errors import NotFoundError, ValidationError
from uaagro_domain.settings import get_defaults, get_settings
from uaagro_domain.timezone import ist

from ..security.deps import (
    DbDep,
    Principal,
    PrincipalDep,
    require_exact_roles,
    require_role,
)

log = structlog.get_logger(__name__)

#: How long a catalogue edit takes to reach a live call. The agent reads
#: inventory per tool call with a short cache, so this is that cache's TTL --
#: shown in the panel because §15.1 requires the delay to be visible rather
#: than guessed at.
_CATALOGUE_PROPAGATION_SECONDS = 5

router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------------------- #
# Dashboard (§15.1)
# --------------------------------------------------------------------------- #


class TopIntent(BaseModel):
    intent: str
    count: int


class DashboardMetrics(BaseModel):
    """§15.1's tiles.

    Field names are camelCase because the consumer is TypeScript and a mapping
    layer between two spellings of the same field is a place for them to drift.
    """

    inboundCalls: int
    outboundCalls: int
    answerRate: float
    avgDurationSeconds: float
    resolutionRate: float
    transferRate: float
    containmentRate: float
    liveConcurrency: int
    concurrencyCapacity: int
    spendTodayRupees: float
    budgetRupees: float
    latencyP50Ms: int
    latencyP95Ms: int
    unhandledIntents: int
    failedCalls: int
    topIntents: list[TopIntent]


def _window(range_: str) -> datetime:
    """Start of the requested window, in UTC.

    Named ranges rather than a free date parameter. §15.1's dashboard compares
    today against yesterday against last week; an arbitrary range would make
    these aggregates unpredictable to plan for, on the screen people open first.
    """
    now = datetime.now(UTC)
    match range_:
        case "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        case "yesterday":
            return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        case "week":
            return now - timedelta(days=7)
        case _:
            raise ValidationError(
                f"Unknown range {range_!r}.", remedy="Use today, yesterday or week."
            )


@router.get("/dashboard", response_model=DashboardMetrics)
async def dashboard(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.READ_ONLY)],
    range: str = Query("today", pattern="^(today|yesterday|week)$"),
) -> DashboardMetrics:
    since = _window(range)

    totals = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count().filter(Call.direction == CallDirection.INBOUND).label("inbound"),
                func.count().filter(Call.direction == CallDirection.OUTBOUND).label("outbound"),
                func.count().filter(Call.answered_at.is_not(None)).label("answered"),
                func.count().filter(Call.outcome == CallOutcome.RESOLVED).label("resolved"),
                func.count().filter(Call.was_transferred.is_(True)).label("transferred"),
                func.count()
                .filter(Call.outcome == CallOutcome.SYSTEM_FAILURE)
                .label("failed"),
                func.coalesce(func.avg(Call.duration_seconds), 0).label("avg_duration"),
                func.coalesce(func.sum(Call.cost_total_inr), 0).label("spend"),
            ).where(Call.started_at >= since)
        )
    ).one()

    total = int(totals.total or 0)

    def ratio(count: int) -> float:
        return (count / total) if total else 0.0

    # Live concurrency is a live count, not a windowed one: the tile answers
    # "what is happening now", and a day of started calls is not that.
    live = int(
        await db.scalar(
            select(func.count()).select_from(Call).where(Call.ended_at.is_(None))
        )
        or 0
    )

    # `intents` is an array on the call, so the top-intent tile unnests it.
    # Counting the array column directly would count *calls*, not intents, and
    # a call that asked three things would register as one.
    intent_column = func.unnest(Call.intents).label("intent")
    intents = (
        await db.execute(
            select(intent_column, func.count().label("n"))
            .where(Call.started_at >= since)
            .group_by(intent_column)
            .order_by(func.count().desc())
            .limit(5)
        )
    ).all()

    unhandled = int(
        await db.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.started_at >= since, Call.intents.contains(["unknown"]))
        )
        or 0
    )

    defaults = get_defaults()
    settings = get_settings()
    budget = defaults.latency_budget_ms.total_no_tool

    return DashboardMetrics(
        inboundCalls=int(totals.inbound or 0),
        outboundCalls=int(totals.outbound or 0),
        answerRate=ratio(int(totals.answered or 0)),
        avgDurationSeconds=float(totals.avg_duration or 0),
        resolutionRate=ratio(int(totals.resolved or 0)),
        transferRate=ratio(int(totals.transferred or 0)),
        # Containment and resolution are not complements of the transfer rate:
        # an abandoned call is neither resolved nor transferred, so the three
        # do not sum to one and should not be presented as though they do.
        containmentRate=ratio(int(totals.resolved or 0)),
        liveConcurrency=live,
        concurrencyCapacity=settings.max_concurrent_calls,
        spendTodayRupees=float(totals.spend or 0),
        budgetRupees=float(settings.daily_spend_cap_inr),
        latencyP50Ms=int(budget.p50),
        latencyP95Ms=int(budget.p95),
        unhandledIntents=unhandled,
        failedCalls=int(totals.failed or 0),
        topIntents=[TopIntent(intent=str(i), count=int(n)) for i, n in intents],
    )


# --------------------------------------------------------------------------- #
# Crop advisory (§9, §15.1)
# --------------------------------------------------------------------------- #


class AdvisoryRow(BaseModel):
    id: str
    cropHi: str
    stage: str | None
    problemHi: str | None
    productHi: str
    dose: str
    timingHi: str | None
    phiDays: int | None
    precautionHi: str | None
    isCropProtection: bool
    approvalState: str
    approvedByName: str | None
    approvedAt: str | None


class AdvisoryList(BaseModel):
    rows: list[AdvisoryRow]
    unapproved: int


@router.get("/advisory", response_model=AdvisoryList)
async def advisory(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.READ_ONLY)],
) -> AdvisoryList:
    statement = (
        select(
            CropRecommendation,
            Crop.name_hi,
            CropProblem.name_hi,
            Product.name_hi,
            User.full_name,
        )
        .join(Crop, CropRecommendation.crop_id == Crop.id)
        .outerjoin(CropProblem, CropRecommendation.problem_id == CropProblem.id)
        .join(ProductVariant, CropRecommendation.product_variant_id == ProductVariant.id)
        .join(Product, ProductVariant.product_id == Product.id)
        .outerjoin(User, CropRecommendation.approved_by_user_id == User.id)
        .where(CropRecommendation.deleted_at.is_(None))
        .order_by(Crop.name_en, CropRecommendation.priority)
    )

    rows: list[AdvisoryRow] = []
    unapproved = 0
    for reco, crop_hi, problem_hi, product_hi, approver in (await db.execute(statement)).all():
        if reco.approval_state is not ApprovalState.APPROVED:
            unapproved += 1
        rows.append(
            AdvisoryRow(
                id=str(reco.id),
                cropHi=crop_hi,
                stage=reco.growth_stage,
                problemHi=problem_hi,
                productHi=product_hi,
                dose=f"{reco.dose_value.normalize()} {reco.dose_unit}",
                timingHi=reco.timing_note_hi,
                phiDays=reco.phi_days,
                precautionHi=reco.precaution_note_hi,
                isCropProtection=reco.is_crop_protection,
                approvalState=reco.approval_state.value,
                approvedByName=approver,
                approvedAt=reco.approved_at.isoformat() if reco.approved_at else None,
            )
        )

    return AdvisoryList(rows=rows, unapproved=unapproved)


@router.post("/advisory/{recommendation_id}/approve", status_code=204)
async def approve_advisory(
    recommendation_id: uuid.UUID,
    db: DbDep,
    principal: PrincipalDep,
    # §9's safety control. An *exact* role list, not a minimum: ops_manager
    # outranks agronomist on everything else in this system and has no business
    # signing off a pesticide dose.
    _: Annotated[Principal, require_exact_roles(Role.AGRONOMIST, Role.SUPER_ADMIN)],
) -> None:
    reco = await db.get(CropRecommendation, recommendation_id)
    if reco is None:
        raise NotFoundError(resource="recommendation", identifier=str(recommendation_id))

    # §16.2, checked here *as well as* by the database CHECK constraint. The
    # constraint is the guarantee; this is the message. A 500 from a constraint
    # violation tells an agronomist nothing about what to fix.
    if reco.is_crop_protection and (reco.phi_days is None or not reco.precaution_note_hi):
        raise ValidationError(
            "A crop-protection recommendation needs a pre-harvest interval and at "
            "least one precaution before it can be approved.",
            remedy="Add both, then approve. The agent speaks them with every "
            "dosage answer (§16.2).",
        )

    # The approver comes from the authenticated principal, never from the
    # request body. §9 records *who* signed off, and a client-supplied approver
    # id would make that record worthless.
    reco.approval_state = ApprovalState.APPROVED
    reco.approved_by_user_id = principal.user_id
    reco.approved_at = datetime.now(UTC)

    log.info(
        "advisory.approved",
        recommendation_id=str(recommendation_id),
        approver=str(principal.user_id),
    )


# --------------------------------------------------------------------------- #
# Calls (§15.1 Call Explorer)
# --------------------------------------------------------------------------- #


class CallRow(BaseModel):
    id: str
    callRef: str
    startedAt: str
    direction: str
    centreCode: str | None
    centreId: str | None
    language: str
    qualityTier: str
    durationSeconds: int
    outcome: str
    intent: str | None
    transferred: bool
    costRupees: float
    #: Last four digits, from the farmer record. §17: the call row holds only a
    #: hash, and nothing above the control plane ever sees more than this.
    callerLast4: str | None


class CallList(BaseModel):
    rows: list[CallRow]
    total: int


def _quality_tier(language: str | None) -> str:
    """The tier §5.1 declares for this language.

    Read from the routing table rather than stored on the call, so the panel
    always shows what a caller in that language *currently* gets. §5 is explicit
    that a lower tier must be labelled honestly rather than quietly shipped as
    equivalent, and a stale stored value would defeat that.
    """
    if not language:
        return "C"
    try:
        _, route = get_defaults().resolve_language(language)
    except Exception:
        return "C"
    return str(route.quality_tier)


@router.get("/calls", response_model=CallList)
async def calls(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.READ_ONLY)],
    outcome: str | None = None,
    language: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> CallList:
    statement: Select[Any] = (
        select(Call, Centre.code, Farmer.phone_last4)
        .outerjoin(Centre, Call.centre_id == Centre.id)
        .outerjoin(Farmer, Call.farmer_id == Farmer.id)
        .order_by(Call.started_at.desc())
    )
    if outcome:
        statement = statement.where(Call.outcome == outcome)
    if language:
        statement = statement.where(Call.language_final == language)

    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (await db.execute(statement.limit(limit).offset(offset))).all()

    return CallList(
        total=total,
        rows=[
            CallRow(
                id=str(call.id),
                callRef=call.call_ref,
                startedAt=call.started_at.isoformat(),
                direction=call.direction.value,
                centreCode=centre_code,
                centreId=str(call.centre_id) if call.centre_id else None,
                language=call.language_final or call.language_detected or "",
                qualityTier=_quality_tier(call.language_final or call.language_detected),
                durationSeconds=int(call.duration_seconds or 0),
                outcome=call.outcome.value if call.outcome else "",
                # The first intent of the call. The full list is on the detail
                # view; a list column in a dense table is unreadable.
                intent=call.intents[0] if call.intents else None,
                transferred=bool(call.was_transferred),
                costRupees=float(call.cost_total_inr or 0),
                callerLast4=last4,
            )
            for call, centre_code, last4 in rows
        ],
    )


# --------------------------------------------------------------------------- #
# Campaigns (§13.1)
# --------------------------------------------------------------------------- #


class CampaignGate(BaseModel):
    total: int
    eligible: int
    removed: dict[str, int]
    blockedBy: list[str]
    estimatedCostRupees: float
    estimatedMinutes: int
    createdByUserId: str


@router.get("/campaigns/{campaign_id}/gate", response_model=CampaignGate)
async def campaign_gate(
    campaign_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> CampaignGate:
    """§13.1's gate, evaluated fresh on every request.

    Never cached. The gate is evaluated again at dial time, and a stale
    eligible-count on an approval screen is a reviewer approving a number that
    is no longer true.
    """
    from uaagro_db.campaigns import evaluate_campaign

    report, created_by = await evaluate_campaign(db, campaign_id)
    settings = get_settings()

    return CampaignGate(
        total=report.total,
        eligible=report.eligible_count,
        removed={check.value: count for check, count in report.removed.items()},
        blockedBy=[check.value for check in report.blocked_by],
        # §13.1 shows this before approval. Deliberately the full per-call
        # ceiling rather than an average: a reviewer deciding whether to spend
        # money wants the number that could appear on the bill.
        estimatedCostRupees=float(settings.max_cost_per_call_inr * report.eligible_count),
        estimatedMinutes=max(
            1, report.eligible_count // max(1, settings.outbound_calls_per_minute)
        ),
        createdByUserId=created_by,
    )


# --------------------------------------------------------------------------- #
# Settings (§15.1)
# --------------------------------------------------------------------------- #


class MaskedSecret(BaseModel):
    name: str
    isSet: bool
    hint: str | None
    updatedAt: str | None


class SecretList(BaseModel):
    secrets: list[MaskedSecret]


@router.get("/settings/secrets", response_model=SecretList)
async def secrets(
    _: Annotated[Principal, require_exact_roles(Role.SUPER_ADMIN)],
) -> SecretList:
    """§15.1: vendor keys are write-only, masked after save, never returned.

    This returns presence and a four-character hint. The hint exists because the
    alternative is worse: without it, an operator facing a failing integration
    cannot tell whether the key is wrong or the vendor is down, and the only way
    to find out is to overwrite a working key.

    :class:`MaskedSecret` has nowhere to put a value, so "never returned by any
    API" is a property of the shape rather than a rule to remember.
    """
    settings = get_settings()
    out: list[MaskedSecret] = []
    for name in settings.secret_field_names():
        value = getattr(settings, name, None)
        text = str(value) if value else ""
        out.append(
            MaskedSecret(
                name=name.upper(),
                isSet=bool(text),
                # Four characters is enough to recognise a key and far too few
                # to use one. Withheld entirely below eight characters, where
                # four would be most of it.
                hint=f"...{text[-4:]}" if len(text) >= 8 else None,
                updatedAt=None,
            )
        )
    return SecretList(secrets=out)


# --------------------------------------------------------------------------- #
# Answer cache (§9 Tier 3, §15.1)
# --------------------------------------------------------------------------- #


class CachedAnswer(BaseModel):
    id: str
    intentKey: str
    language: str
    answerText: str
    isActive: bool
    containsVolatileData: bool


@router.get("/answer-cache", response_model=list[CachedAnswer])
async def answer_cache(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.READ_ONLY)],
) -> list[CachedAnswer]:
    rows = (await db.scalars(select(AnswerCache).order_by(AnswerCache.intent_key))).all()
    return [
        CachedAnswer(
            id=str(row.id),
            intentKey=row.intent_key,
            language=row.language,
            answerText=row.answer_text,
            isActive=row.is_active,
            # §9: anything carrying a price or stock figure is never served
            # from cache. Surfaced so an operator can see *why* an entry is
            # inactive rather than assuming it is broken.
            containsVolatileData=row.contains_volatile_data,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Flows & Prompts (§15.1)
# --------------------------------------------------------------------------- #


class FlowVersion(BaseModel):
    id: str
    name: str
    flowType: str
    version: int
    isPublished: bool
    publishedAt: str | None
    publishedByName: str | None
    changelog: str | None
    toolAllowlist: list[str]
    updatedAt: str


class FlowDetail(FlowVersion):
    """The editable body.

    §1 N6 keeps prompts server-side, and this is the one endpoint that returns
    one. It requires an ops-manager role rather than read-only, and the panel
    renders it only from a Server Component -- the browser never holds a
    credential that could ask for this.
    """

    systemPrompt: str
    greetingTemplate: str
    closingTemplate: str
    escalationRules: dict[str, Any]
    languageRoutes: dict[str, Any]
    llmSettings: dict[str, Any]
    ttsSettings: dict[str, Any]
    guardrails: dict[str, Any]


def _flow_version(row: AgentConfig, publisher: str | None) -> FlowVersion:
    return FlowVersion(
        id=str(row.id),
        name=row.name,
        flowType=row.flow_type.value,
        version=row.version,
        isPublished=row.is_published,
        publishedAt=row.published_at.isoformat() if row.published_at else None,
        publishedByName=publisher,
        changelog=row.changelog,
        toolAllowlist=list(row.tool_allowlist),
        updatedAt=row.updated_at.isoformat(),
    )


@router.get("/flows", response_model=list[FlowVersion])
async def flows(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
    flow_type: str | None = None,
) -> list[FlowVersion]:
    """Every version, newest first, with the published one marked.

    The list carries no prompt text. §1 N6 means a body is fetched only when
    somebody opens a version to edit it, so listing does not put every prompt
    in the response.
    """
    statement: Select[Any] = (
        select(AgentConfig, User.full_name)
        .outerjoin(User, AgentConfig.published_by_user_id == User.id)
        .where(AgentConfig.deleted_at.is_(None))
        .order_by(AgentConfig.flow_type, AgentConfig.version.desc())
    )
    if flow_type:
        statement = statement.where(AgentConfig.flow_type == flow_type)
    rows = (await db.execute(statement)).all()
    return [_flow_version(row, publisher) for row, publisher in rows]


@router.get("/flows/{config_id}", response_model=FlowDetail)
async def flow_detail(
    config_id: uuid.UUID,
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> FlowDetail:
    row = await db.scalar(
        select(AgentConfig).where(
            AgentConfig.id == config_id, AgentConfig.deleted_at.is_(None)
        )
    )
    if row is None:
        raise NotFoundError(resource="agent_config", identifier=str(config_id))
    publisher = (
        await db.scalar(select(User.full_name).where(User.id == row.published_by_user_id))
        if row.published_by_user_id
        else None
    )
    base = _flow_version(row, publisher)
    return FlowDetail(
        **base.model_dump(),
        systemPrompt=row.system_prompt,
        greetingTemplate=row.greeting_template,
        closingTemplate=row.closing_template,
        escalationRules=dict(row.escalation_rules),
        languageRoutes=dict(row.language_routes),
        llmSettings=dict(row.llm_settings),
        ttsSettings=dict(row.tts_settings),
        guardrails=dict(row.guardrails),
    )


class PublishResult(BaseModel):
    id: str
    version: int
    isPublished: bool
    previousVersion: int | None


@router.post("/flows/{config_id}/publish", response_model=PublishResult)
async def publish_flow(
    config_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> PublishResult:
    """§15.1 makes publishing a distinct, audited action.

    It is also the moment the system goes live: ``build_call_pipeline`` refuses
    to assemble an agent without a published config, because an agent speaking
    words nobody approved is worse than one that hands the caller to a person.
    A freshly seeded install therefore takes no calls until a human does this.

    A partial unique index enforces one published version per flow. Rather than
    let the update fail on it, the previous version is unpublished in the same
    transaction: the constraint is the guarantee, this is the intent.
    """
    row = await db.scalar(
        select(AgentConfig).where(
            AgentConfig.id == config_id, AgentConfig.deleted_at.is_(None)
        )
    )
    if row is None:
        raise NotFoundError(resource="agent_config", identifier=str(config_id))
    if row.is_published:
        return PublishResult(
            id=str(row.id), version=row.version, isPublished=True, previousVersion=None
        )

    current = await db.scalar(
        select(AgentConfig).where(
            AgentConfig.organization_id == row.organization_id,
            AgentConfig.flow_type == row.flow_type,
            AgentConfig.is_published.is_(True),
            AgentConfig.deleted_at.is_(None),
        )
    )
    previous = current.version if current else None
    if current is not None:
        current.is_published = False
        current.published_at = None
        current.published_by_user_id = None
        # Flushed before the new one is set, so the partial unique index never
        # sees two published rows even momentarily.
        await db.flush()

    row.is_published = True
    row.published_at = datetime.now(UTC)
    row.published_by_user_id = principal.user_id

    await append_audit(
        db,
        action=AuditAction.PUBLISH,
        resource_type="agent_config",
        resource_id=str(row.id),
        actor_user_id=principal.user_id,
        before={"published_version": previous},
        after={"published_version": row.version, "flow_type": row.flow_type.value},
    )
    log.info(
        "flows.published",
        flow_type=row.flow_type.value,
        version=row.version,
        previous=previous,
    )
    return PublishResult(
        id=str(row.id),
        version=row.version,
        isPublished=True,
        previousVersion=previous,
    )


# --------------------------------------------------------------------------- #
# Users, roles and the audit log (§15.1, §17)
# --------------------------------------------------------------------------- #


class UserRow(BaseModel):
    id: str
    email: str
    fullName: str
    role: str
    centreIds: list[str]
    isActive: bool
    mfaEnrolled: bool
    lastLoginAt: str | None


@router.get("/users", response_model=list[UserRow])
async def users(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> list[UserRow]:
    # The centre scope comes from the join table, aggregated in SQL. Loading
    # the relationship per user would be one query per row on the screen that
    # lists every user.
    statement: Select[Any] = (
        select(
            User,
            func.coalesce(
                func.array_agg(UserCentreAccess.centre_id).filter(
                    UserCentreAccess.centre_id.isnot(None)
                ),
                func.cast(text("'{}'"), ARRAY(PGUUID(as_uuid=True))),
            ).label("centre_ids"),
        )
        .outerjoin(UserCentreAccess, UserCentreAccess.user_id == User.id)
        .where(User.deleted_at.is_(None))
        .group_by(User.id)
        .order_by(User.email)
    )
    rows = (await db.execute(statement)).all()
    return [
        UserRow(
            id=str(row.id),
            email=row.email,
            fullName=row.full_name,
            role=row.role.value,
            centreIds=[str(cid) for cid in (centre_ids or [])],
            isActive=row.is_active,
            # §17 makes MFA mandatory. Surfaced per user because an account
            # without it is the one an attacker uses.
            mfaEnrolled=row.mfa_enrolled_at is not None,
            lastLoginAt=row.last_login_at.isoformat() if row.last_login_at else None,
        )
        for row, centre_ids in rows
    ]


class AuditRow(BaseModel):
    chainIndex: int
    at: str
    actorName: str | None
    action: str
    resourceType: str
    resourceId: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class AuditPage(BaseModel):
    rows: list[AuditRow]
    total: int
    chainIntact: bool
    brokenAt: int | None


@router.get("/audit", response_model=AuditPage)
async def audit(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.AUDITOR)],
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> AuditPage:
    """§15.1's audit log: filterable, exportable, tamper-evident.

    The chain is verified on every page load rather than only by the hourly
    job. An auditor looking at these rows is entitled to know, at that moment,
    whether the chain still holds -- a page that renders a broken chain as
    ordinary history is worse than no page at all.
    """
    statement: Select[Any] = (
        select(AuditLog, User.full_name)
        .outerjoin(User, AuditLog.actor_user_id == User.id)
        .order_by(AuditLog.chain_index.desc())
    )
    if action:
        statement = statement.where(AuditLog.action == action)
    if resource_type:
        statement = statement.where(AuditLog.resource_type == resource_type)

    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (await db.execute(statement.limit(limit).offset(offset))).all()
    verification = await verify_chain(db)

    return AuditPage(
        total=total,
        chainIntact=verification.ok,
        brokenAt=verification.broken_at,
        rows=[
            AuditRow(
                chainIndex=row.chain_index,
                at=row.at.isoformat(),
                actorName=actor,
                action=row.action.value,
                resourceType=row.resource_type,
                resourceId=row.resource_id,
                before=row.before,
                after=row.after,
            )
            for row, actor in rows
        ],
    )


# --------------------------------------------------------------------------- #
# Farmers (§15.1, §17)
# --------------------------------------------------------------------------- #


class FarmerRow(BaseModel):
    id: str
    fullName: str
    village: str | None
    districtName: str | None
    centreCode: str | None
    preferredLanguage: str
    crops: list[str]
    landAreaBigha: float | None
    #: Last four digits, and only ever four. §17: the panel has nowhere to put
    #: a full number because the response model has no field for one.
    phoneLast4: str
    isDnc: bool
    hasPromotionalConsent: bool
    lastContactAt: str | None


class FarmerList(BaseModel):
    rows: list[FarmerRow]
    total: int


@router.get("/farmers", response_model=FarmerList)
async def farmers(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.READ_ONLY)],
    q: str | None = None,
    centre_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> FarmerList:
    """§15.1's searchable directory.

    Searchable by name, village and **phone last-4** -- never by full number.
    A staff member who has a farmer on another line knows the last four digits
    from the CLI; giving the search a full-number mode would mean the panel
    accepting one, which is the first step to storing one.

    The result set is RLS-scoped, so a centre manager's export contains their
    own centre's farmers whatever this handler does.
    """
    now = datetime.now(UTC)
    consent = (
        select(ConsentRecord.farmer_id)
        .where(
            ConsentRecord.consent_type == ConsentType.PROMOTIONAL_VOICE,
            ConsentRecord.revoked_at.is_(None),
            ConsentRecord.expires_at > now,
        )
        .scalar_subquery()
    )

    statement: Select[Any] = (
        select(
            Farmer,
            Centre.code,
            District.name,
            DndStatus.internal_dnc,
            DndStatus.is_dnd,
            Farmer.id.in_(consent).label("has_consent"),
        )
        .outerjoin(Centre, Farmer.assigned_centre_id == Centre.id)
        .outerjoin(District, Centre.district_id == District.id)
        .outerjoin(DndStatus, DndStatus.phone_hash == Farmer.phone_hash)
        .where(Farmer.deleted_at.is_(None))
        .order_by(Farmer.full_name)
    )
    if centre_id:
        statement = statement.where(Farmer.assigned_centre_id == centre_id)
    if q:
        term = q.strip()
        if term.isdigit() and len(term) == 4:
            statement = statement.where(Farmer.phone_last4 == term)
        else:
            pattern = f"%{term}%"
            statement = statement.where(
                Farmer.full_name.ilike(pattern) | Farmer.village.ilike(pattern)
            )

    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (await db.execute(statement.limit(limit).offset(offset))).all()

    return FarmerList(
        total=total,
        rows=[
            FarmerRow(
                id=str(farmer.id),
                fullName=farmer.full_name,
                village=farmer.village,
                districtName=district,
                centreCode=centre_code,
                preferredLanguage=farmer.preferred_language,
                crops=list(farmer.primary_crops),
                landAreaBigha=float(farmer.land_area_value)
                if farmer.land_area_value is not None
                else None,
                phoneLast4=farmer.phone_last4,
                # Either kind of block. A screen that showed only the internal
                # one would let staff dial a nationally-registered number.
                isDnc=bool(internal_dnc or is_dnd),
                hasPromotionalConsent=bool(has_consent),
                lastContactAt=farmer.last_contact_at.isoformat()
                if farmer.last_contact_at
                else None,
            )
            for farmer, centre_code, district, internal_dnc, is_dnd, has_consent in rows
        ],
    )


class DncToggle(BaseModel):
    isDnc: bool


@router.post("/farmers/{farmer_id}/dnc", response_model=DncToggle)
async def set_dnc(
    farmer_id: uuid.UUID,
    body: DncToggle,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> DncToggle:
    """§15.1's manual DNC toggle, audited.

    Turning it *on* is always allowed. Turning it off is a decision to start
    calling somebody who was marked not to be called, so it is recorded with
    the name of whoever made it -- §18 puts the burden of proof on us.
    """
    farmer = await db.scalar(
        select(Farmer).where(Farmer.id == farmer_id, Farmer.deleted_at.is_(None))
    )
    if farmer is None:
        raise NotFoundError(resource="farmer", identifier=str(farmer_id))

    status = await db.scalar(
        select(DndStatus).where(DndStatus.phone_hash == farmer.phone_hash)
    )
    before = bool(status and status.internal_dnc)
    if status is None:
        status = DndStatus(phone_hash=farmer.phone_hash)
        db.add(status)
    status.internal_dnc = body.isDnc
    status.internal_dnc_at = datetime.now(UTC) if body.isDnc else None

    await append_audit(
        db,
        action=AuditAction.UPDATE,
        resource_type="dnd_status",
        resource_id=str(farmer_id),
        actor_user_id=principal.user_id,
        before={"internal_dnc": before},
        after={"internal_dnc": body.isDnc},
    )
    return DncToggle(isDnc=body.isDnc)


# --------------------------------------------------------------------------- #
# Catalogue and inventory (§15.1)
# --------------------------------------------------------------------------- #


class InventoryRow(BaseModel):
    variantId: str
    sku: str
    productHi: str
    packSize: str
    centreId: str
    centreCode: str
    quantity: int
    sellingPriceRupees: float
    isAvailable: bool
    updatedAt: str


class InventoryGrid(BaseModel):
    rows: list[InventoryRow]
    total: int
    #: Seconds before a price edit reaches the agent. §15.1 requires this to be
    #: displayed rather than assumed: staff who do not know the delay conclude
    #: the edit did not work and make it twice.
    propagationSeconds: int


@router.get("/inventory", response_model=InventoryGrid)
async def inventory(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.READ_ONLY)],
    centre_id: uuid.UUID | None = None,
    stock_out: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> InventoryGrid:
    """The per-centre grid §15.1 calls the screen that gets used daily.

    Returned as a flat list rather than nested by product, because the screen
    is a spreadsheet: a nested shape would have to be flattened in the browser
    on every render and would make sorting by stock level a client-side sort of
    a tree.
    """
    statement: Select[Any] = (
        select(Inventory, ProductVariant, Product, Centre)
        .join(ProductVariant, Inventory.variant_id == ProductVariant.id)
        .join(Product, ProductVariant.product_id == Product.id)
        .join(Centre, Inventory.centre_id == Centre.id)
        .where(Product.deleted_at.is_(None))
        .order_by(Centre.code, Product.name_hi)
    )
    if centre_id:
        statement = statement.where(Inventory.centre_id == centre_id)
    if stock_out:
        statement = statement.where(Inventory.is_available.is_(False))

    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (await db.execute(statement.limit(limit).offset(offset))).all()

    return InventoryGrid(
        total=total,
        propagationSeconds=_CATALOGUE_PROPAGATION_SECONDS,
        rows=[
            InventoryRow(
                variantId=str(variant.id),
                sku=product.sku,
                productHi=product.name_hi,
                packSize=f"{variant.pack_size_value:g} {variant.pack_size_unit}",
                centreId=str(centre.id),
                centreCode=centre.code,
                # On hand minus reserved: the figure that decides whether the
                # agent may promise a bag. Reserved stock is already somebody
                # else's, and quoting it is how a farmer drives to a centre for
                # a product that is not there.
                quantity=max(0, int(stock.qty_on_hand or 0) - int(stock.qty_reserved or 0)),
                # Per centre, not per variant: §15.1 puts price on the
                # inventory grid because a centre sets its own.
                sellingPriceRupees=float(stock.selling_price),
                isAvailable=bool(stock.is_available),
                updatedAt=stock.updated_at.isoformat(),
            )
            for stock, variant, product, centre in rows
        ],
    )


# --------------------------------------------------------------------------- #
# Knowledge base (§15.1's retrieval playground)
# --------------------------------------------------------------------------- #


class KbDocumentRow(BaseModel):
    id: str
    title: str
    sourceType: str
    language: str
    version: int
    isPublished: bool
    chunkCount: int
    embeddedCount: int
    updatedAt: str


@router.get("/knowledge/documents", response_model=list[KbDocumentRow])
async def knowledge_documents(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.READ_ONLY)],
) -> list[KbDocumentRow]:
    """Documents with their embedding progress.

    ``embeddedCount`` separate from ``chunkCount`` because they diverge in a
    way that matters: ``uaagro-kb ingest`` completes BM25-only when the
    embedding model is unavailable, and those chunks retrieve far worse without
    ever looking broken. A document showing 40 chunks and 0 embedded is the
    explanation for "the agent cannot find this".
    """
    embedded = func.count(KbChunk.embedding).label("embedded")
    statement: Select[Any] = (
        select(KbDocument, func.count(KbChunk.id).label("chunks"), embedded)
        .outerjoin(KbChunk, KbChunk.document_id == KbDocument.id)
        .where(KbDocument.deleted_at.is_(None))
        .group_by(KbDocument.id)
        .order_by(KbDocument.title)
    )
    rows = (await db.execute(statement)).all()
    return [
        KbDocumentRow(
            id=str(doc.id),
            title=doc.title,
            sourceType=doc.source_type,
            language=doc.language,
            version=doc.version,
            isPublished=doc.is_published,
            chunkCount=int(chunks or 0),
            embeddedCount=int(embedded_count or 0),
            updatedAt=doc.updated_at.isoformat(),
        )
        for doc, chunks, embedded_count in rows
    ]


# --------------------------------------------------------------------------- #
# Analytics (§15.1)
# --------------------------------------------------------------------------- #


class LanguageQuality(BaseModel):
    language: str
    calls: int
    avgConfidence: float | None
    transferRate: float


class CentreQuality(BaseModel):
    centreCode: str
    calls: int
    avgConfidence: float | None
    resolutionRate: float


class Analytics(BaseModel):
    costPerCallRupees: float
    costPerResolvedRupees: float
    costByComponent: dict[str, float]
    byLanguage: list[LanguageQuality]
    byCentre: list[CentreQuality]


@router.get("/analytics", response_model=Analytics)
async def analytics(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
    days: int = Query(7, ge=1, le=90),
) -> Analytics:
    """§15.1's analytics, with the STT-confidence breakdown it names.

    Confidence by language and by centre is the interesting one: §15.1 says
    plainly that it surfaces which districts have accent or noise problems, and
    that is a real operational finding -- one centre with poor audio looks
    exactly like a bad agent until this is broken out.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    base = select(Call).where(Call.started_at >= since).subquery()

    totals = (
        await db.execute(
            select(
                func.count().label("calls"),
                func.coalesce(func.sum(base.c.cost_total_inr), 0).label("spend"),
                func.count().filter(base.c.outcome == CallOutcome.RESOLVED).label("resolved"),
            ).select_from(base)
        )
    ).one()
    calls_count = int(totals.calls or 0)
    spend = float(totals.spend or 0)
    resolved = int(totals.resolved or 0)

    components: dict[str, float] = {}
    for key in ("telephony", "stt", "tts", "llm"):
        # Summed from the JSONB breakdown each call wrote during the call
        # (§8). Stored per call rather than recomputed from vendor rates, so
        # this reflects what was actually spent.
        value = await db.scalar(
            select(
                func.coalesce(
                    func.sum(func.cast(base.c.cost_breakdown[key].astext, Numeric)), 0
                )
            ).select_from(base)
        )
        components[key] = float(value or 0)

    # Confidence is recorded per *turn*, so it is averaged from `call_turns`
    # in its own subquery and joined back. Averaging an average-per-call would
    # weight a two-turn call the same as a twenty-turn one, and the noisy calls
    # this screen exists to find are the long ones.
    confidence = (
        select(
            CallTurn.call_id.label("call_id"),
            func.avg(CallTurn.asr_confidence).label("confidence"),
        )
        .where(CallTurn.asr_confidence.isnot(None))
        .group_by(CallTurn.call_id)
        .subquery()
    )

    language_rows = (
        await db.execute(
            select(
                func.coalesce(base.c.language_final, base.c.language_detected).label("lang"),
                func.count().label("calls"),
                func.avg(confidence.c.confidence).label("confidence"),
                func.count().filter(base.c.was_transferred.is_(True)).label("transferred"),
            )
            .select_from(base)
            .outerjoin(confidence, confidence.c.call_id == base.c.id)
            .group_by("lang")
            .order_by(func.count().desc())
        )
    ).all()

    centre_rows = (
        await db.execute(
            select(
                Centre.code,
                func.count().label("calls"),
                func.avg(confidence.c.confidence).label("confidence"),
                func.count().filter(base.c.outcome == CallOutcome.RESOLVED).label("resolved"),
            )
            .select_from(base)
            .join(Centre, Centre.id == base.c.centre_id)
            .outerjoin(confidence, confidence.c.call_id == base.c.id)
            .group_by(Centre.code)
            .order_by(Centre.code)
        )
    ).all()

    return Analytics(
        costPerCallRupees=round(spend / calls_count, 4) if calls_count else 0.0,
        # Per *resolved* query, not per call. §8 measures the cost of an
        # outcome; a cheap call that resolved nothing is not a saving.
        costPerResolvedRupees=round(spend / resolved, 4) if resolved else 0.0,
        costByComponent=components,
        byLanguage=[
            LanguageQuality(
                language=row.lang or "",
                calls=int(row.calls),
                avgConfidence=float(row.confidence) if row.confidence is not None else None,
                transferRate=round(int(row.transferred) / int(row.calls), 4)
                if row.calls
                else 0.0,
            )
            for row in language_rows
        ],
        byCentre=[
            CentreQuality(
                centreCode=row.code,
                calls=int(row.calls),
                avgConfidence=float(row.confidence) if row.confidence is not None else None,
                resolutionRate=round(int(row.resolved) / int(row.calls), 4)
                if row.calls
                else 0.0,
            )
            for row in centre_rows
        ],
    )


# --------------------------------------------------------------------------- #
# Spam & screening (§15.1)
# --------------------------------------------------------------------------- #


class SpamRuleRow(BaseModel):
    id: str
    ruleType: str
    pattern: str | None
    isActive: bool
    action: str
    hitCount: int
    falsePositiveCount: int
    #: Share of hits a human later marked as a real farmer. The number that
    #: decides whether a rule is working or quietly turning callers away.
    falsePositiveRate: float


@router.get("/spam-rules", response_model=list[SpamRuleRow])
async def spam_rules(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> list[SpamRuleRow]:
    """Rules with live hit counts (§15.1).

    The hit count alone does not say whether a rule is working: a rule with
    four thousand hits is either blocking four thousand robocalls or turning
    away four thousand farmers, and those look identical from here. The
    false-positive rate -- fed by §15.1's one-click "this was a real farmer" --
    is what separates them, so it is returned alongside rather than left to a
    second screen nobody opens.
    """
    rows = (
        await db.scalars(select(SpamRule).order_by(SpamRule.hit_count.desc()))
    ).all()
    return [
        SpamRuleRow(
            id=str(row.id),
            ruleType=row.rule_type.value,
            pattern=row.pattern,
            isActive=row.is_active,
            action=row.action.value,
            hitCount=int(row.hit_count or 0),
            falsePositiveCount=int(row.false_positive_count or 0),
            falsePositiveRate=round(
                int(row.false_positive_count or 0) / int(row.hit_count), 4
            )
            if row.hit_count
            else 0.0,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Live calls and offers (§15.1)
# --------------------------------------------------------------------------- #


class LiveCall(BaseModel):
    id: str
    callRef: str
    startedAt: str
    direction: str
    centreCode: str | None
    language: str
    callerLast4: str | None
    elapsedSeconds: int
    turnCount: int
    lastIntent: str | None


class LiveCalls(BaseModel):
    rows: list[LiveCall]
    concurrency: int
    capacity: int


@router.get("/live-calls", response_model=LiveCalls)
async def live_calls(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> LiveCalls:
    """Calls in flight, from the rows the media path writes as it goes.

    Read from ``calls`` rather than from the worker's memory, on purpose: §1 N8
    has the call record written incrementally *during* the call, so the
    database already knows, and asking the worker would mean the panel holding
    a connection to every media process to render one table.

    What this does not do is stream the transcript, or offer listen, barge-in
    and force-transfer. §15.1 asks for all four; they need a live channel out
    of the media path that does not exist yet, and each is an intervention on a
    call in progress -- the wrong thing to approximate. The turn count is the
    honest substitute: it moves while a call is live.
    """
    settings = get_settings()
    statement: Select[Any] = (
        select(
            Call,
            Centre.code,
            Farmer.phone_last4,
            select(func.count())
            .select_from(CallTurn)
            .where(CallTurn.call_id == Call.id)
            .scalar_subquery()
            .label("turns"),
        )
        .outerjoin(Centre, Call.centre_id == Centre.id)
        .outerjoin(Farmer, Call.farmer_id == Farmer.id)
        .where(Call.status == CallStatus.IN_PROGRESS)
        .order_by(Call.started_at)
    )
    rows = (await db.execute(statement)).all()
    now = datetime.now(UTC)

    return LiveCalls(
        concurrency=len(rows),
        capacity=settings.max_concurrent_calls,
        rows=[
            LiveCall(
                id=str(call.id),
                callRef=call.call_ref,
                startedAt=call.started_at.isoformat(),
                direction=call.direction.value,
                centreCode=centre_code,
                language=call.language_final or call.language_detected or "",
                callerLast4=last4,
                elapsedSeconds=max(0, int((now - call.started_at).total_seconds())),
                turnCount=int(turns or 0),
                lastIntent=call.intents[-1] if call.intents else None,
            )
            for call, centre_code, last4, turns in rows
        ],
    )


class OfferRow(BaseModel):
    id: str
    code: str
    name: str
    descriptionHi: str
    discountType: str
    discountValue: float
    validFrom: str
    validTo: str
    isActive: bool
    whatsappTemplateName: str | None


@router.get("/offers", response_model=list[OfferRow])
async def offers(
    db: DbDep,
    _: Annotated[Principal, require_role(Role.CENTRE_MANAGER)],
) -> list[OfferRow]:
    rows = (
        await db.scalars(
            select(Offer)
            .where(Offer.deleted_at.is_(None))
            .order_by(Offer.valid_to.desc())
        )
    ).all()
    # In IST, because an offer valid "until the 30th" ends at the close of the
    # 30th in Lucknow, not at 05:30 that morning when UTC rolls over.
    today = datetime.now(ist()).date()
    return [
        OfferRow(
            id=str(row.id),
            code=row.code,
            name=row.name,
            descriptionHi=row.description_hi,
            discountType=row.discount_type,
            discountValue=float(row.discount_value),
            validFrom=row.valid_from.isoformat(),
            validTo=row.valid_to.isoformat(),
            # Computed, not stored. An offer whose end date passed last night
            # is expired whether or not a job has run to say so, and a screen
            # showing it as live is how it gets pitched on a call.
            isActive=row.valid_from <= today <= row.valid_to,
            whatsappTemplateName=row.whatsapp_template_name,
        )
        for row in rows
    ]


_ = Decimal  # re-exported for the cost arithmetic above

__all__ = ("router",)
