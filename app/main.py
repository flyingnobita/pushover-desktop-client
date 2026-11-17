"""Application factory: one process, one SQLite database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.collector import Gateway, StubGateway, collect_once, run_collector
from app.config import Settings
from app.pushover import HttpTransport, PushoverGateway, SessionStore, Transport
from app.routes import create_router
from app.storage import Storage

PACKAGE_DIR = Path(__file__).parent


class CollectorControl:
    """Owns the single collector task so onboarding can (re)connect it."""

    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self.gateway: Gateway | None = None
        self.task: asyncio.Task[None] | None = None

    async def start(self, gateway: Gateway) -> None:
        await self.stop()
        self.gateway = gateway
        await asyncio.to_thread(collect_once, gateway, self.storage)
        self.task = asyncio.create_task(
            run_collector(gateway, self.storage, self.settings.collector_interval)
        )

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def create_app(
    settings: Settings | None = None,
    gateway: Gateway | None = None,
    pushover_transport: Transport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    session_store = SessionStore(settings.data_dir / "pushover.json")
    transport = pushover_transport or HttpTransport()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = Storage(settings.database_path)
        app.state.storage = storage
        control = CollectorControl(settings, storage)
        app.state.collector = control
        if settings.collector_backend == "stub":
            await control.start(gateway or StubGateway())
        elif settings.collector_backend == "pushover":
            session = session_store.load()
            if session is not None:
                await control.start(PushoverGateway(session, transport))
        try:
            yield
        finally:
            await control.stop()
            storage.close()

    app = FastAPI(title="Notification History", lifespan=lifespan)
    app.state.settings = settings
    app.state.pushover_store = session_store
    app.state.pushover_transport = transport
    app.include_router(create_router(settings))
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    return app
