"""Outbound: the compliance gate, the conversation, WhatsApp (§13, §14).

§21's Phase 6 gate is that *every compliance check demonstrably blocks a
violating campaign*. "Demonstrably" is the operative word -- a gate nobody has
watched refuse is a gate nobody knows works, and the failure mode is silent: the
campaign runs, the calls connect, and the violation is only visible to a
regulator.

So there is one test per check, each constructing a campaign that violates
exactly that rule and asserting it does not run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uaagro_domain.compliance import (
    MAX_ATTEMPTS_PER_CONTACT,
    MAX_CONTACTS_PER_FARMER_PER_WEEK,
    MIN_HOURS_BETWEEN_ATTEMPTS,
    CampaignSettings,
    Check,
    Contact,
    evaluate,
    is_valid_promotional_cli,
    next_retry,
    within_calling_window,
)
from uaagro_domain.enums import ConsentType
from uaagro_domain.timezone import ist
from voice_worker.outbound.conversation import (
    MAX_OBJECTION_LOOPS,
    Offer,
    OutboundAgent,
    OutboundFlow,
    OutboundState,
    Reply,
    classify_reply,
)
from voice_worker.outbound.whatsapp import (
    FREE_SERVICE_WINDOW_ENDS,
    RATES,
    REGISTRY,
    InboundMessage,
    TemplateCategory,
    estimate_cost,
    parse_webhook,
    validate_parameters,
    verify_webhook,
)

NOW = datetime(2026, 9, 15, 11, 0, tzinfo=ist())


def _contact(**overrides: object) -> Contact:
    """A contact that passes every check, so a test can break exactly one."""
    defaults: dict[str, object] = {
        "farmer_id": uuid.uuid4(),
        "phone_hash": b"\x00" * 32,
        "consent_type": ConsentType.PROMOTIONAL_VOICE,
        "consent_expires_at": NOW + timedelta(days=30),
    }
    return Contact(**{**defaults, **overrides})  # type: ignore[arg-type]


def _campaign(**overrides: object) -> CampaignSettings:
    defaults: dict[str, object] = {
        "is_promotional": True,
        "caller_id": "14012345678",
        "dlt_entity_id": "1234567890",
        "dlt_template_id": "9876543210",
        "dial_at": NOW,
    }
    return CampaignSettings(**{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# §13.1 -- one test per check
# --------------------------------------------------------------------------- #


def test_a_clean_campaign_passes() -> None:
    """The gate has to be quiet when nothing is wrong, or an operator learns to
    route around it."""
    report = evaluate([_contact(), _contact()], _campaign(), now=NOW)
    assert report.passed
    assert report.eligible_count == 2
    assert not report.blocked_by


def test_expired_consent_excludes_the_contact() -> None:
    """§18: consent is not permanent. An expired record is not consent."""
    report = evaluate(
        [_contact(consent_expires_at=NOW - timedelta(days=1))], _campaign(), now=NOW
    )
    assert report.eligible_count == 0
    assert report.removed[Check.CONSENT] == 1


def test_revoked_consent_excludes_the_contact() -> None:
    report = evaluate([_contact(consent_revoked=True)], _campaign(), now=NOW)
    assert report.removed[Check.CONSENT] == 1


def test_a_dnd_number_is_excluded_despite_a_prior_relationship() -> None:
    """§18 is explicit: a prior customer relationship does not exempt a
    DND-registered number. This is the check most likely to be argued with."""
    report = evaluate([_contact(is_dnd=True)], _campaign(), now=NOW)
    assert report.eligible_count == 0
    assert report.removed[Check.DND] == 1


def test_internal_dnc_excludes_from_transactional_calls_too() -> None:
    """Someone who said "don't call me" did not mean "except about orders"."""
    transactional = _campaign(is_promotional=False)
    report = evaluate([_contact(internal_dnc=True)], transactional, now=NOW)
    assert report.eligible_count == 0
    assert report.removed[Check.INTERNAL_DNC] == 1


def test_a_mobile_cli_blocks_the_whole_campaign() -> None:
    """§13.1: promotional traffic uses a 140-series CLI.

    This blocks the *campaign*, not individual contacts -- a campaign dialled
    from a plain mobile violates TCCCPR on every call it places, and the calls
    connect perfectly, so nothing else would notice.
    """
    report = evaluate([_contact()], _campaign(caller_id="9876543210"), now=NOW)
    assert not report.passed
    assert Check.CALLER_ID_SERIES in report.blocked_by


def test_a_transactional_campaign_may_use_a_non_140_cli() -> None:
    """The series rule is about promotional traffic. Applying it to service
    calls would block the order-update calls a farmer actually wants."""
    report = evaluate(
        [_contact()],
        _campaign(is_promotional=False, caller_id="8045678901"),
        now=NOW,
    )
    assert Check.CALLER_ID_SERIES not in report.blocked_by


@pytest.mark.parametrize(
    ("cli", "valid"),
    [
        ("14012345678", True),
        ("+9114012345678", True),
        ("9876543210", False),
        ("18001234567", False),   # toll-free is not the promotional series
        ("16001234567", False),   # 1600 is restricted to regulated entities
    ],
)
def test_the_promotional_series_check(cli: str, valid: bool) -> None:
    assert is_valid_promotional_cli(cli) is valid


def test_missing_dlt_registration_blocks_the_campaign() -> None:
    report = evaluate([_contact()], _campaign(dlt_entity_id=None), now=NOW)
    assert Check.DLT_REGISTRATION in report.blocked_by

    no_template = evaluate([_contact()], _campaign(dlt_template_id=None), now=NOW)
    assert Check.DLT_REGISTRATION in no_template.blocked_by


def test_dialling_outside_the_window_blocks_the_campaign() -> None:
    """§13.1: enforced by the dialer at dial time, not by the schedule alone.

    A campaign scheduled for 20:55 that is still dialling at 21:05 is in
    violation, and only a dial-time check catches that.
    """
    late = evaluate(
        [_contact()], _campaign(dial_at=NOW.replace(hour=21, minute=30)), now=NOW
    )
    assert Check.CALLING_WINDOW in late.blocked_by

    early = evaluate(
        [_contact()], _campaign(dial_at=NOW.replace(hour=7, minute=0)), now=NOW
    )
    assert Check.CALLING_WINDOW in early.blocked_by


@pytest.mark.parametrize(
    ("hour", "allowed"),
    [(8, False), (9, True), (14, True), (21, True), (22, False), (2, False)],
)
def test_the_calling_window_is_ist(hour: int, allowed: bool) -> None:
    """Evaluated in IST. A UTC comparison would put the boundary at 03:30 and
    15:30 local -- wrong twice a day, in the direction of calling at night."""
    assert within_calling_window(NOW.replace(hour=hour, minute=0)) is allowed


def test_the_attempt_cap_excludes_a_contact() -> None:
    report = evaluate(
        [_contact(attempts_this_campaign=MAX_ATTEMPTS_PER_CONTACT)],
        _campaign(),
        now=NOW,
    )
    assert report.removed[Check.FREQUENCY_CAP] == 1


def test_the_minimum_gap_between_attempts_is_enforced() -> None:
    recent = _contact(
        attempts_this_campaign=1,
        last_attempt_at=NOW - timedelta(hours=MIN_HOURS_BETWEEN_ATTEMPTS - 1),
    )
    assert evaluate([recent], _campaign(), now=NOW).removed[Check.FREQUENCY_CAP] == 1

    old = _contact(
        attempts_this_campaign=1,
        last_attempt_at=NOW - timedelta(hours=MIN_HOURS_BETWEEN_ATTEMPTS + 1),
    )
    assert evaluate([old], _campaign(), now=NOW).eligible_count == 1


def test_the_rolling_per_farmer_cap_is_enforced() -> None:
    """§13.1's cap is across *all* campaigns. A farmer contacted twice this
    week by two different campaigns has still been contacted twice."""
    report = evaluate(
        [_contact(contacts_this_week=MAX_CONTACTS_PER_FARMER_PER_WEEK)],
        _campaign(),
        now=NOW,
    )
    assert report.removed[Check.FREQUENCY_CAP] == 1


def test_a_farmer_in_another_live_campaign_is_held_back() -> None:
    report = evaluate([_contact(in_another_live_campaign=True)], _campaign(), now=NOW)
    assert report.removed[Check.DUPLICATE_SUPPRESSION] == 1


def test_every_check_reports_how_many_it_removed() -> None:
    """§13.1: the UI shows exactly how many contacts each check removed.

    Not cosmetic. An operator shown "1200 -> 340" with no breakdown assumes the
    gate is broken and looks for a way around it; one shown the breakdown fixes
    the consent problem instead.
    """
    report = evaluate(
        [
            _contact(is_dnd=True),
            _contact(is_dnd=True),
            _contact(consent_expires_at=NOW - timedelta(days=1)),
            _contact(internal_dnc=True),
            _contact(),
        ],
        _campaign(),
        now=NOW,
    )
    assert report.total == 5
    assert report.eligible_count == 1
    assert report.removed[Check.DND] == 2
    assert report.removed[Check.CONSENT] == 1
    assert report.removed[Check.INTERNAL_DNC] == 1
    # Every check present, including the zeroes: "DND removed 0" is different
    # from "DND was not checked".
    assert set(report.removed) == set(Check)
    assert "dnd removed 2" in report.summary()


def test_a_contact_failing_several_checks_is_counted_in_each() -> None:
    """First-match-wins would attribute every exclusion to whichever check ran
    first, and the operator would fix the wrong thing."""
    report = evaluate(
        [_contact(is_dnd=True, internal_dnc=True, in_another_live_campaign=True)],
        _campaign(),
        now=NOW,
    )
    assert report.removed[Check.DND] == 1
    assert report.removed[Check.INTERNAL_DNC] == 1
    assert report.removed[Check.DUPLICATE_SUPPRESSION] == 1


def test_a_campaign_with_no_eligible_contacts_does_not_pass() -> None:
    """Zero eligible is not "passed with nothing to do" -- it is a contact list
    that is wrong, and approving it wastes a reviewer's time."""
    report = evaluate([_contact(is_dnd=True)], _campaign(), now=NOW)
    assert not report.passed


def test_the_gate_has_no_override() -> None:
    """§13.1: non-overridable. Every override mechanism ever added to a system
    like this has eventually been used routinely."""
    import inspect

    signature = inspect.signature(evaluate)
    for forbidden in ("force", "skip", "override", "bypass", "ignore"):
        assert not any(forbidden in name for name in signature.parameters), (
            f"the gate accepts a {forbidden!r} parameter"
        )


# --------------------------------------------------------------------------- #
# §13.3 -- the retry table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("outcome", "attempts", "retries"),
    [
        ("no_answer", 1, True),
        ("no_answer", MAX_ATTEMPTS_PER_CONTACT, False),
        ("busy", 1, True),
        ("answering_machine", 0, True),
        ("answering_machine", 1, False),
        ("invalid_number", 0, False),
        ("declined", 0, False),
        ("opted_out", 0, False),
        ("transferred", 0, False),
    ],
)
def test_the_retry_policy(outcome: str, attempts: int, retries: bool) -> None:
    """§13.3's table in code, so that "opted out" and "number invalid" cannot
    be retried by an operator setting a schedule."""
    result = next_retry(outcome, last_attempt=NOW, attempts=attempts)
    assert (result is not None) is retries


def test_no_answer_rotates_the_time_of_day_band() -> None:
    """§13.3. A farmer who does not answer at 10am three days running is not
    refusing -- they are in a field at 10am."""
    first = next_retry("no_answer", last_attempt=NOW, attempts=1)
    second = next_retry("no_answer", last_attempt=NOW, attempts=2)
    assert first is not None and second is not None
    assert second > first


# --------------------------------------------------------------------------- #
# §13.2 -- the outbound conversation
# --------------------------------------------------------------------------- #


def _offer() -> Offer:
    return Offer(
        name_hi="रबी ऑफ़र",
        products_hi=("डीएपी", "यूरिया"),
        saving_rupees=Decimal("500"),
        valid_until_hi="30 सितंबर",
        centre_name_hi="बाराबंकी केंद्र",
    )


def test_the_disclosure_names_the_business_and_says_it_is_automated() -> None:
    """§13.2: both, every time. It is a legal posture and the thing that stops
    the call being taken for fraud."""
    disclosure = OutboundAgent(offer=_offer()).disclosure()
    assert "यूए एग्रो" in disclosure
    assert "ऑटोमैटिक" in disclosure


def test_the_offer_is_never_pitched_to_a_third_party() -> None:
    """§13.2. A model told to "deliver the offer" will deliver it to whoever
    picked up, so there is no edge from VERIFY_PERSON to PITCH."""
    from voice_worker.outbound.conversation import TRANSITIONS

    assert OutboundState.PITCH not in TRANSITIONS[OutboundState.VERIFY_PERSON]


def test_the_pitch_is_rendered_from_the_offer_record() -> None:
    """§13.2: never improvised. Every figure a farmer would act on is in the
    record, so a model asked to fill a gap has nothing to invent."""
    rendered = _offer().render()
    assert "500" in rendered
    assert "डीएपी" in rendered
    assert "30 सितंबर" in rendered
    assert "बाराबंकी केंद्र" in rendered
    assert _offer().within_length_cap


@pytest.mark.parametrize(
    ("text", "dtmf", "expected"),
    [
        ("हाँ", None, Reply.AFFIRMATIVE),
        ("", "1", Reply.AFFIRMATIVE),
        ("नहीं", None, Reply.NEGATIVE),
        ("", "2", Reply.NEGATIVE),
        ("दोबारा मत करना", None, Reply.OPT_OUT),
        ("", "9", Reply.OPT_OUT),
        ("वो घर पर नहीं हैं", None, Reply.THIRD_PARTY),
        ("क्या?", None, Reply.UNCLEAR),
    ],
)
def test_dtmf_and_speech_are_handled_identically(
    text: str, dtmf: str | None, expected: Reply
) -> None:
    """§13.2: a farmer in a field cannot press a key, one in a noisy market
    cannot be heard. Both modalities, neither privileged."""
    assert classify_reply(text, dtmf=dtmf) is expected


def test_a_refusal_that_is_also_an_opt_out_is_an_opt_out() -> None:
    """"नहीं, दोबारा मत करना" is both. Treating it as a plain no would leave
    the farmer on the list."""
    assert classify_reply("नहीं, दोबारा मत करना") is Reply.OPT_OUT


async def test_opt_out_is_written_before_the_call_ends() -> None:
    """§13.2: synchronously, never in a background job that might fail.

    A queued write that fails leaves someone who asked to be removed still on
    the list, with a record saying they were removed -- and the next campaign
    calls them again.
    """
    written: list[str] = []

    async def suppress() -> None:
        written.append("internal_dnc")

    agent = OutboundAgent(offer=_offer(), suppress=suppress)
    agent.flow.state = OutboundState.INTEREST_CHECK
    confirmation = await agent.handle_opt_out()

    assert written == ["internal_dnc"], "suppression was not awaited"
    assert agent.flow.opted_out
    assert "हटा" in confirmation


def test_objections_are_capped_at_two_loops() -> None:
    """§13.2, then accept the no. A model asked to "handle objections" has no
    natural stop -- it keeps finding angles, which is how a service call becomes
    a nuisance call."""
    flow = OutboundFlow()
    for _ in range(MAX_OBJECTION_LOOPS):
        assert flow.next_after_interest(Reply.NEGATIVE) is OutboundState.OBJECTION
    assert flow.next_after_interest(Reply.NEGATIVE) is OutboundState.CLOSE


async def test_a_failed_whatsapp_send_is_never_claimed_as_sent() -> None:
    """§13.2. Saying "मैंने भेज दिया है" when nothing was sent is a small lie
    that destroys what the whole call was building."""
    tickets: list[str] = []

    async def failing_send() -> bool:
        return False

    async def create_ticket(reason: str) -> str:
        tickets.append(reason)
        return "TKT-1"

    agent = OutboundAgent(
        offer=_offer(), send_whatsapp=failing_send, create_ticket=create_ticket
    )
    agent.flow.state = OutboundState.CONFIRM
    said = await agent.handle_confirmation()

    assert not agent.flow.whatsapp_sent
    assert agent.flow.spoke_offer_aloud
    assert "भेज दी है" not in said
    # §13.2: read it aloud instead, and raise a ticket.
    assert "500" in said
    assert tickets == ["whatsapp_dispatch_failed"]


async def test_a_send_that_raises_is_treated_as_a_failure() -> None:
    """A vendor exception must not end the call. §11.4 applies to outbound
    too: the farmer is on the line and has just said yes."""

    async def exploding_send() -> bool:
        raise RuntimeError("meta is down")

    agent = OutboundAgent(offer=_offer(), send_whatsapp=exploding_send)
    agent.flow.state = OutboundState.CONFIRM
    said = await agent.handle_confirmation()
    assert agent.flow.spoke_offer_aloud
    assert "500" in said


async def test_a_successful_send_is_announced() -> None:
    async def ok_send() -> bool:
        return True

    agent = OutboundAgent(offer=_offer(), send_whatsapp=ok_send)
    agent.flow.state = OutboundState.CONFIRM
    said = await agent.handle_confirmation()
    assert agent.flow.whatsapp_sent
    assert "व्हाट्सऐप" in said


# --------------------------------------------------------------------------- #
# §14 -- WhatsApp
# --------------------------------------------------------------------------- #


def test_a_dosage_card_is_utility_not_marketing() -> None:
    """§14: the 7.5x difference is the single largest avoidable cost in §8.
    The farmer asked for it, so it is service."""
    assert REGISTRY["dosage_instructions"].category is TemplateCategory.UTILITY
    assert REGISTRY["offer_details"].category is TemplateCategory.MARKETING


def test_the_category_is_not_a_call_site_choice() -> None:
    """It is a property of what the message *is*. A caller free to choose would
    eventually choose wrong, in whichever direction was convenient."""
    import inspect

    assert "category" not in inspect.signature(estimate_cost).parameters


def test_marketing_costs_about_seven_times_utility() -> None:
    marketing = estimate_cost("offer_details", count=1000, on=date(2026, 9, 1))
    utility = estimate_cost("product_price_list", count=1000, on=date(2026, 9, 1))
    assert marketing > utility * 7


def test_a_service_reply_is_free_until_the_window_ends() -> None:
    """§14 says free; Meta's free service window ends 1 October 2026, so a
    projection for an October campaign must not still say zero."""
    before = estimate_cost(
        "ticket_ack", on=FREE_SERVICE_WINDOW_ENDS - timedelta(days=1),
        inside_service_window=True,
    )
    after = estimate_cost(
        "ticket_ack", on=FREE_SERVICE_WINDOW_ENDS, inside_service_window=True
    )
    assert before == Decimal("0.0000")
    assert after > 0


def test_every_template_in_the_spec_exists() -> None:
    """§14 lists seven. A template the code sends but nobody submitted for
    approval fails at Meta, after the farmer was told it is coming."""
    assert set(REGISTRY) == {
        "offer_details",
        "product_price_list",
        "dosage_instructions",
        "centre_location",
        "callback_confirmation",
        "order_status_update",
        "ticket_ack",
    }
    for template in REGISTRY.values():
        assert template.category in RATES


def test_missing_template_parameters_are_caught_before_dispatch() -> None:
    template = REGISTRY["dosage_instructions"]
    problems = validate_parameters(template, {"crop": "गेहूँ"})
    assert any("product" in p for p in problems)
    assert any("phi_days" in p for p in problems)


def test_an_unknown_parameter_is_caught_too() -> None:
    problems = validate_parameters(REGISTRY["ticket_ack"], {"nope": "x"})
    assert any("unknown" in p for p in problems)


def test_a_webhook_signature_is_verified() -> None:
    """§17. A forged delivery receipt can mark a message delivered that never
    arrived -- which is what §13.2's "never claim a message was sent" relies on
    being true."""
    import hashlib
    import hmac as hmac_module

    body = b'{"entry":[]}'
    secret = "app-secret"
    good = hmac_module.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_webhook(body=body, signature=f"sha256={good}", app_secret=secret)
    assert not verify_webhook(body=body, signature=f"sha256={good}", app_secret="no")
    assert not verify_webhook(body=b"other", signature=f"sha256={good}", app_secret=secret)
    assert not verify_webhook(body=body, signature="", app_secret=secret)


def test_an_inbound_reply_is_parsed_and_opens_a_window() -> None:
    """§14: a reply opens the 24-hour service window, so it is routed to a
    ticket for a human rather than answered with a fresh billable template."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "919999000000",
                                    "id": "wamid.1",
                                    "timestamp": str(int(NOW.timestamp())),
                                    "text": {"body": "डीएपी का रेट क्या है"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    messages = parse_webhook(payload)
    assert len(messages) == 1
    assert messages[0].text == "डीएपी का रेट क्या है"
    assert messages[0].window_is_open(now=NOW + timedelta(hours=23))
    assert not messages[0].window_is_open(now=NOW + timedelta(hours=25))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entry": "not a list"},
        {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]},
        {"entry": [{"changes": [{"value": {"messages": [{"type": "image"}]}}]}]},
    ],
)
def test_an_unrecognised_webhook_shape_yields_nothing(payload: dict) -> None:
    """Meta sends status callbacks through the same endpoint. A handler that
    raises on an unexpected shape is one Meta eventually disables for failing."""
    assert parse_webhook(payload) == []


def test_an_inbound_message_knows_when_its_window_closes() -> None:
    message = InboundMessage(
        from_number="919999000000",
        text="hello",
        received_at=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        message_id="wamid.2",
    )
    assert message.window_closes_at() == datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
