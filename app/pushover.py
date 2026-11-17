"""Pushover Open Client adapter: sign-in, device registration, fetch and confirm.

The password and two-factor code exist only for the duration of one sign-in
call. Only the receiving session secret, device ID and non-secret settings are
persisted. Emergency receipts are never acknowledged: priority is display-only.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

API_BASE = "https://api.pushover.net/1"
USER_AGENT = "notification-history/0.1 (personal Pushover Open Client)"
DEVICE_OS = "O"


class PushoverError(Exception):
    """A failed Pushover API call, safe to show to the operator."""


class TwoFactorRequired(Exception):
    """The account requires a two-factor code to complete sign-in."""


class Transport(Protocol):
    def post(self, path: str, data: Mapping[str, str]) -> tuple[int, dict[str, Any]]: ...

    def get(self, path: str, params: Mapping[str, str]) -> tuple[int, dict[str, Any]]: ...


@dataclass(frozen=True)
class PushoverSession:
    secret: str
    device_id: str
    email: str
    device_name: str


class SessionStore:
    """Persisted receiving session material, readable only by this account."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> PushoverSession | None:
        try:
            data = json.loads(self.path.read_text())
            return PushoverSession(
                secret=data["secret"],
                device_id=data["device_id"],
                email=data["email"],
                device_name=data["device_name"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, session: PushoverSession) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(session)))
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)


def error_text(payload: Mapping[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, Mapping):
        parts = [f"{field}: {value}" for field, value in errors.items()]
    elif isinstance(errors, Sequence) and not isinstance(errors, str):
        parts = [str(value) for value in errors]
    else:
        parts = []
    text = "; ".join(parts).strip()
    return text[:300] or "Pushover rejected the request"


def sign_in(
    transport: Transport,
    *,
    email: str,
    password: str,
    twofa: str | None = None,
    device_id: str | None = None,
    device_name: str | None = None,
) -> PushoverSession:
    """Log in and, when this client has no device yet, register one."""
    data = {"email": email, "password": password}
    if twofa:
        data["twofa"] = twofa
    status, payload = transport.post("/users/login.json", data)
    if status == 412 or payload.get("two_factor_required"):
        raise TwoFactorRequired()
    if payload.get("status") != 1 or "secret" not in payload:
        raise PushoverError(error_text(payload))
    secret = str(payload["secret"])
    if device_id is None:
        device_name = device_name or f"notification_history_{secrets.token_hex(2)}"
        status, payload = transport.post(
            "/devices.json", {"secret": secret, "name": device_name, "os": DEVICE_OS}
        )
        if payload.get("status") != 1 or "id" not in payload:
            raise PushoverError(error_text(payload))
        device_id = str(payload["id"])
    return PushoverSession(
        secret=secret,
        device_id=device_id,
        email=email,
        device_name=device_name or "existing device",
    )


def normalize_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Map a downloaded message to the collector's field names."""
    app = str(message.get("app") or "")
    return {
        "id": str(message.get("id_str") or message.get("id") or ""),
        "date": message.get("date"),
        "priority": message.get("priority") or 0,
        "title": str(message.get("title") or app),
        "message": str(message.get("message") or ""),
        "app": app,
        "url": message.get("url"),
        "url_title": message.get("url_title"),
    }


class PushoverGateway:
    """Gateway used by collect_once; queue confirmation only, never receipts."""

    def __init__(self, session: PushoverSession, transport: Transport) -> None:
        self.session = session
        self.transport = transport

    def fetch_messages(self) -> list[dict[str, Any]]:
        _status, payload = self.transport.get(
            "/messages.json",
            {"secret": self.session.secret, "device_id": self.session.device_id},
        )
        if payload.get("status") != 1:
            raise PushoverError(error_text(payload))
        messages = [normalize_message(item) for item in payload.get("messages") or []]
        # Confirm raises the device's highest deleted id, so persist oldest first:
        # a later confirmation must never skip a message this pass has not stored.
        return sorted(messages, key=lambda item: int(item["id"]))

    def confirm(self, message_ids: Sequence[str]) -> None:
        if not message_ids:
            return
        highest = max(int(message_id) for message_id in message_ids)
        _status, payload = self.transport.post(
            f"/devices/{self.session.device_id}/update_highest_message.json",
            {"secret": self.session.secret, "message": str(highest)},
        )
        if payload.get("status") != 1:
            raise PushoverError(error_text(payload))


class HttpTransport:
    """Minimal JSON-over-form transport; no credentials are logged."""

    def __init__(self, base_url: str = API_BASE, timeout: float = 15.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def post(self, path: str, data: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        return self._send(
            urllib.request.Request(
                self.base_url + path,
                data=urllib.parse.urlencode(data).encode(),
                method="POST",
            )
        )

    def get(self, path: str, params: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        return self._send(
            urllib.request.Request(
                f"{self.base_url}{path}?{urllib.parse.urlencode(params)}", method="GET"
            )
        )

    def _send(self, request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            return error.code, payload
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise RuntimeError(f"cannot reach Pushover: {reason}") from None
