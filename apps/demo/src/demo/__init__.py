"""The demo backend: a Flask application that shows the helpline working.

Three pages, one job -- let someone with no telephony account hear the agent
and watch the stack:

* ``/`` -- is everything up? Probes the voice worker and the API and shows
  what it found, refreshing itself.
* ``/call`` -- talk to the agent from a browser. The microphone stands in for
  the phone; the page opens the same media socket a carrier opens and speaks
  the same frames, so what it proves is the real pipeline.
* ``/call?answer=<contact>`` -- answer, as the farmer, an outbound call the
  dialer placed in simulator mode.

Why Flask, when the rest of the backend is FastAPI: this is a request/response
web application -- render a page, probe two URLs, hand back JSON -- and that
is the shape Flask was built for. The audio path is a 20 ms-frame WebSocket
on an asyncio loop and stays on FastAPI, where it belongs; this service is a
client of it, never in it. Served by waitress, a production WSGI server, so
the demo runs from the same container image as every other service.

The app factory pattern (`create_app`) is what makes the tests possible: a
configuration is handed in, nothing is read from the environment at import
time, and two apps with different configurations can exist in one process.
"""

from __future__ import annotations

from flask import Flask, Response

from uaagro_domain.settings import get_settings

from .config import DemoConfig
from .views import demo

#: The demo takes no uploads and no forms of substance; a body this large is
#: a mistake or an attack, and either is refused early.
MAX_REQUEST_BYTES = 16 * 1024


def create_app(config: DemoConfig | None = None) -> Flask:
    """Build the application. Without a config, read the deployment's settings."""
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["DEMO"] = config or DemoConfig.from_settings(get_settings())
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    app.register_blueprint(demo)

    @app.after_request
    def harden(response: Response) -> Response:
        # The call page embeds a socket token; the least it can do is refuse
        # to be framed or sniffed. Same headers the API sends (§17).
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app


__all__ = ("DemoConfig", "create_app")
