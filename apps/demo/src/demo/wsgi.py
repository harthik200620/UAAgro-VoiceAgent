"""The WSGI entry point: ``waitress-serve demo.wsgi:app``, or any other WSGI server."""

from __future__ import annotations

from . import create_app

app = create_app()
