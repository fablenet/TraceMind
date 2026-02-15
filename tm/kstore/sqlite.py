from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Mapping, Tuple

from .api import KStore, register_driver, resolve_path

_SQLITE_DISABLED = os.getenv("NO_SQLITE") == "1"


class SQLiteKStore(KStore):
    """SQLite-backed key-value knowledge store."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        parent = self._path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS kstore (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)

    def put(self, key: str, value: Mapping[str, object]) -> None:
        if not isinstance(key, str):
            raise TypeError("key must be a string")
        payload = json.dumps(dict(value), separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "REPLACE INTO kstore (key, value) VALUES (?, ?)",
                (key, payload),
            )

    def get(self, key: str) -> Mapping[str, object] | None:
        if not isinstance(key, str):
            raise TypeError("key must be a string")
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kstore WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def scan(self, prefix: str) -> Iterable[Tuple[str, Mapping[str, object]]]:
        like_expr = f"{prefix or ''}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM kstore WHERE key LIKE ? ORDER BY key",
                (like_expr,),
            ).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    def delete(self, key: str) -> bool:
        if not isinstance(key, str):
            raise TypeError("key must be a string")
        with self._lock:
            cur = self._conn.execute("DELETE FROM kstore WHERE key = ?", (key,))
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _factory(_: str, parsed) -> KStore:
    path = resolve_path(parsed)
    return SQLiteKStore(path)


if not _SQLITE_DISABLED:
    register_driver("sqlite", _factory)

__all__ = ["SQLiteKStore"]
