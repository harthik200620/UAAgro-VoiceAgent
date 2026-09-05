"""Refuse to start a production stack that is not ready to be one.

    uv run python scripts/deploy_preflight.py --env-file .env [--check-dns]

Reads the env file the compose stack will read, and checks what the
services themselves cannot check until they are already running: that no
placeholder is left, that every public address is https, that the stack's
internal addresses point at the compose services and not at localhost, that
the controls production requires are set, and -- with ``--check-dns`` -- that
the two hostnames resolve to this machine, without which Caddy cannot obtain
a certificate and the first start ends in a certificate loop.

It runs with no dependencies beyond the standard library, so it works inside
``python:3.12-slim`` on a host that has nothing else installed. The values
are never printed: a failing line names the variable and the rule.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import sys
import urllib.request
from pathlib import Path

PLACEHOLDERS = {"", "FILL_ME", "changeme", "CHANGE_ME", "<redacted>", "..."}

#: Required in every production install, whatever the provider.
REQUIRED = (
    "APP_ENV",
    "PANEL_HOST",
    "VOICE_HOST",
    "ACME_EMAIL",
    "PUBLIC_BASE_URL",
    "TELEPHONY_PROVIDER",
    "TELEPHONY_WS_TOKEN",
    "TELEPHONY_IP_ALLOWLIST",
    "INTERNAL_API_TOKEN",
    "SONIOX_API_KEY",
    "BAKBAK_API_KEY",
    "ANTHROPIC_API_KEY",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "DATABASE_URL",
    "DATABASE_URL_MIGRATOR",
    "REDIS_URL",
    "STORAGE_BACKEND",
    "S3_ENDPOINT",
    "S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "PHONE_HASH_PEPPER",
    "JWT_SIGNING_KEY",
    "ADMIN_SESSION_SECRET",
    "SESSION_COOKIE_SECURE",
)
#: Per telephony provider.
PROVIDER_REQUIRED = {
    "exotel": ("EXOTEL_SID", "EXOTEL_API_KEY", "EXOTEL_API_TOKEN", "EXOTEL_APP_ID", "INBOUND_DID"),
    "twilio": (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_API_KEY_SID",
        "TWILIO_API_KEY_SECRET",
        "TWILIO_FROM_NUMBER",
    ),
    "plivo": ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"),
}
#: Secrets that must be long enough to be secrets.
MIN_SECRET_LENGTH = {
    "TELEPHONY_WS_TOKEN": 24,
    "INTERNAL_API_TOKEN": 24,
    "JWT_SIGNING_KEY": 32,
    "ADMIN_SESSION_SECRET": 32,
    "PHONE_HASH_PEPPER": 32,
    "POSTGRES_PASSWORD": 16,
    "REDIS_PASSWORD": 16,
    "S3_SECRET_ACCESS_KEY": 16,
}
#: Where a compose-internal address must point.
INTERNAL_HOSTS = {
    "DATABASE_URL": "@postgres:",
    "DATABASE_URL_MIGRATOR": "@postgres:",
    "REDIS_URL": "@redis:",
    "VOICE_WORKER_URL": "://voice-worker:",
}

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        found = _LINE.match(line)
        if found is None:
            continue
        key, value = found.group(1), found.group(2)
        # A trailing "# comment" after the value, as the example files use.
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def is_placeholder(value: str) -> bool:
    """FILL_ME, empty, or an angle-bracket stand-in such as ``<POSTGRES_PASSWORD>``."""
    stripped = value.strip()
    return stripped in PLACEHOLDERS or "FILL_ME" in value or ("<" in value and ">" in value)


def truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def fail(self, variable: str, rule: str) -> None:
        self.problems.append(f"{variable}: {rule}")

    def warn(self, variable: str, rule: str) -> None:
        self.warnings.append(f"{variable}: {rule}")


def check(env: dict[str, str], *, check_dns: bool) -> Report:
    report = Report()

    def get(key: str) -> str:
        return env.get(key, "")

    if get("APP_ENV") != "production":
        report.fail("APP_ENV", "must be production for a production stack")

    for key in REQUIRED:
        if is_placeholder(get(key)):
            report.fail(key, "is required and still a placeholder")

    provider = get("TELEPHONY_PROVIDER").strip().lower()
    for key in PROVIDER_REQUIRED.get(provider, ()):
        if is_placeholder(get(key)):
            report.fail(key, f"is required when TELEPHONY_PROVIDER={provider}")
    if provider == "simulator":
        report.fail("TELEPHONY_PROVIDER", "simulator is a development setting; nothing is dialled")

    for key, minimum in MIN_SECRET_LENGTH.items():
        value = get(key)
        if value and not is_placeholder(value) and len(value) < minimum:
            report.fail(key, f"is shorter than {minimum} characters; use openssl rand")

    # -- the edge ----------------------------------------------------------- #
    for key in ("PANEL_HOST", "VOICE_HOST"):
        host = get(key)
        if host and not is_placeholder(host):
            if "://" in host or "/" in host:
                report.fail(key, "is a hostname, not a URL (no scheme, no path)")
            elif host in ("localhost", "127.0.0.1") or host.endswith(".local"):
                report.fail(key, "must be a public DNS name for a certificate to be issued")
    if not is_placeholder(get("PANEL_HOST")) and get("PANEL_HOST") == get("VOICE_HOST"):
        report.fail("VOICE_HOST", "must differ from PANEL_HOST: the two are routed by hostname")
    email = get("ACME_EMAIL")
    if email and not is_placeholder(email) and "@" not in email:
        report.fail("ACME_EMAIL", "is not an email address")

    for key in ("PUBLIC_BASE_URL", "VOICE_WORKER_PUBLIC_URL", "API_PUBLIC_URL"):
        url = get(key)
        if not url or is_placeholder(url):
            continue
        if not url.startswith("https://"):
            report.fail(key, "must be https:// -- the provider streams audio over it")
        if key == "PUBLIC_BASE_URL" and get("VOICE_HOST") and get("VOICE_HOST") not in url:
            report.fail(key, "must be https://<VOICE_HOST>: that is where the media socket answers")
    if get("DEMO_PUBLIC_URL"):
        report.warn(
            "DEMO_PUBLIC_URL", "is set; the demo backend is not part of the production stack"
        )

    # -- the services see each other on the compose network ------------------- #
    for key, needle in INTERNAL_HOSTS.items():
        value = get(key)
        if value and not is_placeholder(value) and needle not in value:
            report.fail(
                key,
                f"must point at the compose service ({needle.strip('@:/')}), not a host address",
            )
    pg = get("POSTGRES_PASSWORD")
    if pg and not is_placeholder(pg):
        for key in ("DATABASE_URL", "DATABASE_URL_MIGRATOR"):
            if get(key) and f":{pg}@" not in get(key):
                report.fail(key, "does not carry POSTGRES_PASSWORD")
    rp = get("REDIS_PASSWORD")
    if rp and not is_placeholder(rp) and get("REDIS_URL") and f":{rp}@" not in get("REDIS_URL"):
        report.fail("REDIS_URL", "does not carry REDIS_PASSWORD (redis://:<password>@redis:6379/0)")

    # -- controls the services refuse to start without ------------------------ #
    if not truthy(get("SESSION_COOKIE_SECURE")):
        report.fail("SESSION_COOKIE_SECURE", "must be true behind TLS")
    if get("STORAGE_BACKEND").lower() != "s3":
        report.fail(
            "STORAGE_BACKEND", "must be s3; a recording on a container disk is lost with it"
        )
    if truthy(get("OUTBOUND_QUICK_DIAL_SELF_APPROVE")):
        report.fail("OUTBOUND_QUICK_DIAL_SELF_APPROVE", "must be false; the four-eyes rule holds")
    if truthy(get("ALLOW_LOCAL_DEK_IN_PRODUCTION")):
        if is_placeholder(get("LOCAL_DEK_BASE64")):
            report.fail(
                "LOCAL_DEK_BASE64", "is required when ALLOW_LOCAL_DEK_IN_PRODUCTION is true"
            )
        report.warn(
            "ALLOW_LOCAL_DEK_IN_PRODUCTION",
            "the phone-number data key lives in .env, not a KMS; keep .env readable by root only",
        )
    elif is_placeholder(get("KMS_KEY_ID")):
        report.fail(
            "KMS_KEY_ID",
            "is required, or set ALLOW_LOCAL_DEK_IN_PRODUCTION=true with LOCAL_DEK_BASE64",
        )
    allow = get("TELEPHONY_IP_ALLOWLIST")
    if allow and not is_placeholder(allow):
        for entry in allow.split(","):
            try:
                ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError:
                report.fail("TELEPHONY_IP_ALLOWLIST", f"'{entry.strip()}' is not a CIDR")
    if not truthy(get("LOG_JSON")):
        report.warn("LOG_JSON", "is false; structured logs are easier to ship and search")

    if check_dns:
        _check_dns(env, report)
    return report


def _public_ip() -> str | None:
    """This host's public address, or None when it cannot be learnt."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=4) as response:  # noqa: S310 - fixed https hosts
                text: str = response.read().decode("ascii", "ignore").strip()
            ipaddress.ip_address(text)
            return text
        except Exception:  # noqa: S112 - each lookup service is a best effort; the next is tried
            continue
    return None


def _check_dns(env: dict[str, str], report: Report) -> None:
    public = _public_ip()
    if public is None:
        report.warn(
            "DNS", "could not learn this host's public address; skipping the hostname check"
        )
        return
    for key in ("PANEL_HOST", "VOICE_HOST"):
        host = env.get(key, "")
        if not host or is_placeholder(host):
            continue
        try:
            resolved = sorted(
                {
                    str(info[4][0])
                    for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
                }
            )
        except socket.gaierror:
            report.fail(key, f"{host} does not resolve; add an A record pointing at {public}")
            continue
        if public not in resolved:
            report.fail(
                key, f"{host} resolves to {', '.join(resolved)}, not to this host ({public})"
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", default=".env", type=Path)
    parser.add_argument(
        "--check-dns", action="store_true", help="also verify the hostnames point here"
    )
    args = parser.parse_args(argv)

    if not args.env_file.is_file():
        print(f"error: {args.env_file} does not exist", file=sys.stderr)
        return 2
    report = check(read_env(args.env_file), check_dns=args.check_dns)
    for line in report.warnings:
        print(f"warning: {line}")
    for line in report.problems:
        print(f"error: {line}")
    if report.problems:
        print(f"\n{len(report.problems)} problem(s) in {args.env_file}; the stack must not start.")
        return 1
    print(
        f"ok: {args.env_file} is ready for a production start"
        + (" (DNS verified)" if args.check_dns else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
