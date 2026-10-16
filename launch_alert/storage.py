"""SQLite durable state and outbox, protected by a single-worker file lock."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS launches (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL,
                seen_at TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS events (
                key TEXT PRIMARY KEY, launch_id TEXT NOT NULL, revision INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL, next_try TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                delivered_at TEXT, canceled_at TEXT, error TEXT
            );
            CREATE INDEX IF NOT EXISTS events_ready ON events(next_try)
                WHERE delivered_at IS NULL AND canceled_at IS NULL;
        """)
        self.db.commit()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def close(self):
        self.db.close()


@contextmanager
def worker_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another worker already owns this state database") from exc
        yield
    finally:
        os.close(fd)
