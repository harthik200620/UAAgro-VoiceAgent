"""The old address of the browser test page, kept as a redirect.

The page itself lives in the Flask demo backend (``apps/demo``): it is a web
page, and the voice worker is a media path, not a web server. Links written
against the worker's ``/dev/call`` -- bookmarks, an older panel build -- land
on the demo instead of a 404 that reads like a broken deployment.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse

from uaagro_domain.settings import get_settings


def register_browser_call(app: FastAPI) -> None:
    @app.get("/dev/call")
    async def browser_call(request: Request) -> Response:
        settings = get_settings()
        if settings.app_env != "development":
            return Response(
                "The browser test page is development-only.",
                status_code=404,
                media_type="text/plain",
            )
        target = f"{settings.demo_public_url.rstrip('/')}/call"
        answer = request.query_params.get("answer")
        if answer:
            target += "?" + urlencode({"answer": answer})
        return RedirectResponse(target, status_code=307)


__all__ = ("register_browser_call",)
