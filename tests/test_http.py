"""HTTP seam: authentication, health, and the empty history surface."""

from __future__ import annotations

import html
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.collector import StubGateway, collect_once
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


def test_selected_archive_restore_and_delete(settings: Settings) -> None:
    with reader(settings) as client:
        token = csrf_token(client.get("/").text)
        client.post(
            "/messages/archive",
            data={"csrf_token": token, "ids": "chat", "next": "/?scope=inbox"},
        )
        inbox_after_archive = client.get("/", params={"scope": "inbox"})
        archived_after_archive = client.get("/", params={"scope": "archived"})
        client.post(
            "/messages/restore",
            data={"csrf_token": token, "ids": "chat", "next": "/?scope=inbox"},
        )
        restored = client.get("/", params={"scope": "inbox"})
        client.post("/messages/delete", data={"csrf_token": token, "ids": "deploy", "next": "/"})
        everything = client.get("/", params={"scope": "all"})

    assert "Hello there" not in inbox_after_archive.text
    assert "Hello there" in archived_after_archive.text
    assert "Hello there" in restored.text
    assert "Deploy finished" not in everything.text
    assert "Hello there" in everything.text


def test_archive_all_applies_to_the_whole_inbox_not_only_the_visible_page(
    settings: Settings,
) -> None:
    with reader(settings) as client:
        first_page = client.get("/", params={"scope": "inbox", "limit": 1})
        token = csrf_token(first_page.text)
        client.post(
            "/messages/archive-all",
            data={"csrf_token": token, "next": "/?scope=inbox"},
        )
        inbox = client.get("/", params={"scope": "inbox"})
        archived = client.get("/", params={"scope": "archived"})

    assert "No notifications yet." in inbox.text
    for title in ["Deploy finished", "Hello there", "Old news"]:
        assert title in archived.text


def test_delete_all_shows_the_count_and_aborts_on_drift(settings: Settings) -> None:
    settings = Settings(
        data_dir=settings.data_dir,
        admin_password="secret",
        auth_disabled=False,
        collector_backend="stub",
    )
    gateway = StubGateway(
        [
            stub_message("deploy", 300, title="Deploy finished"),
            stub_message("chat", 200, title="Hello there"),
            stub_message("old", 100, title="Old news"),
        ]
    )
    with TestClient(create_app(settings, gateway=gateway)) as client:
        login(client)
        confirmation = client.get("/history/delete-all")
        token = csrf_token(confirmation.text)

        gateway.pending.append(stub_message("fresh", 400, title="Fresh arrival"))
        collect_once(gateway, client.app.state.storage)

        drift = client.post(
            "/history/delete-all",
            data={"csrf_token": token, "expected_count": "3", "next": "/"},
        )
        still_there = client.get("/", params={"scope": "all"})
        retry_token = csrf_token(drift.text)
        deleted = client.post(
            "/history/delete-all",
            data={"csrf_token": retry_token, "expected_count": "4", "next": "/"},
            follow_redirects=False,
        )
        after = client.get("/", params={"scope": "all"})

    assert "<strong>3</strong>" in confirmation.text
    assert "Fresh arrival" not in confirmation.text
    assert drift.status_code == 409
    assert "Fresh arrival" in still_there.text
    assert deleted.status_code == 303
    assert "No notifications yet." in after.text


def test_delete_all_requires_csrf(settings: Settings) -> None:
    with reader(settings) as client:
        response = client.post("/history/delete-all", data={"expected_count": "3", "next": "/"})
        still_there = client.get("/", params={"scope": "all"})

    assert response.status_code == 400
    assert "Deploy finished" in still_there.text


def test_collector_backend_defaults_to_pushover(monkeypatch) -> None:
    monkeypatch.delenv("COLLECTOR_BACKEND", raising=False)

    settings = Settings.from_env({"ADMIN_PASSWORD": "secret"})

    assert settings.collector_backend == "pushover"


def test_detail_never_links_a_script_url(settings: Settings) -> None:
    settings = Settings(
        data_dir=settings.data_dir,
        admin_password="secret",
        auth_disabled=False,
        collector_backend="stub",
    )
    gateway = StubGateway(
        [stub_message("scripty", 300, title="Suspicious", url="javascript:alert(1)")]
    )
    with TestClient(create_app(settings, gateway=gateway)) as client:
        login(client)
        response = client.get("/messages/scripty")

    assert response.status_code == 200
    assert 'href="javascript:' not in response.text
    assert "javascript:alert(1)" in response.text


def test_list_renders_local_time_elements_and_the_script(settings: Settings) -> None:
    with reader(settings) as client:
        response = client.get("/")

    match = re.search(r'<time datetime="([^"]+)" data-local-time>', response.text)
    assert match, response.text
    datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ")
    assert 'src="/static/local-time.js"' in response.text


def test_authenticated_list_is_not_cached(settings: Settings) -> None:
    with reader(settings) as client:
        response = client.get("/")

    assert "no-store" in response.headers.get("cache-control", "")
