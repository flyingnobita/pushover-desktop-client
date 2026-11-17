"""Application configuration from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_password: str | None
    auth_disabled: bool
    collector_backend: str = "stub"
    collector_interval: float = 30.0
    page_size: int = 50

    @property
    def database_path(self) -> Path:
        return self.data_dir / "history.sqlite3"

    @property
    def session_key_path(self) -> Path:
        return self.data_dir / "session.key"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        auth_disabled = _flag(env.get("AUTH_DISABLED"))
        password = env.get("ADMIN_PASSWORD") or None
        if not auth_disabled and not password:
            raise RuntimeError("ADMIN_PASSWORD is required unless AUTH_DISABLED=true")
        return cls(
            data_dir=Path(env.get("DATA_DIR", "data")),
            admin_password=password,
            auth_disabled=auth_disabled,
            collector_backend=env.get("COLLECTOR_BACKEND", "pushover").strip().lower(),
            collector_interval=float(env.get("COLLECTOR_INTERVAL", "30")),
            page_size=int(env.get("PAGE_SIZE", "50")),
        )
