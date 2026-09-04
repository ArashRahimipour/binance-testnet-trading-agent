"""The trading journal: an append-only record of every decision the agent
makes, for auditability and post-hoc review.

Every signal, risk approval/rejection, order submission, fill, and
exception is written here. Entries are never updated or deleted - the
journal is the audit trail, not application state. `payload` must be
JSON-serializable and must never contain secrets (the caller's
responsibility; nothing in this module ever touches API keys).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Self

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_entry_type ON journal_entries(entry_type);
"""

SIGNAL = "SIGNAL"
RISK_DECISION = "RISK_DECISION"
ORDER_VALIDATION = "ORDER_VALIDATION"
ORDER_SUBMITTED = "ORDER_SUBMITTED"
ORDER_FILLED = "ORDER_FILLED"
RECONCILIATION = "RECONCILIATION"
EXCEPTION = "EXCEPTION"


class Journal:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def record(self, entry_type: str, payload: dict[str, Any], timestamp_ms: int) -> None:
        self._conn.execute(
            "INSERT INTO journal_entries (timestamp_ms, entry_type, payload_json) VALUES (?, ?, ?)",
            (timestamp_ms, entry_type, json.dumps(payload, default=str)),
        )
        self._conn.commit()

    def all_entries(self) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT timestamp_ms, entry_type, payload_json FROM journal_entries ORDER BY id ASC"
        )
        return [
            {"timestamp_ms": row[0], "entry_type": row[1], "payload": json.loads(row[2])}
            for row in cursor.fetchall()
        ]

    def entries_by_type(self, entry_type: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT timestamp_ms, entry_type, payload_json FROM journal_entries "
            "WHERE entry_type = ? ORDER BY id ASC",
            (entry_type,),
        )
        return [
            {"timestamp_ms": row[0], "entry_type": row[1], "payload": json.loads(row[2])}
            for row in cursor.fetchall()
        ]
