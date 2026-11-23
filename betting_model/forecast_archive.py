"""Append-only archive for pregame projections without requiring sportsbook prices.

Forecast snapshots are immutable. Outcomes are appended later as separate
records. This archive is for forecast-error measurement and calibration work;
it never places bets or assigns QUALIFIED status.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .timeutil import parse_aware_datetime

SCHEMA_VERSION = 2
MAX_PROJECTION_AGE = timedelta(hours=6)
CLOCK_SKEW = timedelta(minutes=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parsed(value: Any, label: str) -> tuple[datetime, str]:
    moment = parse_aware_datetime(value)
    if moment is None:
        raise ValueError(f"{label} must be a timezone-aware timestamp")
    return moment.astimezone(timezone.utc), str(value).strip()


def _number(value: Any, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite non-negative number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite non-negative number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return result


def connect(path: Path) -> sqlite3.Connection:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS forecast_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS forecast_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        observation_key TEXT NOT NULL UNIQUE,
        archived_at TEXT NOT NULL,
        sport TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_start TEXT NOT NULL,
        player_id TEXT NOT NULL,
        player_name TEXT,
        team TEXT,
        opponent TEXT,
        market TEXT NOT NULL,
        projected_value REAL NOT NULL,
        projected_opportunity REAL,
        projected_efficiency REAL,
        n_history INTEGER NOT NULL,
        n_opportunity INTEGER NOT NULL,
        effective_opportunity REAL,
        sigma REAL,
        distribution TEXT,
        calibrated INTEGER NOT NULL,
        model_version TEXT NOT NULL,
        projection_at TEXT NOT NULL,
        projection_input_at TEXT,
        projection_input_source TEXT,
        source TEXT NOT NULL,
        warnings_json TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS forecast_results (
        result_id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL REFERENCES forecast_snapshots(snapshot_id),
        attached_at TEXT NOT NULL,
        actual_stat REAL,
        settlement TEXT NOT NULL,
        game_status TEXT NOT NULL,
        result_source TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        supersedes_result_id TEXT,
        FOREIGN KEY (supersedes_result_id) REFERENCES forecast_results(result_id)
    );
    CREATE TRIGGER IF NOT EXISTS forecast_snapshots_no_update
    BEFORE UPDATE ON forecast_snapshots BEGIN
        SELECT RAISE(ABORT, 'forecast snapshots are immutable');
    END;
    CREATE TRIGGER IF NOT EXISTS forecast_snapshots_no_delete
    BEFORE DELETE ON forecast_snapshots BEGIN
        SELECT RAISE(ABORT, 'forecast snapshots are immutable');
    END;
    CREATE TRIGGER IF NOT EXISTS forecast_results_no_update
    BEFORE UPDATE ON forecast_results BEGIN
        SELECT RAISE(ABORT, 'forecast results are immutable');
    END;
    CREATE TRIGGER IF NOT EXISTS forecast_results_no_delete
    BEFORE DELETE ON forecast_results BEGIN
        SELECT RAISE(ABORT, 'forecast results are immutable');
    END;
    """)
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(forecast_snapshots)").fetchall()
    }
    additive_columns = {
        "n_history": "INTEGER NOT NULL DEFAULT 0",
        "n_opportunity": "INTEGER NOT NULL DEFAULT 0",
        "effective_opportunity": "REAL",
        "sigma": "REAL",
        "distribution": "TEXT",
        "calibrated": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additive_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE forecast_snapshots ADD COLUMN {name} {definition}")
    conn.execute(
        "INSERT INTO forecast_meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return SCHEMA_VERSION


def _snapshot_key(row: dict[str, Any]) -> str:
    identity = {
        "sport": row["sport"], "event_id": row["event_id"],
        "player_id": row["player_id"], "market": row["market"],
        "projected_value": round(row["projected_value"], 6),
        "n_history": row["n_history"],
        "n_opportunity": row["n_opportunity"],
        "model_version": row["model_version"],
        "projection_at": row["projection_at"],
        "projection_input_at": row.get("projection_input_at"),
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def archive_forecast(snapshot: dict[str, Any], path: Path) -> dict[str, Any]:
    """Append a forecast made before its event; the archive clock is not caller-controlled."""
    forbidden = {"actual", "actual_stat", "result", "final_stat", "settlement", "game_result"}
    leaked = sorted(key for key in forbidden if snapshot.get(key) not in (None, ""))
    if leaked:
        return {"ok": False, "reason": "postgame fields are not allowed", "fields": leaked}

    required = ("sport", "event_id", "event_start", "player_id", "market",
                "projected_value", "model_version", "projection_at", "source",
                "n_history", "n_opportunity")
    missing = [key for key in required if snapshot.get(key) in (None, "")]
    if missing:
        return {"ok": False, "reason": "missing required fields", "fields": missing}
    try:
        now = _now()
        event_start, event_text = _parsed(snapshot["event_start"], "event_start")
        projection_at, projection_text = _parsed(snapshot["projection_at"], "projection_at")
        projected = _number(snapshot["projected_value"], "projected_value")
        opportunity = None if snapshot.get("projected_opportunity") is None else _number(
            snapshot["projected_opportunity"], "projected_opportunity"
        )
        efficiency = None if snapshot.get("projected_efficiency") is None else _number(
            snapshot["projected_efficiency"], "projected_efficiency"
        )
        n_history = int(snapshot["n_history"])
        n_opportunity = int(snapshot["n_opportunity"])
        if n_history < 0 or n_opportunity < 0 or n_opportunity > n_history:
            raise ValueError("n_history and n_opportunity must be non-negative counts with n_opportunity <= n_history")
        effective_opportunity = None if snapshot.get("effective_opportunity_after_shrinkage") is None else _number(
            snapshot["effective_opportunity_after_shrinkage"], "effective_opportunity_after_shrinkage"
        )
        sigma = None if snapshot.get("sigma") is None else _number(snapshot["sigma"], "sigma")
        distribution = snapshot.get("distribution")
        calibrated = snapshot.get("calibrated") is True
        input_at, input_text = (None, None)
        if snapshot.get("projection_input_at") not in (None, ""):
            input_at, input_text = _parsed(snapshot["projection_input_at"], "projection_input_at")
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    if event_start <= now:
        return {"ok": False, "reason": "event has started or finished"}
    if projection_at >= event_start:
        return {"ok": False, "reason": "projection timestamp is not before event start"}
    if projection_at > now + CLOCK_SKEW:
        return {"ok": False, "reason": "projection timestamp is in the future"}
    if now - projection_at > MAX_PROJECTION_AGE:
        return {"ok": False, "reason": "projection is older than the six-hour archive window"}
    if input_at is not None and input_at > projection_at:
        return {"ok": False, "reason": "projection input timestamp is after projection time"}

    row = {
        "sport": str(snapshot["sport"]).strip().lower(),
        "event_id": str(snapshot["event_id"]).strip(),
        "event_start": event_text,
        "player_id": str(snapshot["player_id"]).strip(),
        "player_name": snapshot.get("player_name") or snapshot.get("player"),
        "team": snapshot.get("team"), "opponent": snapshot.get("opponent"),
        "market": str(snapshot["market"]).strip().upper(),
        "projected_value": projected,
        "projected_opportunity": opportunity,
        "projected_efficiency": efficiency,
        "n_history": n_history,
        "n_opportunity": n_opportunity,
        "effective_opportunity": effective_opportunity,
        "sigma": sigma,
        "distribution": distribution,
        "calibrated": 1 if calibrated else 0,
        "model_version": str(snapshot["model_version"]).strip(),
        "projection_at": projection_text,
        "projection_input_at": input_text,
        "projection_input_source": snapshot.get("projection_input_source"),
        "source": str(snapshot["source"]).strip(),
        "warnings_json": json.dumps(snapshot.get("warnings") or [], sort_keys=True),
        "payload_json": json.dumps(snapshot, sort_keys=True, default=str),
    }
    if not row["sport"] or not row["event_id"] or not row["player_id"] or not row["market"] or not row["model_version"] or not row["source"]:
        return {"ok": False, "reason": "required text fields must not be blank"}
    key = _snapshot_key(row)
    snapshot_id = str(uuid.uuid4())
    with connect(path) as conn:
        if conn.execute("SELECT 1 FROM forecast_snapshots WHERE observation_key=?", (key,)).fetchone():
            return {"ok": False, "reason": "duplicate forecast snapshot"}
        conn.execute(
            """INSERT INTO forecast_snapshots(
                snapshot_id,observation_key,archived_at,sport,event_id,event_start,
                player_id,player_name,team,opponent,market,projected_value,
                projected_opportunity,projected_efficiency,n_history,n_opportunity,
                effective_opportunity,sigma,distribution,calibrated,model_version,
                projection_at,projection_input_at,projection_input_source,
                source,warnings_json,payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snapshot_id, key, _utc_text(now), row["sport"], row["event_id"],
             row["event_start"], row["player_id"], row["player_name"], row["team"],
             row["opponent"], row["market"], row["projected_value"],
             row["projected_opportunity"], row["projected_efficiency"],
             row["n_history"], row["n_opportunity"], row["effective_opportunity"],
             row["sigma"], row["distribution"], row["calibrated"],
             row["model_version"], row["projection_at"], row["projection_input_at"],
             row["projection_input_source"], row["source"], row["warnings_json"],
             row["payload_json"]),
        )
        count = conn.execute("SELECT COUNT(*) FROM forecast_snapshots").fetchone()[0]
    return {"ok": True, "snapshot_id": snapshot_id, "count": count, "path": str(path)}


def attach_forecast_result(
    snapshot_id: str,
    *,
    actual_stat: float | None,
    game_status: str,
    result_source: str,
    retrieved_at: str,
    path: Path,
    supersedes_result_id: str | None = None,
) -> dict[str, Any]:
    """Append a final/void outcome. Corrections supersede by reference, never by edit."""
    try:
        retrieved, retrieved_text = _parsed(retrieved_at, "retrieved_at")
        status = str(game_status).strip().casefold()
        source = str(result_source).strip()
        if not source:
            raise ValueError("result_source must not be blank")
        if status not in {"final", "void", "postponed", "cancelled"}:
            raise ValueError("game_status must be final, void, postponed, or cancelled")
        actual = None if actual_stat is None else _number(actual_stat, "actual_stat")
        if status == "final" and actual is None:
            raise ValueError("a final result requires an actual_stat")
        if status != "final" and actual is not None:
            raise ValueError("void/postponed/cancelled results must not contain an actual_stat")
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    with connect(path) as conn:
        snapshot = conn.execute(
            "SELECT event_start FROM forecast_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            return {"ok": False, "reason": "forecast snapshot not found"}
        event_start = parse_aware_datetime(snapshot["event_start"])
        if event_start is None or retrieved <= event_start.astimezone(timezone.utc):
            return {"ok": False, "reason": "result retrieval must be after event start"}
        if retrieved > _now() + CLOCK_SKEW:
            return {"ok": False, "reason": "result retrieval timestamp is in the future"}
        active = conn.execute(
            """SELECT result_id FROM forecast_results r
               WHERE r.snapshot_id=? AND NOT EXISTS (
                 SELECT 1 FROM forecast_results newer WHERE newer.supersedes_result_id=r.result_id
               )""", (snapshot_id,)
        ).fetchall()
        if active and not supersedes_result_id:
            return {"ok": False, "reason": "a result already exists; corrections must supersede it"}
        if supersedes_result_id:
            prior = conn.execute(
                "SELECT snapshot_id FROM forecast_results WHERE result_id=?",
                (supersedes_result_id,),
            ).fetchone()
            if prior is None or prior["snapshot_id"] != snapshot_id:
                return {"ok": False, "reason": "superseded result must belong to the same forecast"}
            already = conn.execute(
                "SELECT 1 FROM forecast_results WHERE supersedes_result_id=?",
                (supersedes_result_id,),
            ).fetchone()
            if already:
                return {"ok": False, "reason": "result has already been superseded"}
        result_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO forecast_results(
                result_id,snapshot_id,attached_at,actual_stat,settlement,game_status,
                result_source,retrieved_at,supersedes_result_id
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (result_id, snapshot_id, _utc_text(_now()), actual,
             "graded" if status == "final" else "void", status, source,
             retrieved_text, supersedes_result_id),
        )
    return {"ok": True, "result_id": result_id, "snapshot_id": snapshot_id}


def forecast_metrics(path: Path) -> dict[str, Any]:
    """Summarize active final forecast errors by sport, market, and model version."""
    if not Path(path).exists():
        return {"ok": True, "groups": [], "reason": "archive_not_created"}
    with connect(path) as conn:
        rows = conn.execute("""
            SELECT s.sport,s.market,s.model_version,s.event_id,
                   s.projected_value,r.actual_stat,r.result_id
            FROM forecast_snapshots s
            JOIN forecast_results r ON r.snapshot_id=s.snapshot_id
            WHERE r.game_status='final' AND r.actual_stat IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM forecast_results newer
                WHERE newer.supersedes_result_id=r.result_id
              )
            ORDER BY s.sport,s.market,s.model_version,s.event_id
        """).fetchall()
    groups: dict[tuple[str, str, str], list[tuple[float, float, str]]] = {}
    for row in rows:
        key = (row["sport"], row["market"], row["model_version"])
        groups.setdefault(key, []).append((
            float(row["projected_value"]), float(row["actual_stat"]), str(row["event_id"])
        ))
    output = []
    for (sport, market, version), values in sorted(groups.items()):
        errors = [actual - projected for projected, actual, _ in values]
        output.append({
            "sport": sport, "market": market, "model_version": version,
            "n_forecasts": len(errors), "n_events": len({event for _, _, event in values}),
            "mae": sum(abs(error) for error in errors) / len(errors),
            "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
            "mean_signed_error": sum(errors) / len(errors),
            "residuals": errors,
        })
    return {"ok": True, "groups": output, "forecast_rows": len(rows)}



def pending_forecasts(path: Path) -> list[dict[str, Any]]:
    """Return forecast snapshots without an active result, oldest event first."""
    if not Path(path).exists():
        return []
    with connect(path) as conn:
        rows = conn.execute("""
            SELECT s.* FROM forecast_snapshots s
            WHERE NOT EXISTS (
                SELECT 1 FROM forecast_results r
                WHERE r.snapshot_id=s.snapshot_id
                  AND NOT EXISTS (
                    SELECT 1 FROM forecast_results newer
                    WHERE newer.supersedes_result_id=r.result_id
                  )
            )
            ORDER BY s.event_start,s.sport,s.player_id,s.market
        """).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
            item["warnings"] = json.loads(item.pop("warnings_json"))
        except (TypeError, json.JSONDecodeError):
            item["payload"] = {}
            item["warnings"] = ["stored snapshot metadata could not be decoded"]
        output.append(item)
    return output
