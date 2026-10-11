"""The OpenAPI schema, built without a database or secrets.

The frontend's TypeScript client is generated from this at build time
(`npm run gen:api` -> `python -m hisaab export-openapi`), so a renamed field in
a Pydantic response model breaks `tsc` at the call site instead of surfacing
as `undefined` in the browser (tech-design §2.4). It registers exactly the
routes create_app registers (main.register_routes), so the two cannot drift.
"""

from typing import Any

from fastapi import FastAPI

from hisaab.main import APP_TITLE, register_routes


def build_openapi_schema() -> dict[str, Any]:
    app: FastAPI = FastAPI(title=APP_TITLE)
    register_routes(app)
    return app.openapi()
