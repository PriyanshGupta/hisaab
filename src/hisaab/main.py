"""FastAPI application factory.

uvicorn runs this with --factory (see __main__.py), so importing the module has
no side effects: no settings read, no database opened. Tests build isolated
apps by passing their own Settings.

register_routes() is the single list of HTTP routes. create_app() uses it, and
so does openapi_export, so the schema the frontend client is generated from is
exactly what the running app serves.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from hisaab.api import admin_users, auth
from hisaab.config import Settings
from hisaab.db import Database, WriterBusy, make_engine
from hisaab.spa import SpaStaticFiles

APP_TITLE = "Hisaab"
MIN_SESSION_SECRET_LENGTH = 32
FRONTEND_NOT_BUILT = "Frontend not built. Run scripts/run.sh (or npm run build in frontend/)."


def health(request: Request):
    # Liveness plus a real round-trip to SQLite: "process is up" alone would
    # report healthy with an unopenable database file.
    try:
        with request.app.state.db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "error", "database": "unreachable"})
    return {"status": "ok", "database": "ok"}


def register_routes(app: FastAPI) -> None:
    app.include_router(auth.router)
    app.include_router(admin_users.router)
    app.add_api_route("/health", health, methods=["GET"], include_in_schema=False)


def _require_session_secret(settings: Settings) -> None:
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    if not secret:
        raise RuntimeError(
            "HISAAB_SESSION_SECRET is not set. Add it to .env, e.g. the output of: "
            "python3 -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    if len(secret) < MIN_SESSION_SECRET_LENGTH:
        raise RuntimeError(f"HISAAB_SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters.")


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built SPA at / (after the API routes, which therefore take precedence)."""
    if (settings.frontend_dist / "index.html").is_file():
        app.mount("/", SpaStaticFiles(settings.frontend_dist), name="spa")
        return

    @app.get("/", include_in_schema=False)
    def frontend_not_built():
        return JSONResponse(status_code=404, content={"detail": FRONTEND_NOT_BUILT})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    _require_session_secret(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.db.engine.dispose()

    app = FastAPI(title=APP_TITLE, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = Database(make_engine(settings.database_path))
    # How long a user action waits for the writer lock before answering 503.
    app.state.writer_timeout = 10.0

    @app.exception_handler(WriterBusy)
    async def writer_busy(_request: Request, _exc: WriterBusy):
        return JSONResponse(status_code=503, content={"detail": "Busy with a long-running job; try again shortly."})

    register_routes(app)
    _mount_frontend(app, settings)
    return app
