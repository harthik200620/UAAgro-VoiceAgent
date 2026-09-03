"""The background worker: post-call pipeline and dialer (§11.5, §13.1).

Two properties carry the weight here, and neither is visible from a passing
happy path:

**Idempotence.** ARQ retries, so every stage runs more than once in production.
A stage that doubles an effect on the second run raises a farmer's callback
ticket twice, and the farmer learns the system is not paying attention.

**Partial failure.** §11.5 inverts the usual error handling: the expensive thing
— the call — has already happened, so a stage that fails must not cost the
operator the stages that would have succeeded after it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_domain.compliance import Check, Contact
from uaagro_domain.enums import CallDirection, CallOutcome, CallStatus, TelephonyProvider
from worker import dialer as dialer_module
from worker import postcall
from worker.dialer import Dialer, schedule_retry

pytestmark = pytest.mark.integration


@pytest.fixture
async def call_id(app_engine) -> AsyncIterator[uuid.UUID]:  # type: ignore[no-untyped-def]
    """One finished call, written the way the voice worker writes it."""
    from uaagro_db.models import Call

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    started = datetime.now(UTC) - timedelta(minutes=5)

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        org = await session.scalar(text("SELECT id FROM organizations LIMIT 1"))
        centre = await session.scalar(text("SELECT id FROM centres ORDER BY code LIMIT 1"))
        farmer = await session.scalar(text("SELECT id FROM farmers LIMIT 1"))

        call = Call(
            organization_id=org,
            call_ref=f"TEST-{uuid.uuid4().hex[:8].upper()}",
            direction=CallDirection.INBOUND,
            provider=TelephonyProvider.SIMULATOR,
            centre_id=centre,
            farmer_id=farmer,
            started_at=started,
            answered_at=started,
            ended_at=started + timedelta(minutes=3),
            duration_seconds=180,
            status=CallStatus.COMPLETED,
            language_final="hi-IN",
            cost_breakdown={"telephony": 1.2, "stt": 2.1, "tts": 1.9, "llm": 0.9},
        )
        session.add(call)
        await session.commit()
        created = call.id

    yield created

    async with maker() as session:
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        await session.execute(text("DELETE FROM tickets WHERE call_id = :c"), {"c": created})
        await session.execute(text("DELETE FROM calls WHERE id = :c"), {"c": created})
        await session.commit()


@pytest.fixture
async def sessions(app_engine):  # type: ignore[no-untyped-def]
    """A factory for sessions that are all closed at teardown.

    The pipeline commits between stages, so a test needs a fresh session to see
    what the previous run wrote. Handing back an unmanaged session per call
    leaked connections into the garbage collector, which asyncpg then tried to
    terminate on a closed event loop -- noisy, and it would eventually exhaust
    the pool in a longer suite.
    """
    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    opened: list[object] = []

    async def make():  # type: ignore[no-untyped-def]
        session = maker()
        opened.append(session)
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        return session

    yield make

    for session in opened:
        await session.close()  # type: ignore[attr-defined]


async def _load_call(session, call_id):  # type: ignore[no-untyped-def]
    """Fetch a call by id alone.

    Not ``session.get``: ``calls`` is range-partitioned on ``started_at`` (§10),
    so its primary key is the composite ``(id, started_at)`` and a single value
    raises rather than returning None.
    """
    from sqlalchemy import select

    from uaagro_db.models import Call

    return await session.scalar(select(Call).where(Call.id == call_id))


# --------------------------------------------------------------------------- #
# §11.5 -- the post-call pipeline
# --------------------------------------------------------------------------- #


async def test_the_pipeline_runs_every_stage(sessions, call_id) -> None:  # type: ignore[no-untyped-def]
    session = await sessions()
    report = await postcall.run(session, call_id)
    await session.commit()

    names = [stage.name for stage in report.stages]
    # §11.5 lists these. A stage that silently stopped running would leave the
    # call record thinner and nothing would say so.
    assert names == [
        "transcript",
        "outcome",
        # Closes the campaign contact behind an outbound call (§13.1); a no-op
        # for an inbound one, but it still runs and still reports.
        "campaign",
        "tickets",
        "recording",
        "cost",
        "summary",
        "farmer",
        "notify",
    ]
    assert not report.failed, report.summary()


async def test_a_failing_stage_does_not_stop_the_others(sessions, call_id) -> None:  # type: ignore[no-untyped-def]
    """§11.5's inversion: the call already happened, so partial enrichment
    beats none. A summariser that raises must not also cost the operator the
    cost figures and the ticket."""

    async def exploding(_transcript: str, _language: str) -> tuple[str, str]:
        raise RuntimeError("the model is down")

    session = await sessions()
    report = await postcall.run(session, call_id, summarise=exploding)
    await session.commit()

    failed = {stage.name for stage in report.failed}
    assert failed <= {"summary"}, f"a summariser failure broke {failed}"
    # The stages after it still ran.
    assert {"farmer", "notify"} <= {s.name for s in report.stages if s.ok}


async def test_running_twice_does_not_double_the_ticket(sessions, call_id, app_engine) -> None:  # type: ignore[no-untyped-def]
    """ARQ retries, so every stage runs more than once in production. A second
    callback ticket means a farmer is called back twice about one thing."""

    # Force the branch that raises a ticket: a call that ended mid-resolution.
    session = await sessions()
    call = await _load_call(session, call_id)
    assert call is not None
    call.outcome = CallOutcome.ABANDONED_SILENCE
    await session.commit()

    for _ in range(3):
        session = await sessions()
        await postcall.run(session, call_id)
        await session.commit()

    async with app_engine.connect() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        count = await connection.scalar(
            text("SELECT count(*) FROM tickets WHERE call_id = :c"), {"c": call_id}
        )
    assert count == 1, f"{count} tickets after three runs"


async def test_a_resolved_call_gets_no_ticket(sessions, call_id, app_engine) -> None:  # type: ignore[no-untyped-def]
    """A ticket per call would bury the ones that need work."""

    session = await sessions()
    call = await _load_call(session, call_id)
    assert call is not None
    call.outcome = CallOutcome.RESOLVED
    await session.commit()

    session = await sessions()
    await postcall.run(session, call_id)
    await session.commit()

    async with app_engine.connect() as connection:
        await connection.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        count = await connection.scalar(
            text("SELECT count(*) FROM tickets WHERE call_id = :c"), {"c": call_id}
        )
    assert count == 0


async def test_the_cost_total_is_computed_from_its_components(sessions, call_id) -> None:  # type: ignore[no-untyped-def]
    """§8. A total stored without its breakdown cannot be checked, and a
    breakdown without a total has to be summed on every dashboard render."""
    session = await sessions()
    await postcall.run(session, call_id)
    await session.commit()

    session = await sessions()
    call = await _load_call(session, call_id)
    assert call is not None
    # Exactly 6.1, not approximately. Summed in float the same four components
    # give 6.100000000000001, and §8 measures the per-call ceiling and the
    # daily spend cap against this number.
    assert call.cost_total_inr == Decimal("6.1000")
    assert call.cost_breakdown["total"] == pytest.approx(6.1)


async def test_an_outcome_is_never_guessed_as_a_system_failure(sessions, call_id) -> None:  # type: ignore[no-untyped-def]
    """A call that simply stopped is the caller hanging up. Recording it as a
    system failure would inflate the failed-call tile §15 shows and send
    somebody chasing an incident that never happened."""
    session = await sessions()
    await postcall.run(session, call_id)
    await session.commit()

    session = await sessions()
    call = await _load_call(session, call_id)
    assert call is not None
    assert call.outcome is CallOutcome.CALLER_HUNG_UP


async def test_the_farmer_language_is_persisted(sessions, call_id) -> None:  # type: ignore[no-untyped-def]
    """§11.5. The next call starts in the right language instead of guessing
    again -- the difference between being recognised and being re-interrogated.
    """
    session = await sessions()
    report = await postcall.run(session, call_id)
    await session.commit()

    farmer_stage = next(s for s in report.stages if s.name == "farmer")
    assert farmer_stage.ok
    assert "last_contact" in farmer_stage.detail


async def test_a_missing_call_is_reported_not_raised(sessions) -> None:  # type: ignore[no-untyped-def]
    """A job for a call that was deleted must not crash-loop the queue."""
    session = await sessions()
    report = await postcall.run(session, uuid.uuid4())
    assert report.failed
    assert report.failed[0].detail == "call not found"


# --------------------------------------------------------------------------- #
# §13.1 -- the dialer
# --------------------------------------------------------------------------- #


@pytest.fixture
def open_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the §18 calling window open for tests about something else.

    Without this the dialer tests pass before 21:00 IST and fail after it: the
    window check is the first thing in the loop, so a run started at 21:05
    stops before its first recheck and every assertion below reads zero. That
    is the dialer behaving correctly, and a test that only runs in the
    afternoon is a test that is not running.

    Patched on ``worker.dialer`` rather than on the compliance module, because
    the dialer imports the symbol by name. The window itself is exercised by
    `test_the_dialer_stops_when_the_window_closes` and by the compliance
    suite; nothing here weakens it in production, where there is no seam.
    """
    monkeypatch.setattr(dialer_module, "within_calling_window", lambda _now: True)


def _contact() -> Contact:
    return Contact(farmer_id=uuid.uuid4(), phone_hash=b"\x00" * 32)


async def test_the_dialer_rechecks_every_contact_at_dial_time(open_window: None) -> None:
    """§13.1: a campaign approved at 09:00 is still running at 21:05, and by
    then some contacts have opted out on another call. A gate evaluated once is
    a gate that was true once."""
    checked: list[uuid.UUID] = []

    async def recheck(contact: Contact) -> Check | None:
        checked.append(contact.farmer_id)
        return None

    async def dial(_: Contact) -> str:
        return "sid"

    contacts = [_contact() for _ in range(3)]
    dialer = Dialer(dial=dial, recheck=recheck, calls_per_minute=6000)
    run = await dialer.run(uuid.uuid4(), contacts)

    assert len(checked) == 3
    assert run.dialled == 3


async def test_a_contact_that_fails_the_recheck_is_not_dialled(open_window: None) -> None:
    dialled: list[uuid.UUID] = []

    async def recheck(contact: Contact) -> Check | None:
        return Check.INTERNAL_DNC

    async def dial(contact: Contact) -> str:
        dialled.append(contact.farmer_id)
        return "sid"

    run = await Dialer(dial=dial, recheck=recheck, calls_per_minute=6000).run(
        uuid.uuid4(), [_contact()]
    )
    assert dialled == []
    assert run.skipped == 1


async def test_a_pause_stops_new_dials_and_lets_the_rest_finish(open_window: None) -> None:
    """§13.1: "It can be paused instantly." A pause that waited for the campaign
    to finish would not be a pause."""
    started: list[uuid.UUID] = []

    async def recheck(_: Contact) -> Check | None:
        return None

    dialer: Dialer

    async def dial(contact: Contact) -> str:
        started.append(contact.farmer_id)
        if len(started) == 2:
            dialer.pause("test")
        await asyncio.sleep(0.01)
        return "sid"

    dialer = Dialer(dial=dial, recheck=recheck, calls_per_minute=6000)
    run = await dialer.run(uuid.uuid4(), [_contact() for _ in range(10)])

    assert run.stopped_reason == "paused"
    assert run.dialled < 10
    # Everything that was in flight completed rather than being abandoned
    # mid-conversation.
    assert run.dialled == len(started)


async def test_a_failed_dial_does_not_stop_the_campaign(open_window: None) -> None:
    attempts = 0

    async def recheck(_: Contact) -> Check | None:
        return None

    async def flaky(_: Contact) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("carrier rejected")
        return "sid"

    run = await Dialer(dial=flaky, recheck=recheck, calls_per_minute=6000).run(
        uuid.uuid4(), [_contact() for _ in range(3)]
    )
    assert run.dialled == 2
    assert any(o.error for o in run.outcomes)


async def test_pacing_is_a_floor_not_a_bucket(open_window: None) -> None:
    """A token bucket lets a resumed campaign burst its accumulated allowance
    -- forty farmers phoned in one minute, which is the shape of a complaint."""
    import time

    async def recheck(_: Contact) -> Check | None:
        return None

    async def dial(_: Contact) -> str:
        return "sid"

    # 600/minute is a 100 ms floor; three contacts is at least 300 ms.
    started = time.perf_counter()
    await Dialer(dial=dial, recheck=recheck, calls_per_minute=600).run(
        uuid.uuid4(), [_contact() for _ in range(3)]
    )
    assert (time.perf_counter() - started) >= 0.25


async def test_the_dialer_stops_when_the_window_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§13.1 puts the calling window in the dialer, not in the schedule.

    A campaign approved at 09:00 with 400 contacts is still running at 21:05,
    and only a check inside the dial loop catches that. Every contact after the
    boundary is an offence, so the run stops rather than skipping ahead.
    """
    monkeypatch.setattr(dialer_module, "within_calling_window", lambda _now: False)

    async def recheck(_: Contact) -> Check | None:
        return None

    async def dial(_: Contact) -> str:  # pragma: no cover -- must never run
        raise AssertionError("dialled outside the calling window")

    run = await Dialer(dial=dial, recheck=recheck, calls_per_minute=6000).run(
        uuid.uuid4(), [_contact() for _ in range(3)]
    )
    assert run.dialled == 0
    assert run.stopped_reason == "outside the calling window"


def test_the_retry_policy_is_shared_with_the_gate() -> None:
    """One implementation, two callers. Two copies of a retry policy is how
    "opted out" eventually gets retried by whichever copy nobody updated."""
    now = datetime.now(UTC)
    assert schedule_retry("opted_out", last_attempt=now, attempts=0) is None
    assert schedule_retry("busy", last_attempt=now, attempts=1) is not None


# --------------------------------------------------------------------------- #
# §11.5 -- the summary and the recording
# --------------------------------------------------------------------------- #


class _ScriptedGateway:
    """Streams a fixed answer in pieces, the way the §6.1 gateway does."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[tuple[list[str], str, int | None]] = []

    async def stream(  # type: ignore[no-untyped-def]
        self,
        *,
        system_blocks,
        user_message,
        cacheable_prefix="",
        max_tokens=None,
        temperature=None,
    ):
        self.requests.append((list(system_blocks), user_message, max_tokens))
        for piece in self.answer.split("|"):
            yield piece, "primary"


async def test_the_summariser_writes_two_lines_and_never_a_phone_number() -> None:
    """§23-6: whatever the model was told, a number does not leave here."""
    from uaagro_domain.settings import Settings
    from worker import summary

    gateway = _ScriptedGateway(
        "Hindi: किसान ने 50 किलो डीएपी का रेट पूछा, 1250 रुपये बताया; "
        "9876543210 पर कॉलबैक।|\nEnglish: Farmer asked the DAP rate; told 1250; "
        "callback on +91 98765 43210."
    )
    summarise = summary.build_summariser(Settings(), gateway=gateway)
    hindi, english = await summarise("user: डीएपी का रेट\nassistant: 1250 रुपये", "hi-IN")

    assert hindi.startswith("किसान ने 50 किलो")
    assert "1250" in hindi and "9876543210" not in hindi
    assert english.startswith("Farmer asked")
    assert "43210" not in english
    system, user, max_tokens = gateway.requests[0]
    assert system == [summary.SYSTEM_PROMPT]
    assert "डीएपी का रेट" in user
    assert max_tokens == summary.MAX_SUMMARY_TOKENS


async def test_process_call_runs_the_summariser_on_the_transcript(
    embedded_pg, sessions, call_id, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The job passes a real summariser; the row carries what it wrote."""
    from uaagro_db.engine import dispose_engines
    from uaagro_db.models import CallTurn
    from uaagro_domain.enums import TurnRole
    from uaagro_domain.livefeed import NullLiveFeed
    from uaagro_domain.settings import get_settings
    from worker import summary, tasks

    monkeypatch.setenv("DATABASE_URL", embedded_pg.app_dsn)
    get_settings.cache_clear()
    await dispose_engines()

    async def fake(transcript: str, language: str) -> tuple[str, str]:
        assert "डीएपी" in transcript
        return "किसान ने डीएपी का रेट पूछा।", "Farmer asked the DAP rate."

    monkeypatch.setattr(summary, "build_summariser", lambda settings: fake)
    monkeypatch.setattr(tasks, "live_feed", NullLiveFeed)

    session = await sessions()
    call = await _load_call(session, call_id)
    assert call is not None
    session.add_all(
        [
            CallTurn(
                call_id=call.id,
                started_at=call.started_at,
                centre_id=call.centre_id,
                turn_index=index,
                role=role,
                text_original=said,
            )
            for index, role, said in (
                (0, TurnRole.USER, "डीएपी का रेट क्या है"),
                (1, TurnRole.ASSISTANT, "1250 रुपये बोरी।"),
            )
        ]
    )
    await session.commit()

    try:
        outcome = await tasks.process_call({}, str(call_id))
        assert "failed" not in outcome
        session = await sessions()
        call = await _load_call(session, call_id)
        assert call is not None
        assert call.summary_hi == "किसान ने डीएपी का रेट पूछा।"
        assert call.summary_en == "Farmer asked the DAP rate."
    finally:
        session = await sessions()
        await session.execute(text("DELETE FROM call_turns WHERE call_id = :c"), {"c": call_id})
        await session.commit()
        get_settings.cache_clear()
        await dispose_engines()


async def test_the_recording_stage_reports_what_the_worker_stored(sessions, call_id) -> None:  # type: ignore[no-untyped-def]
    """The media path stores the audio itself; the job only says whether it did."""
    session = await sessions()
    report = await postcall.run(session, call_id)
    stage = next(s for s in report.stages if s.name == "recording")
    assert stage.ok and stage.detail == "no recording"

    call = await _load_call(session, call_id)
    assert call is not None
    call.recording_object_key = "recordings/2026/09/03/TEST.wav"
    await session.commit()

    session = await sessions()
    report = await postcall.run(session, call_id)
    stage = next(s for s in report.stages if s.name == "recording")
    assert stage.ok and stage.detail == "stored by the worker"
    # Reported by presence, never by value (§23-6).
    assert "recordings/" not in stage.detail


# --------------------------------------------------------------------------- #
# The heartbeat the panel reads
# --------------------------------------------------------------------------- #


async def test_the_heartbeat_is_written_with_a_ttl_and_scheduled_every_minute() -> None:
    from api.services import jobs
    from worker import tasks

    written: list[tuple[str, str, int | None]] = []

    class _Redis:
        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            written.append((key, value, ex))

    assert await tasks.worker_heartbeat({"redis": _Redis()}) == ""
    key, value, ttl = written[0]
    assert key == tasks.HEARTBEAT_KEY == jobs.HEARTBEAT_KEY
    assert ttl == tasks.HEARTBEAT_TTL_S == jobs.HEARTBEAT_TTL_S
    assert datetime.fromisoformat(value).tzinfo is not None

    beat = next(
        job for job in tasks.WorkerSettings.cron_jobs if job.coroutine is tasks.worker_heartbeat
    )
    assert beat.minute == set(range(60))
    assert tasks.worker_heartbeat in tasks.WorkerSettings.functions
