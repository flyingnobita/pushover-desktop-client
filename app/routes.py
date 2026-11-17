"""HTTP routes: auth, history list/detail, filters, and mutations."""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.config import Settings
from app.pushover import (
    PushoverError,
    PushoverGateway,
    PushoverSession,
    TwoFactorRequired,
    sign_in,
)
from app.storage import MESSAGE_ID_RE, CountChanged, Storage, decode_cursor

PACKAGE_DIR = Path(__file__).parent
COOKIE_NAME = "pdc_session"
SCOPES = {"inbox", "archived", "all"}
READ_FILTERS = {"read", "unread"}
REFRESH_SECONDS = 30


def external_url(value: str | None) -> str | None:
    """Only http(s) URLs may become links; anything else stays plain text."""
    if not value:
        return None
    candidate = value.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        return candidate
    return None


def local_time(timestamp: int | None) -> Markup:
    """UTC as the no-JS fallback; the static script rewrites it to local time."""
    if not timestamp:
        return Markup("never")
    moment = datetime.fromtimestamp(timestamp, tz=UTC)
    iso = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    fallback = moment.strftime("%Y-%m-%d %H:%M UTC")
    return Markup(f'<time datetime="{iso}" data-local-time>{fallback}</time>')


class SessionSigner:
    """Signed cookie payload; the key lives in the data directory."""

    def __init__(self, key_path: Path) -> None:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            self._key = bytes.fromhex(key_path.read_text().strip())
        else:
            self._key = secrets.token_bytes(32)
            key_path.write_text(self._key.hex())
            key_path.chmod(0o600)

    def _sign(self, body: str) -> str:
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()

    def dump(self, payload: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return f"{body}.{self._sign(body)}"

    def load(self, value: str | None) -> dict[str, Any] | None:
        if not value or "." not in value:
            return None
        body, _, signature = value.rpartition(".")
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body.encode()))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def create_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    signer = SessionSigner(settings.session_key_path)
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["local_time"] = local_time
    templates.env.filters["external_url"] = external_url

    def session_of(request: Request) -> dict[str, Any]:
        existing = signer.load(request.cookies.get(COOKIE_NAME))
        if existing is None or not existing.get("csrf"):
            existing = {"csrf": secrets.token_hex(16), "auth": False}
        return existing

    def require_session(request: Request) -> dict[str, Any]:
        session = session_of(request)
        if not settings.auth_disabled and not session.get("auth"):
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return session

    def check_csrf(session: dict[str, Any], token: str) -> None:
        if not token or not hmac.compare_digest(token, session.get("csrf", "")):
            raise HTTPException(status_code=400, detail="invalid CSRF token")

    def respond(
        request: Request,
        template: str,
        context: dict[str, Any],
        session: dict[str, Any],
        status_code: int = 200,
    ) -> Response:
        context.setdefault("csrf_token", session["csrf"])
        context.setdefault("auth_enabled", not settings.auth_disabled)
        context.setdefault("signed_in", bool(session.get("auth")) or settings.auth_disabled)
        result = templates.TemplateResponse(request, template, context, status_code=status_code)
        # Authenticated HTML must re-fetch after Back so read state is never stale.
        result.headers["Cache-Control"] = "no-store"
        result.set_cookie(
            COOKIE_NAME,
            signer.dump(session),
            httponly=True,
            samesite="lax",
            path="/",
        )
        return result

    def see_other(url: str, session: dict[str, Any]) -> Response:
        result = RedirectResponse(url, status_code=303)
        result.set_cookie(
            COOKIE_NAME,
            signer.dump(session),
            httponly=True,
            samesite="lax",
            path="/",
        )
        return result

    def redirect_target(next_url: str | None) -> str:
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return next_url
        return "/"

    def storage(request: Request) -> Storage:
        return request.app.state.storage

    def pushover_session(request: Request) -> PushoverSession | None:
        return request.app.state.pushover_store.load()

    def receiver_status(request: Request) -> dict[str, Any]:
        state = storage(request).sync_state()
        backend = "Stubbed" if settings.collector_backend == "stub" else "Pushover"
        connected = (
            settings.collector_backend != "pushover" or pushover_session(request) is not None
        )
        if not connected:
            kind = "not_connected"
        elif state.status == "never":
            kind = "connected_not_run" if settings.collector_backend == "pushover" else "never"
        elif state.status == "ok":
            kind = "ok"
        else:
            kind = "error"
        return {
            "backend": backend,
            "kind": kind,
            "connected": connected,
            "error": state.last_error,
            "local_success": local_time(state.last_success_at),
        }

    def list_url(request: Request, cursor: str | None = None) -> str:
        params = {key: value for key, value in request.query_params.items() if key != "cursor"}
        cursor = cursor or request.query_params.get("cursor")
        if cursor:
            params["cursor"] = cursor
        query = urlencode(params)
        return f"{request.url.path}?{query}" if query else request.url.path

    def parse_filters(raw: dict[str, str]) -> dict[str, Any]:
        scope = raw.get("scope") or "inbox"
        read = raw.get("read") or None
        search = (raw.get("q") or "").strip()[:200]
        source = (raw.get("source") or "").strip()[:100]
        if scope not in SCOPES:
            raise HTTPException(status_code=400, detail="invalid scope")
        if read is not None and read not in READ_FILTERS:
            raise HTTPException(status_code=400, detail="invalid read filter")
        return {
            "scope": scope,
            "search": search or None,
            "source": source or None,
            "read": read,
        }

    def parse_limit(raw: str | None) -> int:
        if not raw:
            return settings.page_size
        try:
            limit = int(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid limit") from None
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="invalid limit")
        return limit

    def checked_ids(raw: list[str]) -> list[str]:
        ids = [value for value in raw if value]
        if any(not MESSAGE_ID_RE.match(value) for value in ids):
            raise HTTPException(status_code=400, detail="invalid message id")
        return ids

    # -- auth --------------------------------------------------------------

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        session = session_of(request)
        if settings.auth_disabled or session.get("auth"):
            return see_other("/", session)
        return respond(
            request,
            "login.html",
            {"request": request, "error": None, "passwordless": False},
            session,
        )

    @router.post("/login")
    def login(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
    ) -> Response:
        session = session_of(request)
        check_csrf(session, csrf_token)
        if not settings.auth_disabled:
            expected = settings.admin_password or ""
            if not hmac.compare_digest(password, expected):
                return respond(
                    request,
                    "login.html",
                    {"request": request, "error": "Incorrect password.", "passwordless": False},
                    session,
                    status_code=401,
                )
        session = {"csrf": secrets.token_hex(16), "auth": True}
        return see_other("/", session)

    @router.post("/logout")
    def logout(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        session = session_of(request)
        check_csrf(session, csrf_token)
        session = {"csrf": secrets.token_hex(16), "auth": False}
        return see_other("/login", session)

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -- list and detail ---------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    def history(
        request: Request, session: Annotated[dict[str, Any], Depends(require_session)]
    ) -> Response:
        filters = parse_filters(dict(request.query_params))
        limit = parse_limit(request.query_params.get("limit"))
        store = storage(request)
        cursor = request.query_params.get("cursor")
        if cursor and decode_cursor(cursor) is None:
            raise HTTPException(status_code=400, detail="invalid cursor")
        page = store.list_page(cursor=cursor, limit=limit, **filters)
        return respond(
            request,
            "history.html",
            {
                "request": request,
                "items": page.items,
                "filters": filters,
                "sources": store.sources(),
                "counts": store.counts(),
                "cursor": request.query_params.get("cursor"),
                "next_url": list_url(request, page.next_cursor) if page.next_cursor else None,
                "current_url": list_url(request),
                "refresh_url": list_url(request),
                "refresh_seconds": REFRESH_SECONDS,
                "receiver": receiver_status(request),
            },
            session,
        )

    @router.get("/messages/{message_id}", response_class=HTMLResponse)
    def detail(
        request: Request,
        message_id: str,
        session: Annotated[dict[str, Any], Depends(require_session)],
    ) -> Response:
        if not MESSAGE_ID_RE.match(message_id):
            raise HTTPException(status_code=404, detail="not found")
        store = storage(request)
        item = store.get(message_id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        store.set_read([message_id], True)
        item = store.get(message_id)
        return respond(
            request,
            "detail.html",
            {
                "request": request,
                "item": item,
                "back_url": redirect_target(request.query_params.get("back")),
                "receiver": receiver_status(request),
            },
            session,
        )

    # -- selected mutations ------------------------------------------------

    @router.post("/messages/read")
    def mark_read(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        ids: Annotated[list[str], Form()] = [],  # noqa: B006
        next_url: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        selected = checked_ids(ids)
        storage(request).set_read(selected, True)
        return see_other(redirect_target(next_url), session)

    @router.post("/messages/unread")
    def mark_unread(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        ids: Annotated[list[str], Form()] = [],  # noqa: B006
        next_url: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        selected = checked_ids(ids)
        storage(request).set_read(selected, False)
        return see_other(redirect_target(next_url), session)

    @router.post("/messages/archive")
    def archive(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        ids: Annotated[list[str], Form()] = [],  # noqa: B006
        next_url: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        storage(request).archive(checked_ids(ids))
        return see_other(redirect_target(next_url), session)

    @router.post("/messages/restore")
    def restore(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        ids: Annotated[list[str], Form()] = [],  # noqa: B006
        next_url: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        storage(request).restore(checked_ids(ids))
        return see_other(redirect_target(next_url), session)

    @router.post("/messages/delete")
    def delete(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        ids: Annotated[list[str], Form()] = [],  # noqa: B006
        next_url: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        storage(request).delete(checked_ids(ids))
        return see_other(redirect_target(next_url), session)

    @router.post("/messages/archive-all")
    def archive_all(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        next_url: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        storage(request).archive_all()
        return see_other(redirect_target(next_url), session)

    # -- delete all history ------------------------------------------------

    @router.get("/history/delete-all", response_class=HTMLResponse)
    def delete_all_form(
        request: Request, session: Annotated[dict[str, Any], Depends(require_session)]
    ) -> Response:
        return respond(
            request,
            "delete_all.html",
            {
                "request": request,
                "expected_count": storage(request).counts()["all"],
                "error": None,
                "back_url": redirect_target(request.query_params.get("back")),
            },
            session,
        )

    @router.post("/history/delete-all")
    def delete_all(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        expected_count: Annotated[int, Form()] = 0,
        next_url: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        try:
            storage(request).delete_all(expected_count)
        except CountChanged as error:
            return respond(
                request,
                "delete_all.html",
                {
                    "request": request,
                    "expected_count": error.actual,
                    "error": f"History changed since you confirmed: {error}.",
                    "back_url": redirect_target(next_url),
                },
                session,
                status_code=409,
            )
        return see_other("/", session)

    # -- Pushover onboarding ------------------------------------------------

    def settings_context(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "request": request,
            "connection": pushover_session(request),
            "backend_enabled": settings.collector_backend == "pushover",
            "email": "",
            "error": None,
            "twofa_required": False,
            "receiver": receiver_status(request),
            **extra,
        }

    @router.get("/settings/pushover", response_class=HTMLResponse)
    def pushover_settings(
        request: Request, session: Annotated[dict[str, Any], Depends(require_session)]
    ) -> Response:
        return respond(request, "settings.html", settings_context(request), session)

    @router.post("/settings/pushover")
    async def pushover_settings_save(
        request: Request,
        csrf_token: Annotated[str, Form()] = "",
        email: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        twofa: Annotated[str, Form()] = "",
    ) -> Response:
        session = require_session(request)
        check_csrf(session, csrf_token)
        if settings.collector_backend != "pushover":
            raise HTTPException(
                status_code=400, detail="Pushover collection is not enabled for this run"
            )
        email = email.strip()
        if not email or not password:
            raise HTTPException(status_code=400, detail="email and password are required")
        store = request.app.state.pushover_store
        existing = store.load()
        try:
            signed_in = await asyncio.to_thread(
                sign_in,
                request.app.state.pushover_transport,
                email=email,
                password=password,
                twofa=twofa.strip() or None,
                device_id=existing.device_id if existing else None,
                device_name=existing.device_name if existing else None,
            )
        except TwoFactorRequired:
            return respond(
                request,
                "settings.html",
                settings_context(
                    request,
                    email=email,
                    twofa_required=True,
                    error="Two-factor authentication is required. Enter the current code.",
                ),
                session,
                status_code=401,
            )
        except (PushoverError, RuntimeError) as error:
            return respond(
                request,
                "settings.html",
                settings_context(request, email=email, error=str(error)),
                session,
                status_code=502,
            )
        store.save(signed_in)
        await request.app.state.collector.start(
            PushoverGateway(signed_in, request.app.state.pushover_transport)
        )
        return see_other("/settings/pushover", session)

    return router
