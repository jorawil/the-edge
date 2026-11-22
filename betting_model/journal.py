"""Append-only log of collection and grading attempts.

The archive remains the record of snapshots and results. This file records
what a command accepted, rejected, skipped, or could not verify. It is not a
measure of model accuracy.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any

from . import archive as archive_mod

DEFAULT_JOURNAL = Path("data") / "collection_journal.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    archive_id TEXT,
    sport TEXT,
    sportsbook TEXT,
    event_id TEXT,
    player TEXT,
    market TEXT,
    reason TEXT,
    settlement TEXT,
    result_id TEXT,
    raw_json TEXT
);
"""


def default_journal_path() -> Path:
    return Path.cwd() / DEFAULT_JOURNAL


def _utc_text() -> str:
    moment = archive_mod._utc_now()
    if moment.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(value: object) -> str | None:
    if value is None:
        return None
    text = json.dumps(value, sort_keys=True, default=str)
    if len(text) > 20000:
        return json.dumps({"truncated": True, "bytes": len(text)})
    return text


class Journal:
    """One SQLite file. Rows are inserted, not rewritten, except a run's finish line."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_journal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def start_run(self, kind: str) -> str:
        run_id = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO runs (run_id, kind, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, kind, _utc_text()),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def add(self, run_id: str, *, action: str, **fields: Any) -> None:
        raw = fields.pop("raw", None)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO events (
                    run_id, created_at, action, archive_id, sport, sportsbook, event_id,
                    player, market, reason, settlement, result_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _utc_text(),
                    action,
                    fields.get("archive_id"),
                    fields.get("sport"),
                    fields.get("sportsbook"),
                    fields.get("event_id"),
                    fields.get("player"),
                    fields.get("market"),
                    fields.get("reason"),
                    fields.get("settlement"),
                    fields.get("result_id"),
                    _dump(raw),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE run_id = ?",
                (_utc_text(), status, json.dumps(summary, sort_keys=True, default=str), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def events(self, *, run_id: str | None = None, action: str | None = None) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id",
                params,
            ).fetchall()
        finally:
            conn.close()
        found = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            if item.get("raw_json"):
                try:
                    item["raw"] = json.loads(item["raw_json"])
                except json.JSONDecodeError:
                    item["raw"] = None
            found.append(item)
        return found

    def runs(self, kind: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if kind is None:
                rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, run_id DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC, run_id DESC",
                    (kind,),
                ).fetchall()
        finally:
            conn.close()
        found = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            try:
                item["summary"] = json.loads(item["summary_json"]) if item.get("summary_json") else {}
            except json.JSONDecodeError:
                item["summary"] = {}
            found.append(item)
        return found

    def last_success(self, kind: str) -> str | None:
        """Finish time of the newest completed run of this kind with no provider failure."""
        for run in self.runs(kind):
            summary = run.get("summary") or {}
            failures = int(summary.get("provider_failures") or 0)
            if run.get("status") == "completed" and failures == 0:
                return run.get("finished_at")
        return None
