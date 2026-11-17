"""Cross-seam guards: persisted history, ordering, and no resurrection."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.collector import StubGateway, collect_once
from app.config import Settings
from app.main import create_app
from tests.conftest import login


def message(message_id: str, date: int, **fields: Any) -> dict[str, Any]:
    payload = {
        "id": message_id,
        "date": date,
        "title": f"Title {message_id}",
        "message": f"Body {message_id}",
        "app": "TestApp",
        "priority": 0,
    }
    payload.update(fields)
    return payload


def test_collected_stub_messages_appear_through_the_history_route(
    settings: Settings,
) -> None:
    settings = Settings(
        data_dir=settings.data_dir,
        admin_password="secret",
        auth_disabled=False,
        collector_backend="stub",
    )
    gateway = StubGateway([message("a", 100), message("b", 200, priority=2)])

    with TestClient(create_app(settings, gateway=gateway)) as client:
        login(client)
        response = client.get("/")

    assert response.status_code == 200
    assert response.text.index("Title b") < response.text.index("Title a")
    assert "Stubbed receiver" in response.text
    assert "last successful sync" in response.text


def test_outage_keeps_saved_history_and_never_claims_full_recovery(
    settings: Settings,
) -> None:
    settings = Settings(
        data_dir=settings.data_dir,
        admin_password="secret",
        auth_disabled=False,
        collector_backend="stub",
    )
    gateway = StubGateway([message("a", 100)])
    with TestClient(create_app(settings, gateway=gateway)) as client:
        login(client)
        gateway.fail_fetch = "connection refused"
        collect_once(gateway, client.app.state.storage)
        response = client.get("/")

    assert "Title a" in response.text
    assert "connection refused" in response.text
    assert "recovered" not in response.text.lower()
    assert "caught up" not in response.text.lower()
