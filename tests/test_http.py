"""HTTP seam: authentication, health, and the empty history surface."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import csrf_token, login


def test_health_check_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_history_requires_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_does_not_log_in(client: TestClient) -> None:
    token = csrf_token(client.get("/login").text)

    response = client.post("/login", data={"csrf_token": token, "password": "wrong"})

    assert response.status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_requires_csrf_token(client: TestClient) -> None:
    response = client.post("/login", data={"password": "secret"})

    assert response.status_code == 400


def test_authenticated_empty_history_shows_receiver_status(client: TestClient) -> None:
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "No notifications yet." in response.text
    assert "Collection has not run yet." in response.text


def test_passwordless_mode_reaches_history_with_a_warning(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", admin_password=None, auth_disabled=True)
    with TestClient(create_app(settings)) as test_client:
        response = test_client.get("/", follow_redirects=False)

    assert response.status_code == 200
    assert "Access control is disabled" in response.text


def test_login_session_survives_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        login(first)
        cookie = first.cookies.get("pdc_session")

    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.set("pdc_session", cookie)
        response = restarted.get("/", follow_redirects=False)

    assert cookie
    assert response.status_code == 200
