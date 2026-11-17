"""Storage API: schema, defaults, committed visibility, and keyset order."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage import CountChanged, Storage
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


def test_filters_select_scope_source_read_and_search(storage: Storage) -> None:
    storage.persist_delivery(notification("deploy", 300, title="Deploy finished", source_app="CI"))
    storage.persist_delivery(
        notification("chat", 200, title="Hello there", body="lunch plans", source_app="Chat")
    )
    storage.persist_delivery(notification("old", 100, source_app="CI"))
    storage.set_read(["chat"], True)
    storage.archive(["old"])

    def ids(**filters) -> list[str]:
        return [item.pushover_message_id for item in storage.list_page(**filters).items]

    assert ids(scope="inbox") == ["deploy", "chat"]
    assert ids(scope="archived") == ["old"]
    assert ids(scope="all") == ["deploy", "chat", "old"]
    assert ids(scope="all", source="CI") == ["deploy", "old"]
    assert ids(scope="all", read="unread") == ["deploy", "old"]
    assert ids(scope="all", read="read") == ["chat"]
    assert ids(scope="all", search="deploy") == ["deploy"]
    assert ids(scope="all", search="LUNCH") == ["chat"]


def test_second_pass_with_filters_reflects_new_state_without_reordering(
    storage: Storage,
) -> None:
    storage.persist_delivery(notification("a", 100, source_app="CI"))
    storage.persist_delivery(notification("b", 200, source_app="CI"))

    first_pass = storage.list_page(scope="inbox", source="CI", read="unread")

    assert [item.pushover_message_id for item in first_pass.items] == ["b", "a"]

    storage.persist_delivery(notification("c", 300, source_app="Chat"))
    storage.set_read(["a"], True)
    storage.archive(["b"])

    second_pass = storage.list_page(scope="inbox", source="CI", read="unread")
    everything = storage.list_page(scope="all")

    assert second_pass.items == []
    assert [item.pushover_message_id for item in everything.items] == ["c", "b", "a"]
    assert storage.get("b").is_read is False
    assert storage.get("b").is_archived is True


def test_keyset_pages_keep_ties_in_message_id_order(storage: Storage) -> None:
    for message_id in ["a", "b", "c"]:
        storage.persist_delivery(notification(message_id, 200))

    first = storage.list_page(limit=2)

    assert [item.pushover_message_id for item in first.items] == ["c", "b"]
    assert first.next_cursor == "200:b"

    storage.persist_delivery(notification("d", 300))

    second = storage.list_page(limit=2, cursor=first.next_cursor)

    assert [item.pushover_message_id for item in second.items] == ["a"]
    assert second.next_cursor is None


def test_read_state_is_durable_and_reversible(storage: Storage) -> None:
    storage.persist_delivery(notification("a", 100))
    storage.persist_delivery(notification("b", 200))

    assert storage.set_read(["a", "b"], True) == 2
    assert [item.pushover_message_id for item in storage.list_page(read="read").items] == ["b", "a"]
    assert storage.set_read(["a"], False) == 1
    assert [item.pushover_message_id for item in storage.list_page(read="unread").items] == ["a"]
    assert storage.get("b").is_read is True


def test_selected_archive_and_restore_preserve_read_status(storage: Storage) -> None:
    storage.persist_delivery(notification("a", 100))
    storage.persist_delivery(notification("b", 200))
    storage.set_read(["a", "b"], True)

    assert storage.archive(["a"]) == 1
    assert [item.pushover_message_id for item in storage.list_page(scope="archived").items] == ["a"]
    assert storage.get("a").is_read is True

    assert storage.restore(["a"]) == 1
    assert [item.pushover_message_id for item in storage.list_page(scope="inbox").items] == [
        "b",
        "a",
    ]
    assert storage.get("a").is_read is True
    assert storage.get("b").is_read is True


def test_archive_all_moves_every_inbox_item_and_keeps_later_arrivals_in_inbox(
    storage: Storage,
) -> None:
    for message_id in ["a", "b", "c"]:
        storage.persist_delivery(notification(message_id, 100))
    storage.archive(["c"])

    assert storage.archive_all() == 2
    assert storage.counts() == {"inbox": 0, "archived": 3, "all": 3}

    storage.persist_delivery(notification("d", 300))

    assert [item.pushover_message_id for item in storage.list_page(scope="inbox").items] == ["d"]
    assert [item.pushover_message_id for item in storage.list_page(scope="archived").items] == [
        "c",
        "b",
        "a",
    ]


def test_selected_delete_removes_content_but_keeps_the_receipt(storage: Storage) -> None:
    storage.persist_delivery(notification("a", 100))
    storage.persist_delivery(notification("b", 200))

    assert storage.delete(["a"]) == 1

    assert storage.get("a") is None
    assert storage.get("b") is not None
    receipt = storage.receipt("a")
    assert receipt is not None
    assert storage.persist_delivery(notification("a", 100, title="Resurrected")) == "tombstoned"
    assert storage.get("a") is None
    assert storage.counts() == {"inbox": 1, "archived": 0, "all": 1}


def test_delete_all_aborts_on_count_drift_and_keeps_all_history(storage: Storage) -> None:
    storage.persist_delivery(notification("a", 100))
    storage.persist_delivery(notification("b", 200))
    storage.persist_delivery(notification("c", 300))

    with pytest.raises(CountChanged) as error:
        storage.delete_all(expected_count=2)

    assert (error.value.expected, error.value.actual) == (2, 3)
    assert storage.counts() == {"inbox": 3, "archived": 0, "all": 3}

    assert storage.delete_all(expected_count=3) == 3

    assert storage.counts() == {"inbox": 0, "archived": 0, "all": 0}
    assert storage.receipt("a") is not None
    assert storage.receipt("b") is not None
