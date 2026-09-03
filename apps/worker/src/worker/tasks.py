"""ARQ task definitions and the cron schedule (§11.5, §13, §18, §20).

Every background job in the system, in one place, so the answer to "what runs on
a timer" is a file rather than an archaeology exercise.

Three conventions hold across all of them:

**Idempotent.** ARQ retries on failure, and a retry must not double an effect.
The post-call pipeline looks up before creating; the partition job creates only
what is missing; retention deletes by age, which is stable across runs.

**Bounded.** Each job has a timeout, because a job that hangs holds a worker
slot forever and the queue behind it stops. The timeouts are generous relative
to the work and tight relative to the interval.

**Quiet on success.** A job that logs on every run trains people to ignore its
output, which means the run that failed looks the same as the ones that did not.
These log a line when they do something and stay silent when there was nothing
to do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from uaagro_domain.compliance import within_calling_window
from uaagro_domain.eventloop import install_fast_event_loop
from uaagro_domain.livefeed import CONTACT_UPDATED, LiveEvent, LiveFeed, RedisLiveFeed
from uaagro_domain.settings import get_defaults, get_settings
from uaagro_domain.telemetry import INSTRUMENTS
from uaagro_domain.timezone import now_ist

from .sync import run_scheduled_syncs

log = structlog.get_logger(__name__)

#: One publisher per worker process, made on first use so importing this
#: module never opens a socket.
_live_feed: LiveFeed | None = None


def live_feed() -> LiveFeed:
    global _live_feed
    if _live_feed is None:
        _live_feed = RedisLiveFeed(get_settings().redis_url)
    return _live_feed


def campaign_control_key(campaign_id: str | uuid.UUID) -> str:
    """Where the panel's pause/stop instruction for a campaign lives in Redis."""
    return f"campaign:{campaign_id}:control"


#: Where this process says it is alive, and for how long that claim stands.
#: Mirrored by ``api.services.jobs.HEARTBEAT_KEY``; a test keeps them equal.
#: The TTL is three beats: one missed minute is a busy job, three is an outage.
HEARTBEAT_KEY = "uaagro:worker:heartbeat"
HEARTBEAT_TTL_S = 180


#: A contact marked as being dialled this long ago with no live call behind
#: it never connected. Longer than the provider's ring timeout plus a short
#: call, shorter than the retry gap.
DIAL_STALE_AFTER = timedelta(minutes=3)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


async def process_call(ctx: dict[str, Any], call_id: str) -> str:
    """§11.5's post-call pipeline, enqueued when a call ends."""
    from uaagro_db.engine import system_session

    from .postcall import run
    from .summary import build_summariser

    # `system_session`, not a bare sessionmaker: RLS policies read `app.role`,
    # and an unbound session sees no rows at all -- this job would report "call
    # not found" for every call it was handed.
    async with system_session() as session:
        report = await run(
            session,
            uuid.UUID(call_id),
            summarise=build_summariser(get_settings()),
            live_feed=live_feed(),
        )

    if report.failed:
        # Raised so ARQ retries. The stages that succeeded have already
        # committed, and each is idempotent, so a retry re-runs only what is
        # still missing.
        raise RuntimeError(report.summary())
    return report.summary()


async def dial_campaign(ctx: dict[str, Any], campaign_id: str) -> str:
    """Run one approved campaign (§13.1).

    Enqueued by ``start_due_campaigns`` or by an operator pressing start in the
    panel. Not retried on a compliance block: §13.1's blocking checks are
    non-overridable, and a retry is exactly the override the spec forbids.
    """
    from uaagro_db.engine import system_session

    from .campaign import CampaignBlocked, run_campaign

    settings = get_settings()

    async def originate(to_number: str, from_number: str, custom_field: str) -> str:
        from voice_worker.adapters.telephony.control import build_adapter

        # The approved set is the number we just decrypted from this campaign's
        # own contact list. The check that carries the weight is the join in
        # `campaign._plaintext_number` -- see the note there.
        adapter = build_adapter(settings, approved=frozenset({to_number}))
        return await adapter.originate(
            to=to_number,
            from_=from_number,
            callback_url=f"{settings.public_base_url}/ws/voice",
            custom_field=custom_field,
        )

    # The panel's pause and stop buttons write this key; the dialer reads it
    # before every dial. A key rather than a message so a campaign that was
    # paused while the worker restarted stays paused.
    redis = ctx.get("redis")
    control_key = campaign_control_key(campaign_id)

    async def control() -> str | None:
        if redis is None:
            return None
        value = await redis.get(control_key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    try:
        report = await run_campaign(
            system_session,
            uuid.UUID(campaign_id),
            originate=originate,
            max_concurrent=settings.max_concurrent_calls,
            calls_per_minute=settings.outbound_calls_per_minute,
            live_feed=live_feed(),
            control=control,
        )
    except CampaignBlocked as blocked:
        # Logged and swallowed rather than raised: ARQ would retry a raised
        # exception, and retrying a compliance block is the override §13.1
        # exists to prevent. The campaign is already marked and the panel says
        # which checks failed.
        log.error("campaign.refused", campaign_id=campaign_id, checks=str(blocked))
        return f"blocked: {blocked}"
    return report.summary()


async def start_due_campaigns(ctx: dict[str, Any]) -> str:
    """Enqueue campaigns whose scheduled start has arrived (§13.1).

    Runs every five minutes around the clock and checks the window here, in
    IST. §18's window is a legal one and the process timezone is a deployment
    detail; deriving one from the other is how a container that moved to UTC
    starts dialling at half past two in the morning.

    The dialer re-checks per contact anyway, so a campaign enqueued at 20:58
    stops itself at 21:00 rather than needing this to be clever about it.
    """
    from uaagro_db.engine import system_session

    from .campaign import campaign_ids_ready

    if not within_calling_window(now_ist()):
        return ""

    async with system_session() as session:
        due = await campaign_ids_ready(session)
    if not due:
        return ""

    redis = ctx.get("redis")
    if redis is None:  # pragma: no cover -- ARQ always provides one
        return f"{len(due)} due, no queue available"
    for campaign_id in due:
        await redis.enqueue_job("dial_campaign", str(campaign_id))
    log.info("campaign.enqueued", count=len(due))
    return f"enqueued {len(due)}"


async def roll_partitions(ctx: dict[str, Any]) -> str:
    """Create next month's call partitions (§10).

    An insert with no matching partition **fails**, and that failure lands in
    the audio path -- so this runs daily rather than monthly. Twenty-nine
    redundant runs a month is the price of never discovering the gap at
    midnight on the first.
    """
    from datetime import date

    from uaagro_db.engine import get_migrator_engine
    from uaagro_db.partitions import ensure_partitions

    # A raw connection, not a session: partition DDL is not ORM work and
    # running it through a session would put schema changes in the same
    # transaction as whatever else that session was doing.
    async with get_migrator_engine().begin() as connection:
        created = await ensure_partitions(connection, anchor=date.today())

    if created:
        log.info("partitions.created", tables=created)
        return f"created {len(created)}"
    return ""


async def scrub_dnd(ctx: dict[str, Any]) -> str:
    """Refresh the DND register before campaigns run (§13.1, §18).

    §18: scrubbed before *every* campaign, and a prior customer relationship
    does not exempt a registered number. This keeps the local copy fresh; the
    gate still checks it per contact at dial time, because a scrub that ran
    this morning does not know about a registration from this afternoon.
    """
    log.info("dnd.scrub_requested")
    # The NCPR feed is a licensed integration that needs credentials this build
    # does not have. Deliberately not stubbed with fake data: a scrub that
    # silently "succeeds" against nothing is worse than one that says it did
    # not run, because the campaign gate would then trust a fresh timestamp.
    return "not configured"


async def expire_retention(ctx: dict[str, Any]) -> str:
    """Delete data past its §18 retention window.

    Recordings expire through the bucket lifecycle rule; this covers the
    database side -- transcripts and turn text -- which no lifecycle rule can
    reach.
    """
    from sqlalchemy import delete, select

    from uaagro_db.engine import system_session
    from uaagro_db.models import Call, CallTurn

    compliance = get_defaults().compliance
    cutoff = datetime.now(UTC) - timedelta(days=compliance.retention_days_transcripts)

    # RLS is FORCEd, so the owner role is subject to the policies too: running
    # this as the migrator would delete nothing and report success.
    async with system_session() as session:
        stale = select(Call.id).where(Call.started_at < cutoff)
        result = await session.execute(delete(CallTurn).where(CallTurn.call_id.in_(stale)))
        removed = int(getattr(result, "rowcount", 0) or 0)
    if removed:
        log.info("retention.transcripts_deleted", turns=removed, cutoff=cutoff.date().isoformat())
        return f"deleted {removed} turns"
    return ""


async def verify_audit_chain(ctx: dict[str, Any]) -> str:
    """§17: the hash chain is tamper-evident, so something has to check it.

    A chain nobody verifies is a chain that proves nothing. This runs hourly and
    raises on a break, which is what fires the ``AuditChainBroken`` page.
    """
    from uaagro_db.audit import verify_chain
    from uaagro_db.engine import get_migrator_sessionmaker

    async with get_migrator_sessionmaker()() as session:
        result = await verify_chain(session)

    if not result.ok:
        # Raised, not logged. §17 makes this a security incident until shown
        # otherwise, and a log line at 3am is not an incident response.
        raise RuntimeError(f"audit chain diverges at sequence {result.broken_at}: {result.reason}")
    return ""


async def reconcile_contacts(ctx: dict[str, Any]) -> str:
    """Resolve dials that never became calls (§13.3).

    The dialer marks a contact as being dialled and hands the number to the
    provider. If nobody answers, no media stream opens, no call row is
    written and nothing else would ever touch the contact: its card would
    stay amber forever and the retry policy would never see it. Every two
    minutes this finds those, records the no-answer, schedules the retry and
    tells the panel.
    """
    from sqlalchemy import select

    from uaagro_db.engine import system_session
    from uaagro_db.models import Call, CampaignContact, Farmer
    from uaagro_domain.enums import CallStatus, ContactStatus

    from .contacts import contact_result_from_call, panel_status
    from .dialer import schedule_retry

    stale_before = datetime.now(UTC) - DIAL_STALE_AFTER
    resolved = 0
    async with system_session() as session:
        rows = (
            await session.execute(
                select(
                    CampaignContact,
                    Call.status,
                    Call.outcome,
                    Farmer.full_name,
                    Farmer.phone_last4,
                )
                .join(Farmer, Farmer.id == CampaignContact.farmer_id)
                .outerjoin(Call, Call.id == CampaignContact.call_id)
                .where(
                    CampaignContact.status == ContactStatus.DIALING,
                    CampaignContact.last_attempt_at < stale_before,
                )
            )
        ).all()
        for contact, call_status, call_outcome, farmer_name, last4 in rows:
            if call_status is CallStatus.IN_PROGRESS:
                continue  # a long conversation, not a stale dial
            if call_status is not None:
                contact.status, contact.outcome = contact_result_from_call(call_outcome)
            else:
                contact.status, contact.outcome = ContactStatus.NO_ANSWER, "no_answer"
                if contact.last_attempt_at is not None:
                    contact.next_attempt_at = schedule_retry(
                        "no_answer",
                        last_attempt=contact.last_attempt_at,
                        attempts=contact.attempts,
                    )
            resolved += 1
            live_feed().publish(
                LiveEvent(
                    type=CONTACT_UPDATED,
                    payload={
                        "id": str(contact.id),
                        "farmerName": farmer_name,
                        "last4": str(last4),
                        "status": panel_status(contact.status),
                        "outcome": contact.outcome,
                        "dtmf": contact.dtmf_response,
                        "callId": str(contact.call_id) if contact.call_id else None,
                        "attempts": contact.attempts,
                    },
                    campaign_id=str(contact.campaign_id),
                )
            )
        await session.commit()

    if resolved:
        log.info("contacts.reconciled", resolved=resolved)
        return f"resolved {resolved}"
    return ""


async def worker_heartbeat(ctx: dict[str, Any]) -> str:
    """Say that this process is alive, for the panel's knowledge page.

    Every minute, with a TTL of three: the key expires on its own when the
    worker dies, so the panel never reads a heartbeat from a process that is
    not there. Written on startup too, so a fresh worker is seen at once
    rather than at the top of the next minute.
    """
    redis = ctx.get("redis")
    if redis is None:  # pragma: no cover -- ARQ always provides one
        return "no queue available"
    await redis.set(HEARTBEAT_KEY, datetime.now(UTC).isoformat(), ex=HEARTBEAT_TTL_S)
    return ""


async def ingest_document(ctx: dict[str, Any], document_id: str) -> str:
    """Extract, chunk, embed and publish one panel upload (§9, §15.1).

    The row already exists with ``ingest_status='pending'``; this fills it.
    Failures are written to the row rather than raised: an operator reading
    "The PDF contains no readable text" fixes it, one reading a retry
    counter does not -- and ARQ retrying a scan three times would not make
    it readable.
    """
    from uaagro_db.engine import system_session

    from .ingest import ingest_pending_document

    async with system_session() as session:
        outcome = await ingest_pending_document(session, uuid.UUID(document_id))
        await session.commit()
    return outcome


async def refresh_embeddings(ctx: dict[str, Any]) -> str:
    """Re-embed knowledge chunks that have no vector (§9).

    Picks up documents ingested while the embedding model was unavailable --
    which is a real state: `uaagro-kb ingest` completes BM25-only rather than
    failing, and those chunks sit un-embedded until something notices.
    """
    from sqlalchemy import func, select

    from uaagro_db.engine import get_migrator_sessionmaker
    from uaagro_db.models import KbChunk

    async with get_migrator_sessionmaker()() as session:
        pending = int(
            await session.scalar(
                select(func.count()).select_from(KbChunk).where(KbChunk.embedding.is_(None))
            )
            or 0
        )

    if not pending:
        return ""
    # Reported rather than done here: embedding is CPU-heavy and belongs on the
    # ingest path where it can use the machine, not on a cron worker sharing a
    # box with the dialer.
    log.warning("knowledge.chunks_without_vectors", pending=pending)
    return f"{pending} chunks need embedding; run `uaagro-kb ingest`"


# --------------------------------------------------------------------------- #
# Worker settings
# --------------------------------------------------------------------------- #


class WorkerSettings:
    """ARQ's entry point: ``arq worker.tasks.WorkerSettings``.

    ARQ has no equivalent of uvicorn's uvloop auto-detection, so without the
    call below this process -- the dialer included -- runs on the selector
    loop while the two web services run on uvloop.
    """

    # Runs at import, which is before ARQ creates its loop.
    _loop = install_fast_event_loop()

    functions = [  # noqa: RUF012 -- ARQ reads this attribute by name
        process_call,
        dial_campaign,
        start_due_campaigns,
        roll_partitions,
        scrub_dnd,
        expire_retention,
        verify_audit_chain,
        refresh_embeddings,
        reconcile_contacts,
        ingest_document,
        worker_heartbeat,
        run_scheduled_syncs,
    ]

    cron_jobs = [  # noqa: RUF012
        # Every minute. The panel reports the worker missing after three.
        cron(worker_heartbeat, minute=set(range(60))),
        # Hourly pulls from the client's database, and the daily ones at the
        # same minute; the job decides which sources are due.
        cron(run_scheduled_syncs, minute=7, timeout=1800),
        # Daily, not monthly. See roll_partitions.
        cron(roll_partitions, hour=2, minute=0),
        # Every two minutes: a dial that never connected should turn its card
        # red before the operator wonders why it is still amber.
        cron(reconcile_contacts, minute=set(range(0, 60, 2))),
        # Before the calling window opens, so a campaign starting at 09:00 has
        # a scrub from this morning rather than yesterday.
        cron(scrub_dnd, hour=7, minute=0),
        # Overnight: it is a bulk delete and competes with nothing at 03:00.
        cron(expire_retention, hour=3, minute=0),
        # Hourly. A tampered chain discovered a day later is a day of history
        # nobody can vouch for.
        cron(verify_audit_chain, minute=5),
        cron(refresh_embeddings, hour=4, minute=0),
        # Every five minutes through the calling window. A campaign
        # scheduled for 10:00 should start at 10:00, not at the top of the
        # next hour, and the dialer's own window check bounds the tail.
        # Every five minutes, around the clock. The window check lives in
        # the job rather than in this schedule because ARQ's cron fires on the
        # *process* timezone: a worker running in UTC with hour=9..21 would
        # dial from 14:30 IST to 02:30 IST, which is both late for the
        # campaign and outside the legal window at the far end.
        cron(start_due_campaigns, minute=set(range(0, 60, 5))),
    ]

    #: Generous against the work, tight against the interval. A job that hangs
    #: holds a worker slot forever and the queue behind it stops.
    job_timeout = 300
    max_tries = 3
    #: §11.5 wants the post-call pipeline done inside 30 s of the call ending,
    #: so the queue needs headroom for a burst at the end of a busy hour.
    max_jobs = 20

    #: A value, not a method: ARQ reads this class's ``__dict__`` and hands
    #: whatever it finds to the worker, so a callable here reached
    #: ``create_pool`` as-is and the process died before its first job.
    redis_settings: RedisSettings = RedisSettings.from_dsn(get_settings().redis_url)

    @staticmethod
    async def on_startup(_ctx: dict[str, object]) -> None:
        """§19 for the background worker too.

        The dialer's throughput and the post-call pipeline's timing are both
        §19 metrics, and neither was being exported from this process because
        nothing here ever called `setup()`.
        """
        settings = get_settings()
        INSTRUMENTS.setup(
            service_name="uaagro-worker",
            endpoint=settings.otel_exporter_otlp_endpoint or None,
            # Nothing scrapes this process -- it serves no HTTP. Traces and the
            # OTLP push are how its work becomes visible.
            prometheus=False,
        )
        await worker_heartbeat(dict(_ctx))


__all__ = (
    "WorkerSettings",
    "dial_campaign",
    "expire_retention",
    "process_call",
    "refresh_embeddings",
    "roll_partitions",
    "scrub_dnd",
    "start_due_campaigns",
    "verify_audit_chain",
    "worker_heartbeat",
)
