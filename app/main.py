"""Application factory: one process, one SQLite database."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.routes import create_router
from app.storage import Storage

PACKAGE_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.storage = Storage(settings.database_path)
        try:
            yield
        finally:
            app.state.storage.close()

    app = FastAPI(title="Notification History", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(create_router(settings))
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    return app
