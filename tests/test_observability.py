"""Telemetry, alert rules and dashboards (§19, §21 Phase 8).

Dashboards and alert rules are code, so they get tested like code. The failure
these catch is specific and common: a threshold in a YAML file drifts away from
the constant it was copied from, and the alert quietly stops meaning what its
name says. Nobody notices, because an alert that does not fire looks exactly
like a system that is healthy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from uaagro_domain.telemetry import (
    FORBIDDEN_LABELS,
    P50_TARGET_MS,
    P95_CEILING_MS,
    TOTAL_CEILING_MS,
    TOTAL_P50_TARGET_MS,
    TOTAL_P50_WITH_TOOL_TARGET_MS,
    TOTAL_WITH_TOOL_CEILING_MS,
    Instruments,
    Metric,
    Segment,
    evaluate_budget,
    percentile,
)

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "infra" / "prometheus" / "alerts.yml"
DASHBOARDS = ROOT / "infra" / "grafana" / "dashboards"


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #


def test_a_high_cardinality_label_is_refused() -> None:
    """A `call_id` label gives Prometheus one time series per call, which at
    peak is tens of thousands a day. It takes the backend down long before it
    tells anyone anything."""
    instruments = Instruments()
    with pytest.raises(ValueError, match="call_id"):
        instruments.increment(Metric.TRANSFERS, call_id="abc")


@pytest.mark.parametrize("label", sorted(FORBIDDEN_LABELS))
def test_every_forbidden_label_is_actually_refused(label: str) -> None:
    with pytest.raises(ValueError):
        Instruments().record(Metric.TURN_LATENCY, 1.0, **{label: "x"})


def test_a_phone_number_can_never_become_a_metric_label() -> None:
    """§23-6 has no exception for metrics, and a metrics backend is a place
    nobody thinks to look for PII."""
    assert "phone" in FORBIDDEN_LABELS
    assert "phone_hash" in FORBIDDEN_LABELS


def test_ordinary_labels_are_allowed() -> None:
    instruments = Instruments()
    instruments.record(Metric.SEGMENT_LATENCY, 42.0, segment="stt_final", language="hi")
    assert instruments.recorded[-1][0] == Metric.SEGMENT_LATENCY.value


def test_metrics_do_not_require_a_tracing_collector() -> None:
    """§19's metrics are scraped; traces are pushed. They are separate sinks.

    `setup()` used to return early whenever `OTEL_EXPORTER_OTLP_ENDPOINT` was
    unset, so a deployment that scraped Prometheus and ran no collector -- which
    is what §20's compose ships -- got no metrics at all. Requiring a collector
    to be running before any metric exists is the wrong coupling.
    """
    instruments = Instruments()
    instruments.setup(service_name="test", endpoint=None)

    instruments.increment(Metric.BARGE_IN, language="hi")
    assert len(instruments.recorded) == 1

    body, content_type = instruments.exposition()
    assert "text/plain" in content_type
    assert b"uaagro_barge_ins_total" in body


def test_recording_never_raises_without_any_exporter() -> None:
    """Observability that can take down the thing it observes is worse than
    none, so a worker that can export nowhere still serves calls."""
    instruments = Instruments()
    instruments.setup(service_name="test", endpoint=None, prometheus=False)

    assert not instruments.enabled
    instruments.increment(Metric.BARGE_IN, language="hi")
    with instruments.span("turn", call_id="abc"):
        pass
    assert len(instruments.recorded) == 1


def test_a_slow_segment_is_recorded_against_its_ceiling() -> None:
    instruments = Instruments()
    with instruments.timed_segment(Segment.STT_FINAL, language="hi"):
        pass
    name, _value, labels = instruments.recorded[-1]
    assert name == Metric.SEGMENT_LATENCY.value
    assert labels["segment"] == "stt_final"


# --------------------------------------------------------------------------- #
# §7's budget arithmetic
# --------------------------------------------------------------------------- #


def test_the_p50_targets_sum_to_the_stated_totals() -> None:
    """§7's p50 column adds up, and this is the arithmetic check worth having.

    A median turn is roughly the sum of median segments, so a drift here means
    the table and the totals have come apart.
    """
    total = sum(P50_TARGET_MS.values())
    assert total == TOTAL_P50_WITH_TOOL_TARGET_MS
    assert total - P50_TARGET_MS[Segment.TOOL_EXECUTION] == TOTAL_P50_TARGET_MS


def test_the_p95_ceilings_deliberately_exceed_the_total() -> None:
    """And this is why the obvious version of the test above is wrong.

    The p95 ceilings sum to 1,870 ms against a 1,500 ms total, which looks like
    an inconsistency in §7 and is not: the p95 of a sum is not the sum of the
    p95s. Segments do not all reach their 95th percentile on the same turn --
    a turn where the STT was slow is usually one where the LLM was not.

    Asserted explicitly so that nobody "fixes" it later by shrinking the
    segment ceilings, which would force each one far below its real
    distribution and fail builds on turns that were fine.
    """
    assert sum(P95_CEILING_MS.values()) > TOTAL_WITH_TOOL_CEILING_MS

    # Each individual ceiling must still fit inside the total, though. A single
    # segment allowed more than the whole turn would be a genuine error.
    for segment, ceiling in P95_CEILING_MS.items():
        assert ceiling < TOTAL_CEILING_MS, f"{segment.value} alone exceeds the total"


def test_the_percentile_is_nearest_rank() -> None:
    """An interpolated p95 can report a value no turn actually took, which is
    not a number to fail a build on."""
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 0.95) in values
    assert percentile(values, 0.50) in values
    assert percentile([], 0.95) == 0.0


def test_the_budget_verdict_matches_the_ceiling() -> None:
    fast = evaluate_budget([100.0] * 100)
    assert fast.within_budget

    slow = evaluate_budget([2000.0] * 100)
    assert not slow.within_budget
    assert "OVER" in slow.summary()

    # The tool-calling ceiling is higher, so the same numbers can pass there.
    borderline = evaluate_budget([1300.0] * 100, with_tool_call=True)
    assert borderline.within_budget
    assert not evaluate_budget([1300.0] * 100).within_budget


# --------------------------------------------------------------------------- #
# Alert rules as code
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def alerts() -> dict:
    return yaml.safe_load(ALERTS.read_text(encoding="utf-8"))


def test_the_alert_thresholds_match_section_7(alerts: dict) -> None:
    """The specific drift this catches: a threshold copied into YAML and then
    left behind when the constant moved. The alert keeps its name and stops
    meaning what the name says, and an alert that does not fire looks exactly
    like a healthy system.
    """
    rules = {r["alert"]: r for g in alerts["groups"] for r in g["rules"]}

    assert str(TOTAL_CEILING_MS) in rules["TurnLatencyP95Breach"]["expr"]
    assert (
        str(TOTAL_WITH_TOOL_CEILING_MS)
        in rules["TurnLatencyWithToolP95Breach"]["expr"]
    )
    assert (
        str(P95_CEILING_MS[Segment.LLM_TTFT])
        in rules["LlmTimeToFirstTokenSlow"]["expr"]
    )


def test_every_rule_says_what_it_means(alerts: dict) -> None:
    """A page with no summary is a page whose recipient has to read PromQL at
    3am to find out what woke them."""
    for group in alerts["groups"]:
        for rule in group["rules"]:
            annotations = rule.get("annotations", {})
            assert annotations.get("summary"), f"{rule['alert']} has no summary"
            assert rule.get("labels", {}).get("severity") in {"page", "warn"}


def test_paging_rules_point_at_a_runbook_section(alerts: dict) -> None:
    """Every `page` should tell the person what to do. A `warn` can be looked
    at in the morning and does not need one."""
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for group in alerts["groups"]:
        for rule in group["rules"]:
            if rule.get("labels", {}).get("severity") != "page":
                continue
            reference = rule.get("annotations", {}).get("runbook")
            assert reference, f"{rule['alert']} pages with no runbook link"
            anchor = reference.split("#", 1)[1]
            # The anchor must exist. A runbook link to a section nobody wrote
            # is worse than none: it tells the on-call there is a procedure.
            assert f"## {anchor}" in runbook, (
                f"{rule['alert']} links to RUNBOOK.md#{anchor}, which does not exist"
            )


def test_latency_rules_wait_before_firing(alerts: dict) -> None:
    """§7's ceilings are p95 figures over a window. A rule with no `for` fires
    on one slow turn on one rural GSM line, which is a normal Tuesday."""
    for group in alerts["groups"]:
        if group["name"] != "latency":
            continue
        for rule in group["rules"]:
            assert rule.get("for"), f"{rule['alert']} fires instantly"


def test_compliance_and_safety_rules_fire_immediately(alerts: dict) -> None:
    """The opposite rule, for the opposite reason. A call outside the calling
    window is a violation on the first occurrence, and a safety emergency has
    an hour's review window that a `for` clause would eat into."""
    instant = {"CallOutsideCallingWindow", "AuditChainBroken", "SafetyEmergencyTriggered"}
    rules = {r["alert"]: r for g in alerts["groups"] for r in g["rules"]}
    for name in instant:
        assert rules[name].get("for") in (None, "0m"), f"{name} waits before firing"


def test_the_safety_alert_explains_it_is_not_a_fault(alerts: dict) -> None:
    """Somebody woken by this needs to know in the first sentence that the
    system worked and a farmer needs help -- not that something is broken."""
    rules = {r["alert"]: r for g in alerts["groups"] for r in g["rules"]}
    annotations = rules["SafetyEmergencyTriggered"]["annotations"]
    assert "review" in annotations["description"].lower()


# --------------------------------------------------------------------------- #
# Dashboards as code
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path", sorted(DASHBOARDS.glob("*.json")), ids=lambda p: p.stem
)
def test_a_dashboard_is_valid_and_documented(path: Path) -> None:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    assert dashboard["uid"], f"{path.name} has no uid; Grafana cannot update it"
    assert dashboard["panels"], f"{path.name} has no panels"
    # IST, because every timestamp in this system is about when a farmer's
    # phone rang.
    assert dashboard["timezone"] == "Asia/Kolkata"

    for panel in dashboard["panels"]:
        assert panel.get("title"), f"{path.name} has an untitled panel"
        # A panel with no description is one whose meaning lives only in the
        # head of whoever added it.
        assert panel.get("description"), f"{path.name}: {panel['title']} is undocumented"
        assert panel.get("targets"), f"{path.name}: {panel['title']} queries nothing"


def test_the_dashboards_only_query_metrics_that_exist() -> None:
    """A panel querying a metric nobody emits renders an empty graph, which is
    indistinguishable from a metric that is legitimately zero."""
    known = {m.value for m in Metric}
    # Emitted by the API and the worker outside `telemetry.Metric`; listed so
    # this test fails on a typo rather than on a deliberate addition.
    also_emitted = {
        "uaagro_concurrency_capacity",
        "uaagro_calls_total",
        "uaagro_intents_total",
        "uaagro_audit_chain_valid",
        "uaagro_gate_removed_total",
        "uaagro_opt_out_requested_total",
        "uaagro_opt_out_written_total",
        "uaagro_advisory_unapproved",
        "uaagro_recordings_past_retention",
        "uaagro_consent_expiring_14d",
        "uaagro_spend_today_rupees",
        "uaagro_budget_today_rupees",
    }
    allowed = known | also_emitted

    import re

    pattern = re.compile(r"uaagro_[a-z0-9_]+")
    for path in DASHBOARDS.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for name in set(pattern.findall(text)):
            # Histogram queries append _bucket, _count and _sum.
            base = re.sub(r"_(bucket|count|sum)$", "", name)
            assert base in allowed, f"{path.name} queries unknown metric {name}"


# --------------------------------------------------------------------------- #
# §19's scrape target
# --------------------------------------------------------------------------- #


def test_the_metric_names_reach_the_scrape_output() -> None:
    """§19 names these metrics and §20's compose ships a Prometheus that
    scrapes them. Until now `setup()` was called from nowhere, so every one of
    them went to an in-process list and no dashboard could ever have existed.
    """
    instruments = Instruments()
    instruments.setup(service_name="test", endpoint=None)

    instruments.record(Metric.LLM_TTFT, 1251.0, language="hi-IN")
    instruments.record(Metric.TTS_TTFB, 332.0, language="hi-IN")
    instruments.increment(Metric.SAFETY_TRIGGERS, language="hi-IN")

    body = instruments.exposition()[0].decode()

    assert "uaagro_llm_ttft_ms_bucket" in body, "no histogram, so no p95"
    assert "uaagro_tts_ttfb_ms_sum" in body
    assert "uaagro_safety_triggers_total" in body
    assert 'language="hi-IN"' in body


def test_the_correlation_key_stays_off_the_metrics() -> None:
    """§19 makes `call_id` the trace correlation key, which is exactly the
    unbounded-cardinality label that ruins a metrics backend. It belongs on a
    span; `_check_labels` refuses it on a metric."""
    instruments = Instruments()

    with pytest.raises(ValueError, match="call_id"):
        instruments.record(Metric.TURN_LATENCY, 700.0, call_id="abc-123")


def test_every_service_turns_its_instruments_on() -> None:
    """The gap this closed. `Instruments` has existed since Phase 1 with a
    complete OTLP implementation, and no entry point ever called `setup()` --
    so §19 was, in production, a list in memory that nothing read."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for entry in (
        "apps/voice-worker/src/voice_worker/main.py",
        "apps/api/src/api/main.py",
        "apps/worker/src/worker/tasks.py",
    ):
        source = (root / entry).read_text(encoding="utf-8")
        assert "INSTRUMENTS.setup(" in source, f"{entry} never enables telemetry"


def test_setup_is_idempotent() -> None:
    """Lifespans re-enter: uvicorn's reloader, a test booting the app twice.

    Not tidiness. OpenTelemetry refuses to replace an installed MeterProvider
    and merely logs, while each `PrometheusMetricReader()` has already
    registered a collector on prometheus_client's *global* registry -- so a
    second call leaked a collector attached to a provider nothing would read.
    """
    instruments = Instruments()
    instruments.setup(service_name="test", endpoint=None)
    before = instruments.exposition()[0]

    instruments.setup(service_name="test", endpoint=None)
    instruments.setup(service_name="test", endpoint=None)

    assert instruments.enabled
    instruments.record(Metric.TTS_TTFB, 1.0, language="hi")
    assert b"uaagro_tts_ttfb_ms_count" in instruments.exposition()[0]
    assert isinstance(before, bytes)
