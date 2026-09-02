"""Refuse a commit that carries a secret (§17).

Runs as a pre-commit hook over the staged files, or over any paths given on
the command line. Two kinds of finding:

**Known shapes.** Vendor key prefixes and private-key headers. These are
never false positives: a string starting ``sk-ant-`` in a source file is a
key, whatever the file is.

**High entropy.** Long runs of base64 or hex that look like key material.
These can be legitimate -- a content hash in a test, a lockfile -- so the
scan skips lockfiles and fixture hashes and asks for a ``# not-a-secret``
marker on a line that carries one on purpose.

``.env.example`` is scanned like everything else; it passes only because its
values are ``FILL_ME`` or empty, which is the point of the file.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

KNOWN = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "an Anthropic API key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "an API key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "a GitHub token"),
    (
        # The marker sits on the pattern's own line: the pattern matches itself.
        re.compile(r"postgres(?:ql)?://[^:\s]+:[^@\s]{6,}@", re.IGNORECASE),  # not-a-secret
        "a database password in a DSN",
    ),
    (
        re.compile(r"redis://[^:\s]*:[^@\s]{6,}@", re.IGNORECASE),  # not-a-secret
        "a Redis password in a URL",
    ),
]

#: A value made only of letters, spaces and slashes is a name or a path
#: ("Asia/Kolkata"), never key material.
WORDLIKE = re.compile(r"^[A-Za-z_/ .-]+$")

#: A DSN whose password is the word "password", an angle-bracket placeholder
#: or a shell variable is documentation, not a credential.
EXAMPLE_CREDENTIAL = re.compile(r"://[^:\s]*:(?:password|<[A-Za-z_ ]+>|\$\{[A-Z_]+\}|\$[A-Z_]+)@")

#: Assignments whose right-hand side must be a placeholder.
ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PEPPER|DEK[_A-Z0-9]*))\s*=\s*(.+?)\s*$"
)
PLACEHOLDERS = {"", "FILL_ME", "changeme", "CHANGE_ME", "<redacted>", "..."}

HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9+/_-])([A-Za-z0-9+/_-]{40,}={0,2})(?![A-Za-z0-9+/_-])")
TRAILING_COMMENT = re.compile(r"\s+#.*$")
ENTROPY_FLOOR = 4.2
MARKER = "not-a-secret"

SKIP_SUFFIXES = {
    ".lock",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".wav",
    ".mp3",
    ".pdf",
    ".woff",
    ".woff2",
    ".onnx",
}
SKIP_NAMES = {"package-lock.json", "uv.lock", "yarn.lock", "pnpm-lock.yaml"}
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    ".next",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".localdev",
    "models",
}


def shannon(text: str) -> float:
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def staged_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        return []
    completed = subprocess.run(
        [git, "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [Path(line) for line in completed.stdout.splitlines() if line.strip()]


def scannable(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.name in SKIP_NAMES or path.suffix.lower() in SKIP_SUFFIXES:
        return False
    return path.is_file()


def findings_in(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    out: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if MARKER in line or EXAMPLE_CREDENTIAL.search(line):
            continue
        for pattern, what in KNOWN:
            if pattern.search(line):
                out.append(f"{path}:{number}: looks like {what}")
        assignment = ASSIGNMENT.match(line)
        if assignment is not None:
            value = TRAILING_COMMENT.sub("", assignment.group(2)).strip().strip("\"'")
            if (
                value not in PLACEHOLDERS
                and not value.startswith(("${", "$(", "os.environ", "process.env"))
                and len(value) >= 8
                and not WORDLIKE.match(value)
            ):
                out.append(f"{path}:{number}: {assignment.group(1)} has a real-looking value")
        for match in HIGH_ENTROPY.finditer(line):
            candidate = match.group(1)
            if candidate.count("-") > 4 or candidate.count("/") > 1:
                continue  # a path, a URL or a UUID list, not key material
            if shannon(candidate) >= ENTROPY_FLOOR and any(c.isdigit() for c in candidate):
                out.append(f"{path}:{number}: high-entropy string ({len(candidate)} chars)")
    return out


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] if argv else staged_files()
    findings: list[str] = []
    for path in paths:
        if path.is_dir():
            for child in path.rglob("*"):
                if scannable(child):
                    findings.extend(findings_in(child))
        elif scannable(path):
            findings.extend(findings_in(path))
    if findings:
        sys.stderr.write("Secret scan refused the change:\n")
        for finding in findings:
            sys.stderr.write(f"  {finding}\n")
        sys.stderr.write(
            "\nSecrets live only in the environment (§17). Move the value to .env, or mark a\n"
            f"line that is genuinely not a secret with '# {MARKER}'.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
