"""Application factory: one process, one SQLite database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.collector import Gateway, StubGateway, collect_once, run_collector
from app.config import Settings
from app.routes import create_router
from app.storage import Storage

PACKAGE_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = Storage(settings.database_path)
        app.state.storage = storage
        task: asyncio.Task[None] | None = None
        if settings.collector_backend == "stub":
            collector_gateway = gateway or StubGateway()
            app.state.gateway = collector_gateway
            collect_once(collector_gateway, storage)
            task = asyncio.create_task(
                run_collector(collector_gateway, storage, settings.collector_interval)
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            storage.close()

    app = FastAPI(title="Notification History", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(create_router(settings))
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    return app
