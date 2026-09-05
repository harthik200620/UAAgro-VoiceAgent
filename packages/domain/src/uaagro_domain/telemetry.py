"""Metrics and traces (§19).

§19 asks for OpenTelemetry traces with ``call_id`` as the correlation key, one
span per pipeline segment per turn, and a specific list of Prometheus metrics.
Two decisions here shape everything else.

**Metric names and labels are declared, not passed as strings.** A metric
emitted with a typo becomes a second time series nobody alerts on, and it looks
identical to the real one on a dashboard until someone notices the graph went
flat a fortnight ago. Declaring them makes a typo a ``KeyError`` at import.

**Label cardinality is capped by construction.** A ``call_id`` label would give
Prometheus one time series per call, which at UA Agro's peak is tens of
thousands a day and will take the metrics backend down long before it tells
anyone anything. ``call_id`` belongs on the *trace*, where it is the correlation
key, and never on a counter. :func:`_check_labels` refuses the high-cardinality
ones by name.

The exporters are optional. A worker with no OTLP endpoint configured records
into no-op instruments and keeps serving calls -- observability that can take
down the thing it observes is worse than none.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class Metric(StrEnum):
    """§19's Prometheus metrics, by name.

    Declared so a typo is an import-time error rather than a silently duplicated
    time series that looks right on a dashboard.
    """

    CONCURRENCY = "uaagro_calls_in_flight"
    TURN_LATENCY = "uaagro_turn_latency_ms"
    SEGMENT_LATENCY = "uaagro_segment_latency_ms"
    ASR_CONFIDENCE = "uaagro_asr_confidence"
    TOOL_LATENCY = "uaagro_tool_latency_ms"
    TOOL_ERRORS = "uaagro_tool_errors_total"
    LLM_TTFT = "uaagro_llm_ttft_ms"
    TTS_TTFB = "uaagro_tts_ttfb_ms"
    BARGE_IN = "uaagro_barge_ins_total"
    TRANSFERS = "uaagro_transfers_total"
    COST_PER_CALL = "uaagro_call_cost_rupees"
    CAMPAIGN_THROUGHPUT = "uaagro_campaign_calls_total"
    WHATSAPP_DELIVERY = "uaagro_whatsapp_messages_total"
    SAFETY_TRIGGERS = "uaagro_safety_triggers_total"
    VALIDATOR_REJECTIONS = "uaagro_validator_rejections_total"


class Segment(StrEnum):
    """§7's latency budget, segment by segment.

    The same names as the budget table, so a dashboard panel and a build gate
    can refer to the same thing without a translation layer that drifts.
    """

    NETWORK_IN = "network_in"
    TURN_DETECTION = "turn_detection"
    STT_FINAL = "stt_final"
    TOOL_EXECUTION = "tool_execution"
    LLM_TTFT = "llm_ttft"
    TTS_TTFB = "tts_ttfb"
    NETWORK_OUT = "network_out"


#: §7's p95 ceilings, in milliseconds. The source of truth for both the alert
#: rules and the CI latency-regression gate.
P95_CEILING_MS: dict[Segment, int] = {
    Segment.NETWORK_IN: 60,
    Segment.TURN_DETECTION: 250,
    Segment.STT_FINAL: 200,
    Segment.TOOL_EXECUTION: 400,
    Segment.LLM_TTFT: 550,
    Segment.TTS_TTFB: 350,
    Segment.NETWORK_OUT: 60,
}

#: §7's p50 targets, same segments.
#:
#: These *do* sum to the stated totals -- 700 ms without a tool call, 810 ms
#: with one -- because a median turn is roughly the sum of median segments.
P50_TARGET_MS: dict[Segment, int] = {
    Segment.NETWORK_IN: 25,
    Segment.TURN_DETECTION: 120,
    Segment.STT_FINAL: 90,
    Segment.TOOL_EXECUTION: 110,
    Segment.LLM_TTFT: 260,
    Segment.TTS_TTFB: 180,
    Segment.NETWORK_OUT: 25,
}

#: §7's totals.
#:
#: Note that the **p95 ceilings deliberately sum to more than these** -- 1,870 ms
#: against a 1,500 ms total. That is not an inconsistency in §7 and must not be
#: "fixed" by shrinking the segment ceilings.
#:
#: The p95 of a sum is not the sum of the p95s. Segments do not all reach their
#: 95th percentile on the same turn: a turn where the STT was slow is usually one
#: where the LLM was not. Requiring the ceilings to sum would force each segment
#: far below its real distribution and fail builds on turns that were fine.
#:
#: The p50 targets above are the ones that sum, and they are the arithmetic check
#: worth having.
TOTAL_CEILING_MS = 1200
TOTAL_WITH_TOOL_CEILING_MS = 1500
TOTAL_P50_TARGET_MS = 700
TOTAL_P50_WITH_TOOL_TARGET_MS = 810

#: Labels that must never reach a metric.
#:
#: Each is unbounded, and an unbounded label is one time series per distinct
#: value. `call_id` at peak is tens of thousands a day; `phone` is both
#: unbounded and a §23-6 violation in the metrics backend, which is a place
#: nobody thinks to look for PII.
FORBIDDEN_LABELS = frozenset(
    {
        "call_id",
        "call_ref",
        "phone",
        "phone_hash",
        "farmer_id",
        "transcript",
        "stream_sid",
        "session_id",
        "user_id",
    }
)


def _check_labels(labels: Mapping[str, str]) -> None:
    forbidden = set(labels) & FORBIDDEN_LABELS
    if forbidden:
        # Raised, not warned. A high-cardinality label reaching Prometheus is
        # not something to notice later -- it degrades the backend for
        # everything else, and the fix is a redeploy.
        raise ValueError(
            f"{', '.join(sorted(forbidden))} must not be a metric label: "
            "unbounded cardinality. Put it on the trace instead."
        )


@dataclass
class Instruments:
    """Where measurements go.

    A thin façade over OpenTelemetry so the worker does not import it directly
    and so an unconfigured deployment records into nothing rather than failing.
    Observability that can take down the thing it observes is worse than none.
    """

    enabled: bool = False
    _meter: Any = field(default=None, repr=False)
    _tracer: Any = field(default=None, repr=False)
    _counters: dict[str, Any] = field(default_factory=dict, repr=False)
    _histograms: dict[str, Any] = field(default_factory=dict, repr=False)
    #: Populated whether or not exporters are configured, so tests and the load
    #: harness can assert on what was recorded without a collector.
    recorded: list[tuple[str, float, dict[str, str]]] = field(default_factory=list)

    def setup(
        self,
        *,
        service_name: str,
        endpoint: str | None,
        prometheus: bool = True,
    ) -> None:
        """Wire the exporters (§19).

        Two independent sinks, because deployments differ and requiring both
        would leave one of them blind:

        **Prometheus**, on by default. §19's metrics are scraped from
        ``/metrics``; §20's compose ships Prometheus and Grafana and neither
        needs a collector to be running. This used to be unreachable -- the
        whole method returned early when no OTLP endpoint was configured, so a
        deployment that scraped and did not trace got no metrics at all.

        **OTLP**, only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. Traces are
        where ``call_id`` belongs: §19 makes it the correlation key, and it is
        exactly the unbounded-cardinality label that ruins a metrics backend.

        Never raises. Observability that can take down the thing it observes is
        worse than none, so a missing SDK or an unreachable collector degrades
        to recording in-process and says so once.
        """
        if self.enabled:
            # Idempotent by contract, because it is called from a lifespan and
            # lifespans re-enter: uvicorn's reloader, a test that boots the app
            # twice, a worker restarted in-process.
            #
            # Not merely tidiness. OpenTelemetry refuses to replace an
            # installed MeterProvider -- it logs and carries on -- while each
            # `PrometheusMetricReader()` has already registered a collector on
            # prometheus_client's *global* registry. A second call therefore
            # leaks a collector attached to a provider nothing will ever read.
            log.debug("telemetry.already_enabled", service=service_name)
            return

        try:
            from opentelemetry import metrics, trace
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            # The SDK is an optional dependency. A worker without it serves
            # calls fine and says so once, rather than crash-looping.
            log.warning("telemetry.sdk_missing", endpoint=endpoint)
            return

        resource = Resource.create({"service.name": service_name})
        readers: list[Any] = []

        if prometheus:
            try:
                from opentelemetry.exporter.prometheus import PrometheusMetricReader

                readers.append(PrometheusMetricReader())
            except ImportError:
                log.warning("telemetry.prometheus_missing")

        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.sdk.metrics.export import (
                    PeriodicExportingMetricReader,
                )
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
            except ImportError:
                log.warning("telemetry.otlp_missing", endpoint=endpoint)
            else:
                provider = TracerProvider(resource=resource)
                # Batched, not synchronous: a span export inside a turn would
                # put a collector's latency on the caller's critical path.
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
                trace.set_tracer_provider(provider)
                self._tracer = trace.get_tracer(service_name)
                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint)))

        if not readers and self._tracer is None:
            log.info("telemetry.disabled", reason="no exporter available")
            return

        if readers:
            metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
            self._meter = metrics.get_meter(service_name)

        self.enabled = True
        log.info(
            "telemetry.enabled",
            service=service_name,
            prometheus=prometheus,
            otlp=bool(endpoint),
        )

    def exposition(self) -> tuple[bytes, str]:
        """The current metrics, in Prometheus text format (§19).

        Served from ``/metrics``. Returns empty rather than raising when the
        client is absent, so a scrape against a worker built without it gets an
        empty page instead of a 500 that pages somebody.
        """
        try:
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        except ImportError:
            return b"", "text/plain; charset=utf-8"
        return generate_latest(), CONTENT_TYPE_LATEST

    def record(self, metric: Metric, value: float, **labels: str) -> None:
        """Record one measurement."""
        _check_labels(labels)
        self.recorded.append((metric.value, value, dict(labels)))
        if not self.enabled or self._meter is None:
            return
        histogram = self._histograms.get(metric.value)
        if histogram is None:
            histogram = self._meter.create_histogram(metric.value)
            self._histograms[metric.value] = histogram
        histogram.record(value, attributes=labels)

    def increment(self, metric: Metric, **labels: str) -> None:
        _check_labels(labels)
        self.recorded.append((metric.value, 1.0, dict(labels)))
        if not self.enabled or self._meter is None:
            return
        counter = self._counters.get(metric.value)
        if counter is None:
            counter = self._meter.create_counter(metric.value)
            self._counters[metric.value] = counter
        counter.add(1, attributes=labels)

    @contextmanager
    def span(self, name: str, **attributes: str) -> Iterator[None]:
        """One trace span.

        ``call_id`` belongs here and not on a metric: §19 makes it the
        correlation key, which is exactly the unbounded-cardinality property
        that ruins a metrics backend and makes a trace useful.
        """
        if not self.enabled or self._tracer is None:
            yield
            return
        with self._tracer.start_as_current_span(name) as current:
            for key, value in attributes.items():
                current.set_attribute(key, value)
            yield

    @contextmanager
    def timed_segment(self, segment: Segment, *, language: str = "unknown") -> Iterator[None]:
        """Time one §7 segment and record it against the budget."""
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.record(
                Metric.SEGMENT_LATENCY,
                elapsed_ms,
                segment=segment.value,
                language=language,
            )
            ceiling = P95_CEILING_MS[segment]
            if elapsed_ms > ceiling:
                # One line per breach, at info. §19 alerts on the p95 rather
                # than on individual slow turns -- a single slow segment on a
                # rural line is ordinary, and warning on each would drown the
                # signal that matters.
                log.info(
                    "latency.segment_over_ceiling",
                    segment=segment.value,
                    elapsed_ms=round(elapsed_ms, 1),
                    ceiling_ms=ceiling,
                )


#: The process-wide instruments. A module global because metrics are inherently
#: process-scoped and threading a handle through every call site would put
#: observability plumbing into the signature of every function it touches.
INSTRUMENTS = Instruments()


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated, matching ``runtime.metrics``: §7's
    ceilings are pass/fail thresholds, and an interpolated p95 can report a
    value no turn actually took -- which is not a number to fail a build on.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(p * len(ordered)) - 1))
    return ordered[index]


@dataclass
class BudgetReport:
    """Whether a run met §7. Used by the load test and the CI gate."""

    samples: int
    p50_ms: float
    p95_ms: float
    ceiling_ms: int
    per_segment_p95: dict[str, float] = field(default_factory=dict)

    @property
    def within_budget(self) -> bool:
        return self.p95_ms <= self.ceiling_ms

    def summary(self) -> str:
        verdict = "within" if self.within_budget else "OVER"
        return (
            f"{self.samples} turns: p50 {self.p50_ms:.0f} ms, "
            f"p95 {self.p95_ms:.0f} ms, {verdict} the {self.ceiling_ms} ms ceiling"
        )


def evaluate_budget(
    turn_latencies_ms: list[float], *, with_tool_call: bool = False
) -> BudgetReport:
    """§7's gate, computed the same way in CI and in the load test."""
    ceiling = TOTAL_WITH_TOOL_CEILING_MS if with_tool_call else TOTAL_CEILING_MS
    return BudgetReport(
        samples=len(turn_latencies_ms),
        p50_ms=percentile(turn_latencies_ms, 0.50),
        p95_ms=percentile(turn_latencies_ms, 0.95),
        ceiling_ms=ceiling,
    )


__all__ = (
    "FORBIDDEN_LABELS",
    "INSTRUMENTS",
    "P50_TARGET_MS",
    "P95_CEILING_MS",
    "TOTAL_CEILING_MS",
    "TOTAL_P50_TARGET_MS",
    "TOTAL_P50_WITH_TOOL_TARGET_MS",
    "TOTAL_WITH_TOOL_CEILING_MS",
    "BudgetReport",
    "Instruments",
    "Metric",
    "Segment",
    "evaluate_budget",
    "percentile",
)
