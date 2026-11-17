"""Storage API: schema, defaults, committed visibility, and keyset order."""

from __future__ import annotations

from pathlib import Path

from app.storage import Storage
from tests.conftest import notification


def test_persisted_notification_is_unread_inbox(storage: Storage) -> None:
    assert storage.persist_delivery(notification("a", 100), now=1_000) == "created"

    item = storage.get("a")

    assert item is not None
    assert (item.is_read, item.is_archived) == (False, False)
    assert [entry.pushover_message_id for entry in storage.list_page(scope="inbox").items] == ["a"]
    assert storage.list_page(scope="archived").items == []


def test_committed_history_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    first = Storage(path)
    first.persist_delivery(notification("a", 100))
    first.close()

    reopened = Storage(path)

    assert reopened.get("a").title == "Title a"
    reopened.close()


def test_newest_first_orders_by_message_timestamp_then_message_id(storage: Storage) -> None:
    storage.persist_delivery(notification("old", 100))
    storage.persist_delivery(notification("beta", 200))
    storage.persist_delivery(notification("alpha", 200))

    ordered = [item.pushover_message_id for item in storage.list_page(scope="inbox").items]

    assert ordered == ["beta", "alpha", "old"]


def test_second_read_keeps_timestamp_order_when_older_history_arrives(storage: Storage) -> None:
    storage.persist_delivery(notification("B", 200))

    first_page = storage.list_page(limit=1)

    assert [item.pushover_message_id for item in first_page.items] == ["B"]

    storage.persist_delivery(notification("A", 100))

    fresh_page = storage.list_page(limit=1)

    assert [item.pushover_message_id for item in fresh_page.items] == ["B"]
    assert storage.get("B").is_read is False

    following_page = storage.list_page(limit=1, cursor=fresh_page.next_cursor)

    assert [item.pushover_message_id for item in following_page.items] == ["A"]
