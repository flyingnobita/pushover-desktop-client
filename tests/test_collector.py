"""Collector seam: persist-before-confirm, dedupe, and recorded failures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.collector import CollectResult, StubGateway, collect_once
from app.storage import Notification, Storage


def message(message_id: str = "a", date: int = 100, **fields: Any) -> dict[str, Any]:
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


class BrokenStorage:
    """Real storage that fails to write one message, like a full disk."""

    def __init__(self, storage: Storage, fail_id: str) -> None:
        self._storage = storage
        self._fail_id = fail_id

    def persist_delivery(self, item: Notification, now: int | None = None) -> str:
        if item.pushover_message_id == self._fail_id:
            raise RuntimeError("disk full")
        return self._storage.persist_delivery(item, now=now)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)


class RecordingFaults:
    """Real storage whose named writes fail, like a full disk."""

    def __init__(self, storage: Storage, *failing: str) -> None:
        self._storage = storage
        self._failing = failing

    def __getattr__(self, name: str) -> Any:
        if name in self._failing:

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("disk full")

            return fail
        return getattr(self._storage, name)


def test_collected_message_becomes_visible_history_and_is_confirmed(
    storage: Storage,
) -> None:
    gateway = StubGateway([message("a", 100, title="Deploy", priority=2)])

    result = collect_once(gateway, storage)

    assert result == CollectResult(ok=True, received=1, duplicates=0, confirmed=1, error=None)
    item = storage.get("a")
    assert (item.title, item.body, item.message_timestamp) == ("Deploy", "Body a", 100)
    assert (item.priority, item.source_app, item.is_read, item.is_archived) == (
        2,
        "TestApp",
        False,
        False,
    )
    assert gateway.confirmed == ["a"]
    assert gateway.pending == []
    assert storage.sync_state().status == "ok"
    assert storage.receipt("a").confirmed_at is not None


def test_failed_write_confirms_nothing(storage: Storage) -> None:
    gateway = StubGateway([message("bad"), message("good")])

    result = collect_once(gateway, BrokenStorage(storage, fail_id="bad"))

    assert result.ok is False
    assert result.confirmed == 0
    assert gateway.confirmed == []
    assert gateway.pending
    assert storage.get("good") is None
    assert "disk full" in (storage.sync_state().last_error or "")


def test_second_delivery_preserves_read_and_archive_state(storage: Storage) -> None:
    first = StubGateway([message("a", 100)])
    assert collect_once(first, storage).received == 1
    storage.set_read(["a"], True)
    storage.archive(["a"])

    redelivery = StubGateway([message("a", 100, title="Changed title")])

    result = collect_once(redelivery, storage)

    assert (result.ok, result.received, result.duplicates) == (True, 0, 1)
    assert redelivery.confirmed == ["a"]
    item = storage.get("a")
    assert (item.title, item.is_read, item.is_archived) == ("Title a", True, True)


def test_fetch_failure_records_error_and_keeps_history(storage: Storage) -> None:
    gateway = StubGateway([message("a", 100)])
    collect_once(gateway, storage)
    gateway.fail_fetch = "connection refused"

    result = collect_once(gateway, storage)

    assert result.ok is False
    assert "connection refused" in (result.error or "")
    state = storage.sync_state()
    assert state.status == "error"
    assert "connection refused" in (state.last_error or "")
    assert storage.get("a") is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "has spaces", "date": 100},
        {"id": "a", "date": "not-a-number"},
        {"id": "a", "date": 100, "priority": 9},
        {"date": 100},
    ],
)
def test_malformed_message_is_skipped_and_never_persisted(
    storage: Storage, payload: Mapping[str, Any]
) -> None:
    gateway = StubGateway([payload])

    result = collect_once(gateway, storage)

    assert result.ok is False
    # A well-formed id is confirmed so it leaves the queue; an unusable id is not.
    expected = ["a"] if payload.get("id") == "a" else []
    assert gateway.confirmed == expected
    assert storage.counts()["all"] == 0
    assert storage.sync_state().status == "error"


def test_malformed_message_is_skipped_and_later_messages_are_persisted(
    storage: Storage,
) -> None:
    gateway = StubGateway([message("bad", 100, priority=9), message("good", 200)])

    try:
        collect_once(gateway, storage)
    except Exception as error:  # noqa: BLE001 - pre-fix the malformed message aborts the pass
        pytest.fail(f"collect_once raised instead of skipping: {error!r}")

    assert storage.get("good") is not None
    assert storage.get("bad") is None
    assert gateway.confirmed == ["bad", "good"]
    assert gateway.pending == []
    assert storage.sync_state().status == "error"


def test_confirm_failure_leaves_the_message_persisted_for_redelivery(
    storage: Storage,
) -> None:
    class ConfirmFails(StubGateway):
        def confirm(self, message_ids: Sequence[str]) -> None:
            raise RuntimeError("connection reset")

    gateway = ConfirmFails([message("a", 100)])

    result = collect_once(gateway, storage)

    assert result.ok is False
    assert storage.get("a") is not None

    retry = StubGateway([message("a", 100)])

    assert collect_once(retry, storage).duplicates == 1
    assert retry.confirmed == ["a"]


def test_mark_confirmed_failure_is_recorded_and_never_escapes(storage: Storage) -> None:
    class MarkConfirmedFails:
        """Real storage whose receipt write fails, like a full disk."""

        def mark_confirmed(self, message_id: str, confirmed_at: int) -> None:
            raise RuntimeError("disk full")

        def __getattr__(self, name: str) -> Any:
            return getattr(storage, name)

    gateway = StubGateway([message("a", 100)])

    try:
        result = collect_once(gateway, MarkConfirmedFails())
    except Exception as error:  # noqa: BLE001 - pre-fix this kills the collector task
        pytest.fail(f"collect_once raised instead of recording the fault: {error!r}")

    assert result.ok is False
    assert "disk full" in (result.error or "")
    assert storage.sync_state().status == "error"
    assert "disk full" in (storage.sync_state().last_error or "")
    # Persist-before-confirm is unchanged: the message and its queue confirmation
    # already happened before the receipt write failed.
    assert storage.get("a") is not None
    assert gateway.confirmed == ["a"]


def test_full_disk_recording_fault_never_escapes_the_pass(storage: Storage) -> None:
    """Both the receipt write and the failure record fail: the pass still returns."""
    gateway = StubGateway([message("a", 100)])

    try:
        result = collect_once(
            gateway, RecordingFaults(storage, "mark_confirmed", "record_failure")
        )
    except Exception as error:  # noqa: BLE001 - pre-fix this kills the collector task
        pytest.fail(f"collect_once raised on a recording fault: {error!r}")

    assert result.ok is False
    assert "disk full" in (result.error or "")
    assert storage.get("a") is not None
    assert gateway.confirmed == ["a"]


def test_recording_fault_while_skipping_a_malformed_message_never_escapes(
    storage: Storage,
) -> None:
    gateway = StubGateway([message("bad", 100, priority=9)])

    try:
        result = collect_once(gateway, RecordingFaults(storage, "record_failure"))
    except Exception as error:  # noqa: BLE001 - pre-fix this kills the collector task
        pytest.fail(f"collect_once raised on a recording fault: {error!r}")

    assert result.ok is False
    assert "invalid priority: 9" in (result.error or "")
    assert gateway.confirmed == ["bad"]


def test_recording_fault_after_a_clean_pass_keeps_the_success_result(
    storage: Storage,
) -> None:
    gateway = StubGateway([message("a", 100)])

    try:
        result = collect_once(gateway, RecordingFaults(storage, "record_success"))
    except Exception as error:  # noqa: BLE001 - pre-fix this kills the collector task
        pytest.fail(f"collect_once raised on a recording fault: {error!r}")

    assert result == CollectResult(ok=True, received=1, duplicates=0, confirmed=1, error=None)
    assert storage.get("a") is not None
    assert gateway.confirmed == ["a"]


def test_fetch_failure_with_a_recording_fault_never_escapes(storage: Storage) -> None:
    gateway = StubGateway()
    gateway.fail_fetch = "connection refused"

    try:
        result = collect_once(gateway, RecordingFaults(storage, "record_failure"))
    except Exception as error:  # noqa: BLE001 - pre-fix this kills the collector task
        pytest.fail(f"collect_once raised on a recording fault: {error!r}")

    assert result.ok is False
    assert "connection refused" in (result.error or "")
