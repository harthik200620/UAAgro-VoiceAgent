"""``uaagro-demo``: serve the demo backend with waitress.

Waitress rather than Flask's development server on purpose. The built-in
server is single-threaded, prints a warning that it is not for production,
and the demo is exactly the thing that gets left running on a shared machine
for a week. Waitress is a small, pure-Python, production WSGI server, so the
demo container is the same image as the rest of the stack.
"""

from __future__ import annotations

import argparse

from waitress import serve

from . import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="UA Agro demo backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="waitress worker threads; the probes are the only slow thing here",
    )
    args = parser.parse_args()
    serve(create_app(), host=args.host, port=args.port, threads=args.threads, ident="uaagro-demo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
