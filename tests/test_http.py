"""HTTP seam: authentication, health, and the empty history surface."""

from __future__ import annotations

import html
import re
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.collector import StubGateway
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


def stub_message(message_id: str, date: int, **fields) -> dict:
    payload = {
        "id": message_id,
        "date": date,
        "title": f"Title {message_id}",
        "message": f"Body {message_id}",
        "app": "CI",
        "priority": 0,
    }
    payload.update(fields)
    return payload


@contextmanager
def reader(settings: Settings):
    settings = Settings(
        data_dir=settings.data_dir,
        admin_password="secret",
        auth_disabled=False,
        collector_backend="stub",
    )
    gateway = StubGateway(
        [
            stub_message("deploy", 300, title="Deploy finished"),
            stub_message("chat", 200, title="Hello there", app="Chat", priority=2),
            stub_message("old", 100, title="Old news"),
        ]
    )
    with TestClient(create_app(settings, gateway=gateway)) as client:
        login(client)
        yield client


def test_list_shows_newest_first_with_source(settings: Settings) -> None:
    with reader(settings) as client:
        response = client.get("/")

    assert response.text.index("Deploy finished") < response.text.index("Hello there")
    assert response.text.index("Hello there") < response.text.index("Old news")
    assert "CI" in response.text and "Chat" in response.text


def test_search_scope_source_and_read_filters(settings: Settings) -> None:
    with reader(settings) as client:
        search = client.get("/", params={"q": "deploy"})
        source = client.get("/", params={"source": "Chat"})
        unread = client.get("/", params={"read": "unread"})

        token = csrf_token(client.get("/").text)
        client.post("/messages/read", data={"csrf_token": token, "ids": "chat", "next": "/"})
        read = client.get("/", params={"read": "read"})

    assert "Deploy finished" in search.text and "Old news" not in search.text
    assert "Hello there" in source.text and "Deploy finished" not in source.text
    assert "Hello there" in unread.text
    assert "Hello there" in read.text and "Deploy finished" not in read.text


def test_detail_marks_read_and_keeps_list_filters(settings: Settings) -> None:
    with reader(settings) as client:
        response = client.get("/messages/chat", params={"back": "/?scope=inbox&read=unread"})
        read_list = client.get("/", params={"read": "read"})

    assert response.status_code == 200
    assert "Hello there" in response.text
    assert "Emergency" in response.text
    assert "Body chat" in response.text
    assert 'href="/?scope=inbox&amp;read=unread"' in response.text
    assert "Hello there" in read_list.text


def test_unknown_message_is_not_found(settings: Settings) -> None:
    with reader(settings) as client:
        missing = client.get("/messages/missing")
        invalid = client.get("/messages/not valid")

    assert missing.status_code == 404
    assert invalid.status_code == 404


def test_selected_mark_unread_returns_to_the_same_filtered_list(settings: Settings) -> None:
    with reader(settings) as client:
        token = csrf_token(client.get("/").text)
        response = client.post(
            "/messages/unread",
            data={"csrf_token": token, "ids": "chat", "next": "/?scope=inbox&read=read"},
            follow_redirects=False,
        )
        read_list = client.get("/", params={"scope": "inbox", "read": "read"})

    assert response.status_code == 303
    assert response.headers["location"] == "/?scope=inbox&read=read"
    assert "Hello there" not in read_list.text


def test_pagination_keeps_filters_and_reaches_older_items(settings: Settings) -> None:
    with reader(settings) as client:
        first = client.get("/", params={"scope": "inbox", "limit": 1})
        assert "Deploy finished" in first.text and "Hello there" not in first.text
        older_link = re.search(r'href="(/[^"]*cursor=[^"]*)"', first.text)
        assert older_link, first.text
        second = client.get(html.unescape(older_link.group(1)))

    assert "Hello there" not in first.text
    assert "Hello there" in second.text
    assert "Deploy finished" not in second.text


def test_timed_refresh_keeps_the_current_query(settings: Settings) -> None:
    with reader(settings) as client:
        response = client.get("/", params={"scope": "all", "q": "news", "read": "unread"})

    assert 'http-equiv="refresh"' in response.text
    assert "scope=all" in response.text
    assert "q=news" in response.text
    assert "read=unread" in response.text


def test_invalid_filters_and_cursors_are_rejected(settings: Settings) -> None:
    with reader(settings) as client:
        for params in [
            {"scope": "trash"},
            {"read": "unknown"},
            {"limit": "0"},
            {"limit": "many"},
            {"cursor": "not-a-cursor"},
        ]:
            assert client.get("/", params=params).status_code == 400, params


def test_mutations_require_a_csrf_token(settings: Settings) -> None:
    with reader(settings) as client:
        response = client.post("/messages/archive", data={"ids": "deploy", "next": "/"})

    assert response.status_code == 400


def test_second_pass_after_archive_keeps_other_items_in_order(settings: Settings) -> None:
    with reader(settings) as client:
        token = csrf_token(client.get("/").text)
        client.post(
            "/messages/archive",
            data={"csrf_token": token, "ids": "chat", "next": "/?scope=inbox"},
        )
        inbox = client.get("/", params={"scope": "inbox"})
        archived = client.get("/", params={"scope": "archived"})

    assert "Hello there" not in inbox.text
    assert inbox.text.index("Deploy finished") < inbox.text.index("Old news")
    assert "Hello there" in archived.text
    assert "Deploy finished" not in archived.text
