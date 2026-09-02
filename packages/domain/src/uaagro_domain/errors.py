"""Typed, actionable errors.

§22: every user-facing error names what to do next. These carry a stable
``code`` for the API envelope and a ``remedy`` sentence for the operator, so a
centre manager reading a failure in the admin panel is told the fix rather than
a stack trace.

The audio path never raises these to the caller -- the pipeline converts them to
a cached hold phrase and an escalation (§11.4). They surface in logs, the call
record and the admin panel.
"""

from __future__ import annotations

from typing import Any


class UAAgroError(Exception):
    """Base class. Never raised directly."""

    code: str = "uaagro_error"
    http_status: int = 500

    def __init__(self, message: str, *, remedy: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy
        self.context: dict[str, Any] = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remedy": self.remedy,
            "context": self.context,
        }

    def __str__(self) -> str:
        return f"{self.message} -- {self.remedy}"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class ConfigurationError(UAAgroError):
    """A required setting is absent or malformed.

    §0 rule 4: wire the integration fully and fail loudly naming the variable,
    rather than degrading silently.
    """

    code = "configuration_error"
    http_status = 500


class MissingCredentialError(ConfigurationError):
    code = "missing_credential"

    def __init__(self, variable: str, *, needed_for: str) -> None:
        super().__init__(
            f"{variable} is not set, and it is required for {needed_for}.",
            remedy=f"Set {variable} in the environment (.env locally, Secrets Manager in "
            f"production) and restart the service.",
            context={"variable": variable, "needed_for": needed_for},
        )
        self.variable = variable


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class ValidationError(UAAgroError):
    code = "validation_error"
    http_status = 422


class InvalidArgumentError(ValidationError):
    """A tool argument the caller supplied is unusable.

    Distinct from a schema failure, which is caught before the tool runs: this
    is an argument that is well-formed but wrong -- an id that is not a UUID, a
    field the model sent as a string where an object was meant. §6.3 makes both
    a refusal rather than a guess, so the remedy names the argument to re-send.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(
            f"{field} {reason}.",
            remedy=f"Supply a valid {field} and call the tool again.",
            context={"field": field},
        )


class InvalidPhoneNumberError(ValidationError):
    code = "invalid_phone_number"

    def __init__(self, reason: str) -> None:
        # The number itself is deliberately absent from the message: §23-6
        # forbids logging a full phone number, and errors reach the logs.
        super().__init__(
            f"Phone number is not a valid Indian mobile number: {reason}.",
            remedy="Supply a 10-digit Indian mobile number starting 6-9, optionally "
            "prefixed with +91, 91 or 0.",
            context={"reason": reason},
        )


class UnitConversionError(ValidationError):
    code = "unit_conversion_error"

    def __init__(self, message: str, *, remedy: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message, remedy=remedy, context=context)


# --------------------------------------------------------------------------- #
# Safety and grounding -- the highest-severity class in the system
# --------------------------------------------------------------------------- #


class SafetyError(UAAgroError):
    code = "safety_error"
    http_status = 409


class UnapprovedAdvisoryError(SafetyError):
    """An advisory row exists but no agronomist has approved it.

    §9 / §16.2 / KB §5: an unapproved dose is never served. The agent says it
    does not know and escalates. This is the single most important safety
    control in the platform, so it is an exception rather than a filtered query
    result -- a caller must never receive silence where a dose was expected.
    """

    code = "unapproved_advisory"

    def __init__(self, *, crop: str, stage: str | None = None) -> None:
        super().__init__(
            f"No agronomist-approved recommendation exists for crop={crop!r} stage={stage!r}.",
            remedy="Approve the matching row in Crop Advisory, or let the agent escalate "
            "to the centre manager. Never serve an unapproved dose.",
            context={"crop": crop, "stage": stage},
        )


class RestrictedProductError(SafetyError):
    """§16.2: a restricted or licence-requiring product is never recommended."""

    code = "restricted_product"

    def __init__(self, *, sku: str) -> None:
        super().__init__(
            f"Product {sku} is restricted or requires a licence and cannot be "
            f"recommended by the agent.",
            remedy="Route the caller to a human. Restricted products are sold only with "
            "a licence check the agent cannot perform.",
            context={"sku": sku},
        )


class UngroundedClaimError(SafetyError):
    """The post-generation validator caught a price or quantity with no
    supporting tool result this turn (§16.3, §1 N1)."""

    code = "ungrounded_claim"

    def __init__(self, *, claim: str) -> None:
        super().__init__(
            f"Generated response contains an unsupported factual claim: {claim!r}.",
            remedy="Regenerate once; on a second failure speak the cached safe phrase and "
            "escalate. Never let an ungrounded price or dose reach the caller.",
            context={"claim": claim},
        )


# --------------------------------------------------------------------------- #
# Compliance -- platform-enforced, not operator-overridable (§18)
# --------------------------------------------------------------------------- #


class ComplianceError(UAAgroError):
    code = "compliance_error"
    http_status = 403


class OutsideCallingWindowError(ComplianceError):
    code = "outside_calling_window"

    def __init__(self, *, now_local: str, window: str) -> None:
        super().__init__(
            f"Dialling is not permitted at {now_local}; the window is {window} "
            f"recipient local time.",
            remedy="The dialer resumes automatically inside the window. This limit cannot "
            "be overridden by an operator.",
            context={"now_local": now_local, "window": window},
        )


class ConsentError(ComplianceError):
    code = "consent_error"

    def __init__(self, *, reason: str) -> None:
        super().__init__(
            f"Contact cannot be called: {reason}.",
            remedy="Re-acquire consent through a lawful channel, or remove the contact "
            "from the campaign. Consent expires and is not permanent.",
            context={"reason": reason},
        )


class CallerIdSeriesError(ComplianceError):
    code = "caller_id_series"

    def __init__(self, *, cli: str, required_series: str) -> None:
        super().__init__(
            f"Caller ID {cli} is not in the {required_series}-series required for "
            f"promotional calling.",
            remedy=f"Assign a {required_series}-series CLI to the campaign. A plain "
            "10-digit mobile number cannot be used for commercial calling.",
            context={"cli": cli, "required_series": required_series},
        )


class ApprovalRequiredError(ComplianceError):
    code = "approval_required"

    def __init__(self, *, what: str) -> None:
        super().__init__(
            f"{what} requires approval by a second user before it can run.",
            remedy="Ask an ops_manager or super_admin who did not create it to approve. "
            "Four-eyes is enforced; a creator cannot approve their own campaign.",
            context={"what": what},
        )


# --------------------------------------------------------------------------- #
# Vendors and the audio path
# --------------------------------------------------------------------------- #


class VendorError(UAAgroError):
    code = "vendor_error"
    http_status = 502


class VendorUnavailableError(VendorError):
    """Wraps any provider construction or connection failure so one bad vendor
    degrades to its fallback rather than dropping the call (§11.4)."""

    code = "vendor_unavailable"

    def __init__(self, *, vendor: str, service: str, detail: str) -> None:
        super().__init__(
            f"{vendor} {service} is unavailable: {detail}",
            remedy="The pipeline falls back to the secondary provider. If this persists, "
            "check the vendor status page and the credential expiry.",
            context={"vendor": vendor, "service": service, "detail": detail},
        )


class VendorTimeoutError(VendorError):
    code = "vendor_timeout"
    http_status = 504

    def __init__(self, *, vendor: str, service: str, timeout_ms: int) -> None:
        super().__init__(
            f"{vendor} {service} exceeded {timeout_ms} ms.",
            remedy="The pipeline speaks a cached hold phrase and retries once, then "
            "escalates to a human. Check the latency dashboard for a vendor-side spike.",
            context={"vendor": vendor, "service": service, "timeout_ms": timeout_ms},
        )


class ToolExecutionError(UAAgroError):
    code = "tool_execution_error"
    http_status = 500

    def __init__(self, *, tool: str, detail: str) -> None:
        super().__init__(
            f"Tool {tool} failed: {detail}",
            remedy="The agent speaks a hold phrase and retries once, then escalates. "
            "A repeated failure here is a P1 -- the agent cannot answer without tools.",
            context={"tool": tool, "detail": detail},
        )


class BudgetExceededError(UAAgroError):
    """§8 hard guard: a runaway call is terminated with an apology and a ticket."""

    code = "budget_exceeded"
    http_status = 402

    def __init__(self, *, scope: str, limit_inr: float, spent_inr: float) -> None:
        super().__init__(
            f"{scope} cost {spent_inr:.2f} INR exceeded the {limit_inr:.2f} INR ceiling.",
            remedy="The call is closed politely and a ticket raised. Raise the ceiling in "
            "Settings only after checking why the call ran long.",
            context={"scope": scope, "limit_inr": limit_inr, "spent_inr": spent_inr},
        )


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #


class AuthenticationError(UAAgroError):
    code = "authentication_error"
    http_status = 401

    def __init__(self, message: str = "Authentication failed.") -> None:
        # Deliberately uniform: never reveal whether the account exists.
        super().__init__(
            message,
            remedy="Check the email, password and authenticator code. After repeated "
            "failures the account locks temporarily.",
        )


class MFARequiredError(AuthenticationError):
    code = "mfa_required"

    def __init__(self) -> None:
        super().__init__("Multi-factor authentication is required.")
        self.remedy = "Complete TOTP enrolment, then sign in with your authenticator code."


class AuthorizationError(UAAgroError):
    code = "authorization_error"
    http_status = 403

    def __init__(self, *, action: str, resource: str) -> None:
        super().__init__(
            f"Your role may not {action} {resource}.",
            remedy="Ask a super_admin to grant the role or the centre access you need.",
            context={"action": action, "resource": resource},
        )


class NotFoundError(UAAgroError):
    code = "not_found"
    http_status = 404

    def __init__(self, *, resource: str, identifier: str) -> None:
        super().__init__(
            f"{resource} {identifier} was not found.",
            remedy="Check the identifier. If you expected access, confirm the record "
            "belongs to a centre you are assigned to.",
            context={"resource": resource, "identifier": identifier},
        )


class ConflictError(UAAgroError):
    code = "conflict"
    http_status = 409

    def __init__(self, *, message: str, remedy: str) -> None:
        super().__init__(message, remedy=remedy)


class RateLimitedError(UAAgroError):
    code = "rate_limited"
    http_status = 429

    def __init__(self, *, retry_after_s: int) -> None:
        super().__init__(
            f"Too many requests. Retry in {retry_after_s} seconds.",
            remedy="Slow the request rate. Bulk work belongs in an export or a background "
            "job rather than a tight loop against the API.",
            context={"retry_after_s": retry_after_s},
        )


class ServiceUnavailableError(UAAgroError):
    """A backing service the request needs is unreachable.

    Raised at the boundary that does the I/O, so every endpoint reports an
    outage the same way instead of leaking a driver traceback -- which would
    also disclose the DSN and schema to an unauthenticated caller.
    """

    code = "service_unavailable"
    http_status = 503

    def __init__(self, *, service: str, detail: str = "") -> None:
        super().__init__(
            f"{service} is not reachable." + (f" ({detail})" if detail else ""),
            remedy=f"Check that {service} is running and its connection settings are "
            f"correct. Locally, `make dev` brings the stack up.",
            context={"service": service},
        )


class AuditChainError(UAAgroError):
    """The hash chain over ``audit_log`` does not verify (§17)."""

    code = "audit_chain_broken"
    http_status = 500

    def __init__(self, *, at_row: str) -> None:
        super().__init__(
            f"Audit hash chain is broken at row {at_row}.",
            remedy="Treat as a potential tampering incident: preserve the database, alert "
            "the security owner, and follow the runbook before writing further audit rows.",
            context={"at_row": at_row},
        )
