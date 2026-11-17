"""SQLite storage for received notification history.

Ordering uses Pushover's message timestamp, never arrival or insertion order:
``message_timestamp DESC, pushover_message_id DESC``.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Scope = Literal["inbox", "archived", "all"]
ReadFilter = Literal["read", "unread"] | None
PersistResult = Literal["created", "duplicate", "tombstoned"]

MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CURSOR_RE = re.compile(r"^(\d{1,12}):([A-Za-z0-9._-]{1,64})$")


class CountChanged(Exception):
    """Delete-all aborted because the confirmed count no longer matches."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"history changed since confirmation: expected {expected}, now {actual}")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class Notification:
    pushover_message_id: str
    title: str
    body: str = ""
    message_timestamp: int = 0
    priority: int = 0
    source_app: str = ""
    url: str | None = None
    url_title: str | None = None
    is_read: bool = False
    is_archived: bool = False
    received_at: int = 0


@dataclass(frozen=True)
class Page:
    items: list[Notification]
    next_cursor: str | None


@dataclass(frozen=True)
class Receipt:
    pushover_message_id: str
    first_received_at: int
    confirmed_at: int | None


@dataclass(frozen=True)
class SyncState:
    status: str
    last_attempt_at: int | None
    last_success_at: int | None
    last_error: str | None


MIGRATIONS = [
    """
    CREATE TABLE notifications (
        pushover_message_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        message_timestamp INTEGER NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -2 AND 2),
        source_app TEXT NOT NULL DEFAULT '',
        url TEXT,
        url_title TEXT,
        is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
        is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
        received_at INTEGER NOT NULL
    );
    CREATE INDEX notifications_order
        ON notifications (message_timestamp DESC, pushover_message_id DESC);
    CREATE TABLE sync_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        status TEXT NOT NULL DEFAULT 'never',
        last_attempt_at INTEGER,
        last_success_at INTEGER,
        last_error TEXT
    );
    INSERT INTO sync_state (id) VALUES (1);
    """,
    """
    CREATE TABLE delivery_receipts (
        pushover_message_id TEXT PRIMARY KEY,
        first_received_at INTEGER NOT NULL,
        confirmed_at INTEGER
    );
    """,
]

_SELECT = """
    SELECT pushover_message_id, title, body, message_timestamp, priority,
           source_app, url, url_title, is_read, is_archived, received_at
      FROM notifications
"""


def _row(row: sqlite3.Row) -> Notification:
    return Notification(
        pushover_message_id=row[0],
        title=row[1],
        body=row[2],
        message_timestamp=row[3],
        priority=row[4],
        source_app=row[5],
        url=row[6],
        url_title=row[7],
        is_read=bool(row[8]),
        is_archived=bool(row[9]),
        received_at=row[10],
    )


def encode_cursor(item: Notification) -> str:
    return f"{item.message_timestamp}:{item.pushover_message_id}"


def decode_cursor(cursor: str) -> tuple[int, str] | None:
    match = CURSOR_RE.match(cursor or "")
    if not match:
        return None
    return int(match.group(1)), match.group(2)


class Storage:
    """Public storage API; one writer per transaction."""

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Threadpool routes and the collector share one writer; the lock keeps
        # each short transaction whole.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self._write(immediate=True) as conn:
                conn.executescript(script)
                conn.execute(f"PRAGMA user_version = {index}")

    @contextmanager
    def _write(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _read(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    # -- ingestion ---------------------------------------------------------

    def persist_delivery(self, item: Notification, now: int | None = None) -> PersistResult:
        """Persist one received message and its receipt in a single transaction.

        A redelivery never changes content, read state or archive membership; a
        tombstoned ID (receipt without a row) is acknowledged without recreating it.
        """
        now = int(time.time()) if now is None else now
        with self._write(immediate=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM notifications WHERE pushover_message_id = ?",
                (item.pushover_message_id,),
            ).fetchone()
            receipt = conn.execute(
                "SELECT 1 FROM delivery_receipts WHERE pushover_message_id = ?",
                (item.pushover_message_id,),
            ).fetchone()
            if exists:
                return "duplicate"
            conn.execute(
                "INSERT OR IGNORE INTO delivery_receipts"
                " (pushover_message_id, first_received_at) VALUES (?, ?)",
                (item.pushover_message_id, now),
            )
            if receipt:
                return "tombstoned"
            conn.execute(
                """
                INSERT INTO notifications (
                    pushover_message_id, title, body, message_timestamp, priority,
                    source_app, url, url_title, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.pushover_message_id,
                    item.title,
                    item.body,
                    item.message_timestamp,
                    item.priority,
                    item.source_app,
                    item.url,
                    item.url_title,
                    now,
                ),
            )
            return "created"

    def mark_confirmed(self, message_id: str, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        with self._write() as conn:
            conn.execute(
                "UPDATE delivery_receipts SET confirmed_at = ?"
                " WHERE pushover_message_id = ? AND confirmed_at IS NULL",
                (now, message_id),
            )

    # -- reads -------------------------------------------------------------

    def get(self, message_id: str) -> Notification | None:
        rows = self._read(_SELECT + " WHERE pushover_message_id = ?", (message_id,))
        return _row(rows[0]) if rows else None

    def list_page(
        self,
        *,
        scope: Scope = "inbox",
        search: str | None = None,
        source: str | None = None,
        read: ReadFilter = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page:
        where: list[str] = []
        params: list[object] = []
        if scope == "inbox":
            where.append("is_archived = 0")
        elif scope == "archived":
            where.append("is_archived = 1")
        if search:
            where.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
            pattern = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            params.extend([pattern, pattern])
        if source:
            where.append("source_app = ?")
            params.append(source)
        if read == "read":
            where.append("is_read = 1")
        elif read == "unread":
            where.append("is_read = 0")
        decoded = decode_cursor(cursor) if cursor else None
        if decoded:
            where.append(
                "(message_timestamp < ? OR (message_timestamp = ? AND pushover_message_id < ?))"
            )
            params.extend([decoded[0], decoded[0], decoded[1]])
        sql = _SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY message_timestamp DESC, pushover_message_id DESC LIMIT ?"
        params.append(limit + 1)
        rows = [_row(row) for row in self._read(sql, params)]
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = encode_cursor(rows[-1])
        else:
            next_cursor = None
        return Page(items=rows, next_cursor=next_cursor)

    def sources(self) -> list[str]:
        rows = self._read(
            "SELECT DISTINCT source_app FROM notifications"
            " WHERE source_app != '' ORDER BY source_app"
        )
        return [row[0] for row in rows]

    def counts(self) -> dict[str, int]:
        row = self._read(
            "SELECT COALESCE(SUM(is_archived = 0), 0), COALESCE(SUM(is_archived = 1), 0),"
            " COUNT(*) FROM notifications"
        )[0]
        return {"inbox": row[0], "archived": row[1], "all": row[2]}

    def receipt(self, message_id: str) -> Receipt | None:
        rows = self._read(
            "SELECT pushover_message_id, first_received_at, confirmed_at"
            " FROM delivery_receipts WHERE pushover_message_id = ?",
            (message_id,),
        )
        return Receipt(rows[0][0], rows[0][1], rows[0][2]) if rows else None

    # -- local state -------------------------------------------------------

    def set_read(self, message_ids: Sequence[str], read: bool = True) -> int:
        return self._update_ids(
            "UPDATE notifications SET is_read = ? WHERE pushover_message_id = ?",
            [(int(read), message_id) for message_id in message_ids],
        )

    def archive(self, message_ids: Sequence[str]) -> int:
        return self._update_ids(
            "UPDATE notifications SET is_archived = 1 WHERE pushover_message_id = ?",
            [(message_id,) for message_id in message_ids],
        )

    def restore(self, message_ids: Sequence[str]) -> int:
        return self._update_ids(
            "UPDATE notifications SET is_archived = 0 WHERE pushover_message_id = ?",
            [(message_id,) for message_id in message_ids],
        )

    def delete(self, message_ids: Sequence[str]) -> int:
        return self._update_ids(
            "DELETE FROM notifications WHERE pushover_message_id = ?",
            [(message_id,) for message_id in message_ids],
        )

    def _update_ids(self, sql: str, params: Sequence[tuple[object, ...]]) -> int:
        if not params:
            return 0
        with self._write() as conn:
            changed = 0
            for values in params:
                changed += conn.execute(sql, values).rowcount
            return changed

    def archive_all(self) -> int:
        with self._write(immediate=True) as conn:
            return conn.execute(
                "UPDATE notifications SET is_archived = 1 WHERE is_archived = 0"
            ).rowcount

    def delete_all(self, expected_count: int) -> int:
        """Delete Inbox and Archived history, aborting if the count drifted."""
        with self._write(immediate=True) as conn:
            actual = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            if actual != expected_count:
                raise CountChanged(expected_count, actual)
            return conn.execute("DELETE FROM notifications").rowcount

    # -- sync state --------------------------------------------------------

    def record_failure(self, error: str, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        with self._write() as conn:
            conn.execute(
                "UPDATE sync_state SET status = 'error', last_attempt_at = ?,"
                " last_error = ? WHERE id = 1",
                (now, error[:500]),
            )

    def record_success(self, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        with self._write() as conn:
            conn.execute(
                "UPDATE sync_state SET status = 'ok', last_attempt_at = ?,"
                " last_success_at = ?, last_error = NULL WHERE id = 1",
                (now, now),
            )

    def sync_state(self) -> SyncState:
        row = self._read(
            "SELECT status, last_attempt_at, last_success_at, last_error"
            " FROM sync_state WHERE id = 1"
        )[0]
        return SyncState(row[0], row[1], row[2], row[3])
