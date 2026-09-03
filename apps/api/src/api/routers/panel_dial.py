"""Call these numbers now (§13.1, §15.1).

A quick dial is a campaign with the bookkeeping done in one request: the
pasted numbers become farmers, consent records and contacts through the same
path a campaign takes, the gate runs over them, and -- when the deployment
lets the person who placed it approve it -- the dialer is started before the
response is written. Nothing is skipped, only the waiting.

Two things it does not do. It does not get past the gate: a blocked quick
dial is a campaign waiting in the outbound list with its checks named,
exactly as a blocked campaign is. And it does not waive the four-eyes rule
on its own: ``OUTBOUND_QUICK_DIAL_SELF_APPROVE`` is a development setting,
production refuses to start with it on, and a quick dial there waits for a
second person like everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field

from uaagro_db.audit import append_audit
from uaagro_db.campaigns import evaluate_campaign
from uaagro_domain.enums import AuditAction, CampaignStatus, Role
from uaagro_domain.settings import get_settings
from uaagro_domain.timezone import now_ist

from ..security.deps import DbDep, Principal, require_role
from ..services import jobs
from .panel_campaigns import (
    CampaignCreated,
    ConsentRequiredError,
    RemovedBy,
    import_campaign,
    published_outbound_flow,
    summaries,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["panel"])

#: A quick dial is a handful of numbers; this rings them a few at a time
#: without asking the operator to size it.
QUICK_DIAL_CONCURRENCY = 5


class DialBody(BaseModel):
    numbers: str = Field(min_length=1)
    flowId: str | None = None
    name: str | None = Field(default=None, min_length=2, max_length=200)
    consentAttested: bool = False
    consentNote: str | None = Field(default=None, max_length=500)


class DialResult(CampaignCreated):
    started: bool
    blockedBy: list[str]


def _default_name() -> str:
    local = now_ist()
    return f"Quick dial {local.day} {local:%b %H:%M}"


@router.post("/dial", response_model=DialResult, status_code=201)
async def dial(
    body: DialBody,
    db: DbDep,
    principal: Annotated[Principal, require_role(Role.OPS_MANAGER)],
) -> DialResult:
    if not body.consentAttested:
        raise ConsentRequiredError(
            "The consent attestation is required.",
            remedy="Tick the box confirming these farmers agreed to promotional calls "
            "from UA Agro. Without recorded consent a promotional call is an offence.",
        )
    settings = get_settings()
    flow = await published_outbound_flow(db, body.flowId)
    imported = await import_campaign(
        db,
        principal,
        name=(body.name or _default_name()).strip(),
        flow=flow,
        numbers=body.numbers,
        max_concurrent=QUICK_DIAL_CONCURRENCY,
        consent_note=body.consentNote,
        source_type="quick_dial",
    )
    campaign = imported.campaign

    report, _ = await evaluate_campaign(db, campaign.id)
    blocked = [check.value for check in report.blocked_by]
    if not report.eligible:
        blocked.append("no_eligible_contacts")

    started = False
    if not blocked and settings.outbound_quick_dial_self_approve:
        campaign.status = CampaignStatus.APPROVED
        campaign.approved_by_user_id = principal.user_id
        campaign.approved_at = datetime.now(UTC)
        # The four-eyes waiver, on the row. The schema refuses an approver
        # equal to the creator without it (migration 0011).
        campaign.self_approved = True
        await db.flush()
        # Committed before the job is enqueued: the dialer runs in another
        # process and must find the campaign and its contacts.
        await db.commit()
        await jobs.set_control(campaign.id, None)
        await jobs.enqueue("dial_campaign", str(campaign.id))
        campaign.status = CampaignStatus.RUNNING
        started = True

    await append_audit(
        db,
        action=AuditAction.CREATE,
        resource_type="quick_dial",
        resource_id=str(campaign.id),
        actor_user_id=principal.user_id,
        after={
            "name": campaign.name,
            "flow": f"{flow.name} v{flow.version}",
            "contacts": imported.imported,
            "started": started,
            "self_approved": started,
            "blocked_by": blocked,
        },
    )
    log.info(
        "campaign.quick_dial",
        campaign_id=str(campaign.id),
        contacts=imported.imported,
        started=started,
        blocked=blocked,
    )
    summary = (await summaries(db, [campaign], principal))[0]
    return DialResult(
        campaign=summary,
        imported=imported.imported,
        invalid=imported.invalid,
        removedBy=[RemovedBy(check=check, count=count) for check, count in imported.removed_by],
        started=started,
        blockedBy=blocked,
    )


__all__ = ("QUICK_DIAL_CONCURRENCY", "DialResult", "router")
