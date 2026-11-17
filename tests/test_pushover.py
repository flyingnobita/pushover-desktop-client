"""Pushover adapter and onboarding seam: mocked transport, no network."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.collector import collect_once
from app.config import Settings
from app.main import create_app
from app.pushover import PushoverGateway, PushoverSession
from app.storage import Notification, Storage
from tests.conftest import csrf_token, login

PASSWORD = "hunter2-pushover"


class FakeTransport:
    """In-memory Open Client API: records calls, never touches the network."""

    def __init__(self) -> None:
        self.secret = "session-secret"
        self.device_id = "device-123"
        self.device_name: str | None = None
        self.messages: list[dict[str, Any]] = []
        self.confirmed_up_to: list[int] = []
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.fail: str | None = None
        self.two_factor_pending = False
        self.login_errors: Mapping[str, Any] | None = None

    def _guard(self) -> None:
        if self.fail:
            raise RuntimeError(self.fail)

    def get(self, path: str, params: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        self._guard()
        self.calls.append(("GET", path, dict(params)))
        if path == "/messages.json":
            assert params["secret"] == self.secret
            assert params["device_id"] == self.device_id
            return 200, {"status": 1, "messages": [dict(item) for item in self.messages]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, data: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        self._guard()
        self.calls.append(("POST", path, dict(data)))
        if path == "/users/login.json":
            if self.two_factor_pending and data.get("twofa") != "123456":
                return 412, {"status": 0, "two_factor_required": True}
            self.two_factor_pending = False
            if self.login_errors is not None:
                return 200, {"status": 0, "errors": self.login_errors}
            return 200, {"status": 1, "id": "user-key", "secret": self.secret}
        if path == "/devices.json":
            assert data["os"] == "O"
            self.device_name = data["name"]
            return 200, {"status": 1, "id": self.device_id}
        if path.endswith("/update_highest_message.json"):
            highest = int(data["message"])
            self.confirmed_up_to.append(highest)
            self.messages = [item for item in self.messages if int(item["id_str"]) > highest]
            return 200, {"status": 1}
        raise AssertionError(f"unexpected POST {path}")


class BrokenStorage:
    def __init__(self, storage: Storage, fail_id: str) -> None:
        self._storage = storage
        self._fail_id = fail_id

    def persist_delivery(self, item: Notification, now: int | None = None) -> str:
        if item.pushover_message_id == self._fail_id:
            raise RuntimeError("disk full")
        return self._storage.persist_delivery(item, now=now)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)


def pushover_message(message_id: int, date: int, **fields: Any) -> dict[str, Any]:
    payload = {
        "id": message_id,
        "id_str": str(message_id),
        "date": date,
        "title": f"Title {message_id}",
        "message": f"Body {message_id}",
        "app": "Monitor",
        "priority": 0,
    }
    payload.update(fields)
    return payload


def pushover_settings(tmp_path: Path, backend: str = "pushover") -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        admin_password="secret",
        auth_disabled=False,
        collector_backend=backend,
    )


def session_file(settings: Settings) -> Path:
    return settings.data_dir / "pushover.json"


def sign_in_through_form(client: TestClient, email: str = "me@example.com") -> Any:
    token = csrf_token(client.get("/settings/pushover").text)
    return client.post(
        "/settings/pushover",
        data={"csrf_token": token, "email": email, "password": PASSWORD},
        follow_redirects=False,
    )


def test_sign_in_persists_only_receiving_session_material(
    tmp_path: Path,
) -> None:
    settings = pushover_settings(tmp_path)
    transport = FakeTransport()
    with TestClient(create_app(settings, pushover_transport=transport)) as client:
        login(client)
        assert "not connected" in client.get("/settings/pushover").text.lower()

        response = sign_in_through_form(client)

        page = client.get("/settings/pushover")
        history = client.get("/")

    assert response.status_code == 303
    assert "Connected" in page.text
    assert "me@example.com" in page.text
    assert transport.device_id in page.text
    assert "last successful sync" in page.text
    assert PASSWORD not in page.text + history.text

    stored = json.loads(session_file(settings).read_text())
    assert set(stored) == {"secret", "device_id", "email", "device_name"}
    assert stored["secret"] == transport.secret
    assert stored["device_id"] == transport.device_id
    assert stat.S_IMODE(session_file(settings).stat().st_mode) == 0o600
    for path in settings.data_dir.rglob("*"):
        if path.is_file():
            assert PASSWORD not in path.read_text(errors="ignore"), path


def test_two_factor_challenge_prompts_for_a_code_then_signs_in(tmp_path: Path) -> None:
    settings = pushover_settings(tmp_path)
    transport = FakeTransport()
    transport.two_factor_pending = True
    with TestClient(create_app(settings, pushover_transport=transport)) as client:
        login(client)
        token = csrf_token(client.get("/settings/pushover").text)

        challenge = client.post(
            "/settings/pushover",
            data={"csrf_token": token, "email": "me@example.com", "password": PASSWORD},
        )

        assert challenge.status_code == 401
        assert "Two-factor authentication is required" in challenge.text
        assert 'name="twofa"' in challenge.text
        assert PASSWORD not in challenge.text
        assert "me@example.com" in challenge.text
        assert not session_file(settings).exists()

        completed = client.post(
            "/settings/pushover",
            data={
                "csrf_token": token,
                "email": "me@example.com",
                "password": PASSWORD,
                "twofa": "123456",
            },
            follow_redirects=False,
        )
        page = client.get("/settings/pushover")

    assert completed.status_code == 303
    assert "Connected" in page.text
    assert session_file(settings).exists()


def test_adapter_maps_fields_and_confirms_only_through_the_queue(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.messages = [
        pushover_message(300, 1_700_000_300, title="Critical", priority=2, receipt="receipt-1"),
        pushover_message(200, 1_700_000_200, title="", app="CI"),
    ]
    storage = Storage(tmp_path / "storage.sqlite3")
    gateway = PushoverGateway(
        PushoverSession(
            secret=transport.secret,
            device_id=transport.device_id,
            email="me@example.com",
            device_name="device",
        ),
        transport,
    )

    result = collect_once(gateway, storage)

    assert (result.ok, result.received) == (True, 2)
    emergency = storage.get("300")
    assert (emergency.title, emergency.body, emergency.priority) == ("Critical", "Body 300", 2)
    assert (emergency.source_app, emergency.url, emergency.url_title) == (
        "Monitor",
        None,
        None,
    )
    assert storage.get("200").title == "CI"
    assert transport.confirmed_up_to == [200, 300]
    called_paths = [path for _, path, _ in transport.calls]
    assert not any("receipt" in path or "acknowledge" in path for path in called_paths)
    assert storage.receipt("300").confirmed_at is not None
    storage.close()


def test_failed_write_never_confirms_past_an_unstored_message(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.messages = [
        pushover_message(200, 1_700_000_200),
        pushover_message(300, 1_700_000_300),
    ]
    storage = Storage(tmp_path / "storage.sqlite3")
    gateway = PushoverGateway(
        PushoverSession(
            secret=transport.secret,
            device_id=transport.device_id,
            email="me@example.com",
            device_name="device",
        ),
        transport,
    )

    result = collect_once(gateway, BrokenStorage(storage, fail_id="300"))

    assert result.ok is False
    assert transport.confirmed_up_to == [200]
    assert [item["id_str"] for item in transport.messages] == ["300"]
    assert storage.get("300") is None
    storage.close()


def test_second_pass_after_outage_keeps_session_and_reports_both_states(
    tmp_path: Path,
) -> None:
    settings = pushover_settings(tmp_path)
    transport = FakeTransport()
    transport.messages = [pushover_message(300, 1_700_000_300, title="First arrival")]
    with TestClient(create_app(settings, pushover_transport=transport)) as client:
        login(client)
        sign_in_through_form(client)
        stored_before = session_file(settings).read_text()
        gateway = client.app.state.collector.gateway
        assert gateway is not None

        transport.fail = "connection refused"
        collect_once(gateway, client.app.state.storage)
        failed_page = client.get("/settings/pushover")

        transport.fail = None
        transport.messages.append(pushover_message(400, 1_700_000_400, title="Second arrival"))
        collect_once(gateway, client.app.state.storage)
        recovered_page = client.get("/settings/pushover")
        listing = client.get("/")

    assert transport.fail is None
    assert session_file(settings).read_text() == stored_before
    assert "connection refused" in failed_page.text
    assert "last successful sync" in failed_page.text.lower()
    assert "recovered" not in failed_page.text.lower()
    assert "caught up" not in failed_page.text.lower()
    assert "last successful sync" in recovered_page.text.lower()
    assert "connection refused" not in recovered_page.text
    assert "Second arrival" in listing.text
    assert listing.text.index("Second arrival") < listing.text.index("First arrival")


def test_persisted_session_is_reused_after_restart(tmp_path: Path) -> None:
    settings = pushover_settings(tmp_path)
    transport = FakeTransport()
    with TestClient(create_app(settings, pushover_transport=transport)) as client:
        login(client)
        sign_in_through_form(client)

    calls_before = len(transport.calls)
    with TestClient(create_app(settings, pushover_transport=transport)) as restarted:
        login(restarted)
        page = restarted.get("/settings/pushover")

    assert "Connected" in page.text
    assert "me@example.com" in page.text
    assert not any(path == "/users/login.json" for _, path, _ in transport.calls[calls_before:])


def test_settings_rejects_missing_csrf_and_wrong_password(tmp_path: Path) -> None:
    settings = pushover_settings(tmp_path)
    transport = FakeTransport()
    with TestClient(create_app(settings, pushover_transport=transport)) as client:
        login(client)
        no_token = client.post(
            "/settings/pushover",
            data={"email": "me@example.com", "password": PASSWORD},
        )
        token = csrf_token(client.get("/settings/pushover").text)
        transport.login_errors = {"email": ["is invalid"]}
        rejected = client.post(
            "/settings/pushover",
            data={"csrf_token": token, "email": "me@example.com", "password": PASSWORD},
        )

    assert no_token.status_code == 400
    assert rejected.status_code == 502
    assert "is invalid" in rejected.text
    assert PASSWORD not in rejected.text
    assert not session_file(settings).exists()


def test_sign_in_is_refused_when_pushover_collection_is_disabled(
    tmp_path: Path,
) -> None:
    settings = pushover_settings(tmp_path, backend="stub")
    transport = FakeTransport()
    with TestClient(create_app(settings, pushover_transport=transport)) as client:
        login(client)
        page = client.get("/settings/pushover")
        response = sign_in_through_form(client)

    assert "not enabled" in page.text
    assert response.status_code == 400
    assert not session_file(settings).exists()


def test_gateway_has_no_emergency_acknowledgement_operation() -> None:
    assert not any("acknowledge" in name.lower() for name in dir(PushoverGateway)), (
        "emergency acknowledgement must stay out of this client"
    )


def test_confirmation_sends_the_highest_seen_message_id(tmp_path: Path) -> None:
    transport = FakeTransport()
    gateway = PushoverGateway(
        PushoverSession(
            secret=transport.secret,
            device_id=transport.device_id,
            email="me@example.com",
            device_name="device",
        ),
        transport,
    )

    gateway.confirm(["200", "300"])

    assert transport.confirmed_up_to == [300]
    assert transport.calls[-1][2]["secret"] == transport.secret
