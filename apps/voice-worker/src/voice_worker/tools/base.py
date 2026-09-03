"""The tool contract (§6.3).

Tools are the **only** route to facts. §1 N1 is absolute: every factual claim in
a call must trace to a tool call or a retrieved document, and if the data is
missing the agent says so and offers escalation. Nothing in this system lets a
model state a price, a stock figure or a dose from its own knowledge.

Four rules from §6.3, all enforced here rather than left to each tool:

**Arguments are validated before execution.** A malformed argument is a
*refusal*, never a guess. Coercing ``"do bori"`` into ``2`` at this layer would
put an invented quantity behind the audit trail that §18 relies on.

**Every tool has a 150 ms p95 budget and a 400 ms hard timeout.** A timeout is
not an error to propagate -- §11.4 has the agent speak a cached hold phrase and
retry once, then escalate.

**Results are trimmed.** §6.2 targets under 2,000 input tokens per turn and says
tool results are cut to the fields the answer needs. Returning a full ORM row
would spend the context budget on columns no answer mentions.

**Independent tools run in parallel**, capped at two per turn.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import structlog

from uaagro_domain.errors import UAAgroError

log = structlog.get_logger(__name__)

#: §6.3 budgets.
P95_BUDGET_MS = 150
HARD_TIMEOUT_MS = 400
MAX_CALLS_PER_TURN = 2
RETRIES = 1


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who is asking, and on whose behalf.

    Carried explicitly rather than read from a global so a tool cannot widen
    its own scope: the centre and farmer here are the ones RLS will enforce
    anyway, and a tool that ignored them would simply get an empty result.
    """

    call_id: str
    language: str = "hi-IN"
    farmer_id: str | None = None
    centre_id: str | None = None
    organization_id: str | None = None
    #: ``inbound`` or ``outbound``. Retrieval uses it to leave out documents
    #: marked for the other kind of call (§9); tools that do not care ignore
    #: it. ``None`` means "not on a call" -- the panel's test question.
    direction: str | None = None


#: Result fields the loop needs but neither the model nor the call record
#: should see: a hand-over destination is a phone number (§17, §23-6).
_NOT_FOR_THE_RECORD = frozenset({"target_number"})


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool returned, and how long it took."""

    tool: str
    ok: bool
    #: Trimmed payload. Goes into the LLM context, so it carries only what an
    #: answer would mention.
    data: dict[str, Any] = field(default_factory=dict)
    #: Set when the tool could not answer. Phrased for the agent to relay, not
    #: for a developer to read.
    error: str | None = None
    latency_ms: float = 0.0
    timed_out: bool = False
    #: True when the result is grounded in stored data. §1 N1 lets the agent
    #: state a fact only from a result where this holds.
    grounded: bool = True

    @property
    def within_budget(self) -> bool:
        return self.latency_ms <= P95_BUDGET_MS

    def to_dict(self) -> dict[str, Any]:
        """Persisted to ``call_turns.tool_results`` (§10)."""
        payload: dict[str, Any] = {
            "tool": self.tool,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1),
        }
        data = {key: value for key, value in self.data.items() if key not in _NOT_FOR_THE_RECORD}
        if data:
            payload["data"] = data
        if self.error:
            payload["error"] = self.error
        if self.timed_out:
            payload["timed_out"] = True
        return payload


class ValidationFailure(Exception):
    """Arguments did not match the tool's schema."""

    def __init__(self, tool: str, problems: list[str]) -> None:
        super().__init__(f"{tool}: {'; '.join(problems)}")
        self.tool = tool
        self.problems = problems


class Tool(ABC):
    """One LLM-callable capability."""

    #: Name the model calls. Stable: it appears in ``agent_configs.tool_allowlist``.
    name: str
    #: Shown to the model. Says what the tool answers, not how it works.
    description: str
    #: JSON Schema for the arguments.
    parameters: ClassVar[dict[str, Any]]
    #: False for tools that change something. §6.3 runs read-only tools in
    #: parallel; running two writes concurrently could double a ticket.
    read_only: bool = True

    def warmup_args(self) -> Mapping[str, Any] | None:
        """Arguments that compile this tool's query without matching real data.

        ``None`` -- the default -- means the tool is not warmed.

        This exists because of a measurement, not a theory. The first execution
        of ``lookup_farmer`` in a fresh process takes 671 ms against 13 ms for
        the second, and the difference is almost entirely SQLAlchemy compiling
        the ORM statement plus Postgres planning it -- opening the connection
        is 47 ms of it. ``HARD_TIMEOUT_MS`` is 400, so the *first farmer to
        call after a worker restart* hears "that lookup is taking too long"
        while every caller after them is served in single-digit milliseconds.

        The arguments must therefore run the real statement, and must match
        nothing: a warm-up that returns rows would put a real farmer's details
        into a log line nobody asked for.
        """
        return None

    async def warm(self) -> None:
        """Compile this tool's statements. Default: run it once on
        :meth:`warmup_args`.

        A tool whose query path branches before the expensive statement --
        ``lookup_farmer`` returns early for an unknown caller -- overrides this
        so the warm-up reaches the statement that actually costs.
        """
        args = self.warmup_args()
        if args is None:
            return
        await self.run(args, ToolContext(call_id="warmup"))

    @abstractmethod
    async def run(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        """Do the work and return the trimmed payload.

        Raise :class:`~uaagro_domain.errors.UAAgroError` for a condition the
        agent should relay. Anything else is treated as a fault.
        """

    def schema(self) -> dict[str, Any]:
        """The definition handed to the model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def validate(self, args: Mapping[str, Any]) -> None:
        """Check arguments against the schema.

        Raises:
            ValidationFailure: with every problem found, not just the first --
                a model correcting one argument at a time burns turns the
                caller is waiting through.
        """
        problems = validate_against_schema(args, self.parameters)
        if problems:
            raise ValidationFailure(self.name, problems)


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


def validate_against_schema(args: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    """Validate a flat argument object against a JSON Schema subset.

    Deliberately small: tool arguments are flat objects of strings, numbers,
    booleans and enums. A full JSON Schema implementation would accept nested
    constructs no tool uses, and every accepted construct is one more shape an
    argument can take before it reaches a query.
    """
    problems: list[str] = []
    properties: dict[str, Any] = dict(schema.get("properties", {}))
    required: Sequence[str] = schema.get("required", [])

    for name in required:
        if args.get(name) is None:
            problems.append(f"{name!r} is required")

    if schema.get("additionalProperties") is False:
        for name in args:
            if name not in properties:
                problems.append(f"{name!r} is not a recognised argument")

    for name, value in args.items():
        spec = properties.get(name)
        if spec is None or value is None:
            continue
        problems.extend(_check_value(name, value, spec))

    return problems


def _check_value(name: str, value: Any, spec: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    expected = spec.get("type")

    if expected == "string":
        if not isinstance(value, str):
            return [f"{name!r} must be text"]
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{name!r} must be one of: {', '.join(map(str, spec['enum']))}")
        if "minLength" in spec and len(value) < spec["minLength"]:
            problems.append(f"{name!r} is too short")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            problems.append(f"{name!r} is too long")

    elif expected in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, int | float):
            return [f"{name!r} must be a number"]
        if expected == "integer" and not float(value).is_integer():
            problems.append(f"{name!r} must be a whole number")
        if "minimum" in spec and value < spec["minimum"]:
            problems.append(f"{name!r} must be at least {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            problems.append(f"{name!r} must be at most {spec['maximum']}")

    elif expected == "boolean" and not isinstance(value, bool):
        problems.append(f"{name!r} must be true or false")

    elif expected == "array" and not isinstance(value, list):
        problems.append(f"{name!r} must be a list")

    return problems


# --------------------------------------------------------------------------- #
# Registry and execution
# --------------------------------------------------------------------------- #


def _pool_size() -> int:
    """How many connections a warm-up should cover.

    Read from settings rather than hard-coded, and clamped: a large pool would
    otherwise make startup wait on dozens of round trips for diminishing
    returns, and a misconfigured zero would warm nothing.
    """
    try:
        from uaagro_domain.settings import get_settings

        configured = int(get_settings().database_pool_size)
    except Exception:
        return 10
    return max(1, min(configured, 32))


@dataclass
class ToolRegistry:
    """The tools an agent config allows, and the only way to run one."""

    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def allowed(self, allowlist: Sequence[str]) -> list[Tool]:
        """Tools an agent config permits.

        A name in the allowlist with no implementation is a configuration
        error worth surfacing: the model would be offered a capability that
        fails on first use.
        """
        missing = [name for name in allowlist if name not in self.tools]
        if missing:
            log.error("tools.allowlist_unknown", missing=missing)
        return [self.tools[name] for name in allowlist if name in self.tools]

    def schemas(self, allowlist: Sequence[str]) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.allowed(allowlist)]

    def restricted(self, allowlist: Sequence[str]) -> ToolRegistry:
        """A registry holding only what this agent config allows (§10).

        The allowlist used to be loaded from the published config, logged,
        and never applied: every agent could run every tool. An empty
        allowlist keeps the whole registry -- the panel's test question and
        the tests build agents without a config.
        """
        if not allowlist:
            return self
        return ToolRegistry(tools={tool.name: tool for tool in self.allowed(allowlist)})

    async def execute(
        self,
        name: str,
        args: Mapping[str, Any],
        context: ToolContext,
        *,
        timeout_ms: int = HARD_TIMEOUT_MS,
        retries: int = RETRIES,
    ) -> ToolResult:
        """Run one tool with validation, timeout and one retry.

        Never raises. A tool failure becomes a result the agent can speak
        around -- §11.4 requires every failure path to end in an answer, a
        human, or a ticket, and an exception escaping into the audio loop ends
        in none of those.
        """
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(
                tool=name,
                ok=False,
                error=f"No tool named {name!r} is available.",
                grounded=False,
            )

        try:
            tool.validate(args)
        except ValidationFailure as failure:
            # §6.3: a malformed argument is a refusal, never a guess.
            log.info("tools.validation_failed", tool=name, problems=failure.problems)
            return ToolResult(
                tool=name,
                ok=False,
                error="; ".join(failure.problems),
                grounded=False,
            )

        attempt = 0
        while True:
            started = time.perf_counter()
            try:
                data = await asyncio.wait_for(tool.run(args, context), timeout=timeout_ms / 1000)
                elapsed = (time.perf_counter() - started) * 1000
                result = ToolResult(tool=name, ok=True, data=data, latency_ms=elapsed)
                if not result.within_budget:
                    log.info(
                        "tools.over_budget",
                        tool=name,
                        latency_ms=round(elapsed, 1),
                        budget_ms=P95_BUDGET_MS,
                    )
                return result

            except TimeoutError:
                elapsed = (time.perf_counter() - started) * 1000
                if attempt < retries:
                    attempt += 1
                    log.warning("tools.timeout_retrying", tool=name, attempt=attempt)
                    continue
                log.error("tools.timeout", tool=name, timeout_ms=timeout_ms)
                return ToolResult(
                    tool=name,
                    ok=False,
                    error="That lookup is taking too long.",
                    latency_ms=elapsed,
                    timed_out=True,
                    grounded=False,
                )

            except UAAgroError as exc:
                # Domain errors are already phrased for the agent to relay --
                # "no approved recommendation exists", "this product needs a
                # licence". Flattening them into a generic message would lose
                # exactly the distinction the agent needs to answer well, and
                # in the advisory case would turn a safety signal into a shrug.
                elapsed = (time.perf_counter() - started) * 1000
                return ToolResult(
                    tool=name,
                    ok=False,
                    error=exc.message,
                    data={"code": exc.code, "remedy": exc.remedy},
                    latency_ms=elapsed,
                    grounded=False,
                )

            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000
                # A stack trace must never reach the audio path. §11.4 turns
                # this into a hold phrase and, if it repeats, an escalation.
                log.error("tools.failed", tool=name, error=type(exc).__name__)
                return ToolResult(
                    tool=name,
                    ok=False,
                    error="That information is not available right now.",
                    latency_ms=elapsed,
                    grounded=False,
                )

    async def execute_many(
        self,
        calls: Sequence[tuple[str, Mapping[str, Any]]],
        context: ToolContext,
    ) -> list[ToolResult]:
        """Run several tools, in parallel where it is safe.

        §6.3 caps a turn at two calls. Read-only tools go concurrently;
        anything that writes runs in sequence, because two concurrent writes
        could raise the same ticket twice.
        """
        if len(calls) > MAX_CALLS_PER_TURN:
            log.warning("tools.too_many_calls", requested=len(calls), cap=MAX_CALLS_PER_TURN)
            calls = calls[:MAX_CALLS_PER_TURN]

        writes = [c for c in calls if not self._is_read_only(c[0])]
        reads = [c for c in calls if self._is_read_only(c[0])]

        results: list[ToolResult] = []
        if reads:
            results.extend(
                await asyncio.gather(*(self.execute(name, args, context) for name, args in reads))
            )
        for name, args in writes:
            results.append(await self.execute(name, args, context))
        return results

    async def warm(self, *, timeout_ms: int = 5_000, connections: int | None = None) -> list[str]:
        """Compile every warmable read-only tool's query before taking calls.

        Called from worker startup, before the process reports ready (§20). A
        worker that accepts a call with a cold statement cache spends the whole
        §7 budget on compilation and times the tool out -- see
        :meth:`Tool.warmup_args` for the measurement.

        Writes are never warmed, whatever they declare: `read_only` is the
        gate, and a warm-up that raised a ticket would be a ticket nobody asked
        for. The timeout is generous because this is the run that is *expected*
        to be slow, and it is not on the audio path.

        Failures are logged and skipped. A database that is not up yet is a
        readiness problem the health check already owns; refusing to start over
        a cold cache would turn a slow first call into no calls at all.

        ``connections`` is how many pooled connections to warm each statement
        on, and it defaults to the pool size. That is not an optimisation. The
        driver prepares statements **per connection**, so warming one
        connection leaves every other one in the pool cold, and the first call
        that lands on each of them pays the compilation again -- measured at
        268 ms p50 versus 21 ms once the whole pool is warm. With a 400 ms hard
        timeout, that is roughly the first ``pool_size`` callers after a deploy
        being told the lookup is taking too long, rather than just the first.
        """

        depth = connections if connections is not None else _pool_size()

        async def warm_one(name: str, tool: Tool) -> str | None:
            try:
                async with asyncio.timeout(timeout_ms / 1000):
                    # Concurrently across the pool, so every connection
                    # prepares the statement rather than only the first.
                    await asyncio.gather(*(tool.warm() for _ in range(depth)))
            except UAAgroError:
                # "No such SKU" during a warm-up means the statement ran and is
                # now compiled, which is the entire point. Only an unexpected
                # exception is a failed warm-up.
                return name
            except Exception as exc:
                # The tool name and the error type only -- a warm-up traceback
                # would be the first thing in the log on every boot (§23-6).
                log.warning("tools.warm_failed", tool=name, error=type(exc).__name__)
                return None
            return name

        # One tool at a time, each fanned out across the pool. The other order
        # -- every tool at once -- asks for `tools x depth` connections and
        # queues most of them behind a pool that is `depth` wide, which turns
        # the warm-up into a serial one with extra steps.
        warmed = [
            name
            for name, tool in self.tools.items()
            if tool.read_only and await warm_one(name, tool) is not None
        ]
        log.info("tools.warmed", count=len(warmed))
        return warmed

    def _is_read_only(self, name: str) -> bool:
        tool = self.tools.get(name)
        return tool.read_only if tool else True
