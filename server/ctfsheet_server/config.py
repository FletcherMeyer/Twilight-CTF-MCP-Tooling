"""Runtime configuration, read from environment variables.

On Linux deployments these live in /etc/ctfsheet/ctfsheet.env (mode 0600),
written by deploy/install.sh.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("CTFSHEET_DB", "./ctfsheet.db"))
    # Comma-separated Fernet keys. The FIRST key encrypts; all keys can decrypt (rotation).
    data_keys: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            k.strip() for k in os.environ.get("CTFSHEET_DATA_KEYS", "").split(",") if k.strip()
        )
    )
    allow_team_creation: bool = field(default_factory=lambda: _bool("CTFSHEET_ALLOW_TEAM_CREATION", True))
    # Optional instance-wide code required to create a team (empty = anyone can create one).
    team_creation_code: str = field(default_factory=lambda: os.environ.get("CTFSHEET_TEAM_CREATION_CODE", ""))
    max_upload_mb: int = field(default_factory=lambda: _int("CTFSHEET_MAX_UPLOAD_MB", 20))
    session_hours: int = field(default_factory=lambda: _int("CTFSHEET_SESSION_HOURS", 12))
    token_days: int = field(default_factory=lambda: _int("CTFSHEET_TOKEN_DAYS", 90))
    cookie_secure: bool = field(default_factory=lambda: _bool("CTFSHEET_COOKIE_SECURE", True))
    min_password_len: int = field(default_factory=lambda: _int("CTFSHEET_MIN_PASSWORD_LEN", 10))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings()

DIFFICULTIES = ["easy", "medium", "hard", "insane", "unrated"]
STATUSES = ["open", "in_progress", "solved"]
ARTIFACT_KINDS = ["context", "solution", "human_solution", "attachment", "image"]
