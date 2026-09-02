"""Print the current TOTP code for the local admin account.

A development convenience for the no-Docker stack. `scripts/dev_local.py`
seeds the accounts; the first browser sign-in enrols MFA and writes the shared
secret to `.localdev/`, and this turns that secret into the six-digit code the
login form is asking for -- so you can get into the panel without setting up an
authenticator app first.

It reads a file that only exists on this machine, under a directory that is
gitignored. There is no production equivalent and there should not be: §17
makes the second factor mandatory precisely so that knowing the password is
not enough, and a server-side "just give me the code" endpoint would undo that.
"""

from __future__ import annotations

import pathlib
import sys

import pyotp

SECRET_FILE = pathlib.Path(__file__).resolve().parents[1] / ".localdev" / "admin_totp_secret.txt"


def main() -> int:
    if not SECRET_FILE.exists():
        sys.stderr.write(
            f"No secret at {SECRET_FILE}.\n"
            "Sign in once at http://localhost:3000/en/login -- the panel shows "
            "the setup key on first sign-in.\n"
        )
        return 1

    totp = pyotp.TOTP(SECRET_FILE.read_text(encoding="utf-8").strip())
    remaining = totp.interval - (int(__import__("time").time()) % totp.interval)
    print(f"{totp.now()}   (valid for {remaining}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
