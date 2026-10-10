"""FastAPI application factory.

uvicorn runs this with --factory (see __main__.py), so importing the module has
no side effects: no settings read, no database opened. Tests build isolated
apps by passing their own Settings.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from hisaab.config import Settings
from hisaab.db import make_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.engine.dispose()

    app = FastAPI(title="Hisaab", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_path)

    @app.get("/health")
    def health(request: Request):
        # Liveness plus a real round-trip to SQLite: "process is up" alone would
        # report healthy with an unopenable database file.
        try:
            with request.app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"status": "error", "database": "unreachable"})
        return {"status": "ok", "database": "ok"}

    return app
