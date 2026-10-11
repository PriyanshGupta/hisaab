"""Serving the built React app (tech-design §2.4).

The SPA does its own routing, so a hard refresh on /reports/2026-08 reaches the
server as a request for a file that doesn't exist. SpaStaticFiles answers such
client routes with index.html, and React Router takes over from there.

Two kinds of path must never get index.html:
- /api/...: an unknown API path has to stay a JSON 404. Otherwise the SPA
  would try to parse HTML as JSON and fail in a confusing way.
- paths whose last segment has a file extension (/favicon.ico, /assets/x.js):
  a missing asset is a real 404, not a page.

Path traversal is handled by StaticFiles itself, which refuses anything
resolving outside the directory.
"""

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

INDEX = "index.html"


def _is_client_route(path: str) -> bool:
    if path == "api" or path.startswith("api/"):
        return False
    last_segment = path.rsplit("/", 1)[-1]
    return "." not in last_segment


class SpaStaticFiles(StaticFiles):
    def __init__(self, directory):
        super().__init__(directory=directory, html=True)

    async def get_response(self, path: str, scope: Scope) -> Response:
        serving_index = path in (".", "", INDEX)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not _is_client_route(path):
                raise
            response = await super().get_response(INDEX, scope)
            serving_index = True
        if serving_index:
            # index.html names the hashed bundles of the current build; it must be
            # revalidated so a rebuilt app is picked up. Hashed assets cache freely.
            response.headers["Cache-Control"] = "no-cache"
        return response
