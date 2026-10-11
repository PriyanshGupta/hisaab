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

from hisaab.accounts import repository as accounts_repo
from hisaab.api import accounts, admin_users, auth, transactions
from hisaab.auth import users
from hisaab.auth.throttle import LoginThrottle
from hisaab.transactions import repository as txns_repo
from hisaab.transactions.legs import LegError
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
    app.include_router(accounts.router)
    app.include_router(transactions.router)
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


# Repository rule violations, turned into HTTP errors in one place. Each carries
# a sentence for the person; routes just let them propagate. More specific
# classes are listed before their bases (Starlette matches by class anyway).
_ERROR_STATUS = (
    (WriterBusy, 503),
    (accounts_repo.NotFound, 404),
    (accounts_repo.DuplicateLast4, 409),
    (accounts_repo.DuplicateIdentifier, 409),
    (accounts_repo.AccountError, 422),
    (users.DuplicateUsername, 409),
    (users.LastAdmin, 409),
    (users.UserError, 422),
    (txns_repo.NotFound, 404),
    (txns_repo.EditsWouldBeLost, 409),
    (txns_repo.TransactionError, 422),
    (LegError, 422),
)
_WRITER_BUSY_DETAIL = "Busy with a long-running job; try again shortly."


def _register_error_handlers(app: FastAPI) -> None:
    def respond(status_code: int):
        async def handler(_request: Request, exc: Exception):
            detail = _WRITER_BUSY_DETAIL if isinstance(exc, WriterBusy) else str(exc)
            return JSONResponse(status_code=status_code, content={"detail": detail})
        return handler

    for exc_type, status_code in _ERROR_STATUS:
        app.add_exception_handler(exc_type, respond(status_code))


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
    app.state.login_throttle = LoginThrottle()

    _register_error_handlers(app)
    register_routes(app)
    _mount_frontend(app, settings)
    return app
