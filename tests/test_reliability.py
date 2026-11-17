"""Cross-seam guards: persisted history, ordering, and no resurrection."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.collector import StubGateway, collect_once
from app.config import Settings
from app.main import create_app
from tests.conftest import csrf_token, login


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


def stub_app(settings: Settings, messages: list[dict[str, Any]]):
    settings = Settings(
        data_dir=settings.data_dir,
        admin_password="secret",
        auth_disabled=False,
        collector_backend="stub",
    )
    gateway = StubGateway(messages)
    return TestClient(create_app(settings, gateway=gateway)), gateway


def test_redelivery_after_archive_and_read_keeps_local_state(settings: Settings) -> None:
    original = message("deploy", 300, title="Deploy finished")
    client, _ = stub_app(settings, [original])
    with client:
        login(client)
        token = csrf_token(client.get("/").text)
        client.get("/messages/deploy")
        client.post(
            "/messages/archive",
            data={"csrf_token": token, "ids": "deploy", "next": "/?scope=all"},
        )

        redelivery = StubGateway([original])
        result = collect_once(redelivery, client.app.state.storage)

        everything = client.get("/", params={"scope": "all"})
        archived = client.get("/", params={"scope": "archived"})
        inbox = client.get("/", params={"scope": "inbox"})
        unread = client.get("/", params={"read": "unread"})

    assert result.duplicates == 1
    assert redelivery.confirmed == ["deploy"]
    assert "Deploy finished" in everything.text
    assert "Deploy finished" in archived.text
    assert "Deploy finished" not in inbox.text
    assert "Deploy finished" not in unread.text


def test_redelivery_after_permanent_delete_does_not_resurrect(settings: Settings) -> None:
    original = message("deploy", 300, title="Deploy finished")
    client, _ = stub_app(settings, [original])
    with client:
        login(client)
        token = csrf_token(client.get("/").text)
        client.post("/messages/delete", data={"csrf_token": token, "ids": "deploy", "next": "/"})

        redelivery = StubGateway([original])
        result = collect_once(redelivery, client.app.state.storage)

        everything = client.get("/", params={"scope": "all"})
        receipt = client.app.state.storage.receipt("deploy")

    assert result.duplicates == 1
    assert "Deploy finished" not in everything.text
    assert "No notifications yet." in everything.text
    assert receipt is not None


def test_redelivery_after_delete_all_does_not_resurrect(settings: Settings) -> None:
    first = message("deploy", 300, title="Deploy finished")
    second = message("chat", 200, title="Hello there")
    client, _ = stub_app(settings, [first, second])
    with client:
        login(client)
        confirmation = client.get("/history/delete-all")
        token = csrf_token(confirmation.text)
        client.post(
            "/history/delete-all",
            data={"csrf_token": token, "expected_count": "2", "next": "/"},
        )

        collect_once(StubGateway([first, second]), client.app.state.storage)

        everything = client.get("/", params={"scope": "all"})

    assert "No notifications yet." in everything.text


def test_emergency_notification_has_no_acknowledgement_control(settings: Settings) -> None:
    emergency = message("sos", 300, title="Server down", priority=2)
    client, gateway = stub_app(settings, [emergency])
    with client:
        login(client)
        detail = client.get("/messages/sos")

    assert "Emergency" in detail.text
    assert "acknowledg" not in detail.text.lower()
    assert gateway.confirmed == ["sos"]
