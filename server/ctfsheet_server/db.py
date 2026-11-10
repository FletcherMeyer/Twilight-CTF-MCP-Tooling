"""SQLite storage. One connection per request; WAL mode for concurrent readers."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
    editor_code_hash     TEXT NOT NULL,
    viewer_code_hash     TEXT NOT NULL,
    agent_md             BLOB,                 -- encrypted
    agent_md_updated_at  TEXT,
    agent_md_updated_by  TEXT,
    revision             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    team_id     INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash     TEXT NOT NULL,
    initials    TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')),
    disabled    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    expires_at    TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id        INTEGER PRIMARY KEY,
    team_id   INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    name      TEXT NOT NULL COLLATE NOCASE,
    position  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (team_id, name)
);

CREATE TABLE IF NOT EXISTS challenges (
    id           INTEGER PRIMARY KEY,
    team_id      INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name         TEXT NOT NULL COLLATE NOCASE,
    difficulty   TEXT NOT NULL,
    points       INTEGER,
    status       TEXT NOT NULL DEFAULT 'open',
    description  BLOB,                         -- encrypted
    notes        BLOB,                         -- encrypted, free-form shared notes
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (category_id, name)
);

CREATE TABLE IF NOT EXISTS flags (
    id            INTEGER PRIMARY KEY,
    challenge_id  INTEGER NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
    label         TEXT NOT NULL,
    value         BLOB NOT NULL,               -- encrypted
    submitted_by  TEXT NOT NULL,               -- initials
    user_id       INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS completions (
    id            INTEGER PRIMARY KEY,
    challenge_id  INTEGER NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL,
    initials      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (challenge_id, initials)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              INTEGER PRIMARY KEY,
    challenge_id    INTEGER NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES artifacts(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    flag_id         INTEGER REFERENCES flags(id) ON DELETE SET NULL,
    title           TEXT,
    filename        TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size            INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    content         BLOB NOT NULL,             -- encrypted
    source          TEXT NOT NULL CHECK (source IN ('agent','human')),
    agent_name      TEXT,
    author_id       INTEGER,
    author_initials TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY,
    team_id     INTEGER,
    user_id     INTEGER,
    action      TEXT NOT NULL,
    detail      TEXT,
    ip          TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_chal_team ON challenges(team_id, category_id);
CREATE INDEX IF NOT EXISTS ix_art_chal ON artifacts(challenge_id);
CREATE INDEX IF NOT EXISTS ix_flag_chal ON flags(challenge_id);
CREATE INDEX IF NOT EXISTS ix_comp_chal ON completions(challenge_id);
CREATE INDEX IF NOT EXISTS ix_sess_exp ON sessions(expires_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    # FastAPI may run a sync dependency and the endpoint on different pool threads;
    # each connection is still used by exactly one request at a time.
    conn = sqlite3.connect(path or settings.db_path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(path: str | None = None) -> None:
    path = path or settings.db_path
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency. Endpoints call db.commit() explicitly after writes;
    anything uncommitted is rolled back when the connection closes."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def bump_revision(conn: sqlite3.Connection, team_id: int) -> None:
    conn.execute("UPDATE teams SET revision = revision + 1 WHERE id = ?", (team_id,))


def audit(conn: sqlite3.Connection, team_id: int | None, user_id: int | None, action: str,
          detail: str = "", ip: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit(team_id, user_id, action, detail, ip, created_at) VALUES (?,?,?,?,?,?)",
        (team_id, user_id, action, detail[:500], ip, now()),
    )
