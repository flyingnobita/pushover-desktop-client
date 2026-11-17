"""Collector boundary: fetch device messages, persist, then confirm the queue.

Persistence happens before queue confirmation, so a failed write can never
confirm delivery. Redelivery of a known or tombstoned message is confirmed
without touching local content or state.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.storage import MESSAGE_ID_RE, Notification, Storage


class Gateway(Protocol):
    def fetch_messages(self) -> list[dict[str, Any]]: ...

    def confirm(self, message_ids: Sequence[str]) -> None: ...


@dataclass(frozen=True)
class CollectResult:
    ok: bool
    received: int = 0
    duplicates: int = 0
    confirmed: int = 0
    error: str | None = None


def map_message(message: Mapping[str, Any]) -> Notification:
    """Validate and map a Pushover device message; raise ValueError if malformed."""
    message_id = str(message.get("id") or "")
    if not MESSAGE_ID_RE.match(message_id):
        raise ValueError(f"invalid message id: {message_id!r}")
    try:
        timestamp = int(message["date"])
        priority = int(message.get("priority") or 0)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid message fields: {error}") from None
    if not -2 <= priority <= 2:
        raise ValueError(f"invalid priority: {priority}")
    title = str(message.get("title") or "").strip() or "(no title)"
    return Notification(
        pushover_message_id=message_id,
        title=title,
        body=str(message.get("message") or ""),
        message_timestamp=timestamp,
        priority=priority,
        source_app=str(message.get("app") or ""),
        url=str(message["url"]) if message.get("url") else None,
        url_title=str(message["url_title"]) if message.get("url_title") else None,
    )


class StubGateway:
    """Deterministic in-memory gateway; confirms by removing from the queue."""

    def __init__(self, messages: Sequence[Mapping[str, Any]] | None = None) -> None:
        self.pending: list[dict[str, Any]] = [dict(message) for message in messages or ()]
        self.confirmed: list[str] = []
        self.fail_fetch: str | None = None

    def fetch_messages(self) -> list[dict[str, Any]]:
        if self.fail_fetch:
            raise RuntimeError(self.fail_fetch)
        return [dict(message) for message in self.pending]

    def confirm(self, message_ids: Sequence[str]) -> None:
        for message_id in message_ids:
            self.pending = [item for item in self.pending if item.get("id") != message_id]
            self.confirmed.append(message_id)


def collect_once(gateway: Gateway, storage: Storage) -> CollectResult:
    """Run one collection pass; never confirms a message it failed to persist."""
    try:
        messages = gateway.fetch_messages()
    except Exception as error:  # noqa: BLE001 - any fetch fault is a recorded outage
        storage.record_failure(f"fetch failed: {error}")
        return CollectResult(ok=False, error=f"fetch failed: {error}")

    received = duplicates = confirmed = 0
    for message in messages:
        try:
            notification = map_message(message)
            outcome = storage.persist_delivery(notification)
            gateway.confirm([notification.pushover_message_id])
        except Exception as error:  # noqa: BLE001 - surface the fault, confirm nothing
            storage.record_failure(f"delivery failed: {error}")
            return CollectResult(
                ok=False,
                received=received,
                duplicates=duplicates,
                confirmed=confirmed,
                error=f"delivery failed: {error}",
            )
        storage.mark_confirmed(notification.pushover_message_id, int(time.time()))
        confirmed += 1
        if outcome == "created":
            received += 1
        else:
            duplicates += 1

    storage.record_success()
    return CollectResult(ok=True, received=received, duplicates=duplicates, confirmed=confirmed)


async def run_collector(
    gateway: Gateway, storage: Storage, interval: float = 30.0, max_interval: float = 300.0
) -> None:
    """Retry loop: back off on failure, resume the normal interval on success."""
    delay = interval
    while True:
        await asyncio.sleep(delay)
        result = collect_once(gateway, storage)
        delay = interval if result.ok else min(delay * 2, max_interval)
