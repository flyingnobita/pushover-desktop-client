"""Shared fixtures: temporary data directories and HTTP clients at the seams."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage import Notification, Storage

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_token(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "no CSRF token in page"
    return match.group(1)


def login(client: TestClient, password: str = "secret") -> None:
    token = csrf_token(client.get("/login").text)
    response = client.post("/login", data={"csrf_token": token, "password": password})
    assert response.status_code == 200, response.text


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        admin_password="secret",
        auth_disabled=False,
        collector_backend="off",
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def storage(tmp_path: Path):
    store = Storage(tmp_path / "storage.sqlite3")
    yield store
    store.close()


def notification(message_id: str, timestamp: int = 100, **fields) -> Notification:
    return Notification(
        pushover_message_id=message_id,
        title=f"Title {message_id}",
        body=f"Body {message_id}",
        message_timestamp=timestamp,
        **fields,
    )
