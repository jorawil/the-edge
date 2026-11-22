"""Append-only archive of what THE EDGE knew before a game started.

This module does not invent projections, prices, or results. It does not
turn a recent average into a forecast. It does not change a stored snapshot
when a result arrives later.

A duplicate is the same observation submitted twice: same player, event,
market, side, line, price, book, projection, and the same projection and
price timestamps. A later price or a later projection is a new snapshot.

``archived_at`` is the clock time of the insert, in UTC. The caller cannot
choose it. A projection timestamp that is after that clock, after the game
starts, or more than six hours before the insert is refused. That is what
stops a backdated or reconstructed history.

The older JSON helper ``calibration.archive_projection`` is unchanged. This
SQLite file is the durable archive for evaluations going forward.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import (
    canonical_market,
    parse_american_odds,
    parse_finite_number,
    parse_side,
)
from .pairing import attach_opposites
from .distributions import INTEGER_COUNT_MARKETS, line_kind
from .sports import evaluate_prop
from .timeutil import parse_aware_datetime
from .version import MODEL_VERSION

SCHEMA_VERSION = 2
# A projection stamped much earlier than the insert is a backdate, not a record
# of what the program knew at insert time.
MAX_PROJECTION_AGE = timedelta(hours=6)
CLOCK_SKEW = timedelta(minutes=2)

DEFAULT_DB = Path("data") / "pregame_archive.sqlite"

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    archive_id TEXT PRIMARY KEY,
    observation_key TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    sport TEXT NOT NULL,
    league TEXT,
    event_id TEXT NOT NULL,
    event_start TEXT NOT NULL,
    event_start_source TEXT,
    player_id TEXT,
    player_name TEXT,
    team TEXT,
    opponent TEXT,
    market TEXT NOT NULL,
    canonical_market TEXT NOT NULL,
    side TEXT NOT NULL,
    line REAL NOT NULL,
    projected_value REAL NOT NULL,
    projection_source TEXT NOT NULL,
    projection_model_version TEXT,
    projection_created_at TEXT NOT NULL,
    projection_created_at_source TEXT NOT NULL,
    projection_input_at TEXT,
    projection_input_at_source TEXT,
    sigma REAL,
    sigma_source TEXT,
    sigma_calibrated INTEGER NOT NULL,
    distribution TEXT,
    distribution_calibrated INTEGER NOT NULL,
    p_over REAL,
    p_under REAL,
    conditional_win_probability REAL,
    sportsbook TEXT NOT NULL,
    bookmaker_market_id TEXT,
    american_odds INTEGER NOT NULL,
    raw_implied_probability REAL,
    no_vig_probability REAL,
    odds_retrieved_at TEXT NOT NULL,
    odds_retrieved_at_source TEXT NOT NULL,
    odds_provider TEXT,
    region TEXT,
    region_verified INTEGER NOT NULL,
    region_note TEXT,
    legacy_model_probability REAL,
    legacy_edge REAL,
    legacy_positive_edge INTEGER,
    no_vig_edge REAL,
    expected_value_per_unit REAL,
    screening_grade TEXT,
    qualified INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    result_id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES snapshots(archive_id),
    attached_at TEXT NOT NULL,
    actual_stat REAL,
    actual_missing INTEGER NOT NULL,
    settlement TEXT NOT NULL,
    settlement_reason TEXT NOT NULL,
    game_status TEXT NOT NULL,
    result_source TEXT NOT NULL,
    result_retrieved_at TEXT NOT NULL,
    result_retrieved_at_source TEXT NOT NULL,
    supersedes_result_id TEXT,
    FOREIGN KEY (archive_id) REFERENCES snapshots(archive_id)
);
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
BEFORE UPDATE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS results_no_update
BEFORE UPDATE ON results
BEGIN
    SELECT RAISE(ABORT, 'results are immutable; attach another result instead');
END;
CREATE TRIGGER IF NOT EXISTS results_no_delete
BEFORE DELETE ON results
BEGIN
    SELECT RAISE(ABORT, 'results are immutable');
END;
"""

_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES snapshots(archive_id),
    created_at TEXT NOT NULL,
    field_name TEXT NOT NULL,
    original_value TEXT,
    corrected_value TEXT,
    reason TEXT NOT NULL,
    source TEXT
);
CREATE TRIGGER IF NOT EXISTS corrections_no_update
BEFORE UPDATE ON corrections
BEGIN
    SELECT RAISE(ABORT, 'corrections are immutable');
END;
CREATE TRIGGER IF NOT EXISTS corrections_no_delete
BEFORE DELETE ON corrections
BEGIN
    SELECT RAISE(ABORT, 'corrections are immutable');
END;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
]

LEAKED_INPUT_FIELDS = {
    "actual",
    "actual_stat",
    "result",
    "final_stat",
    "settlement",
    "game_result",
}


def default_db_path() -> Path:
    return Path.cwd() / DEFAULT_DB


def _utc_now() -> datetime:
    """The real UTC clock. Callers cannot pass a substitute into the public API."""
    return datetime.now(timezone.utc)


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return moment.astimezone(timezone.utc)


def _utc_text(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_pair(value: object, label: str) -> tuple[datetime, str]:
    parsed = parse_aware_datetime(value)
    if parsed is None:
        raise ValueError(f"{label} must be a timezone-aware timestamp")
    return parsed, str(value).strip()


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if row is None:
        return 0
    found = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if found is None:
        return 0
    return int(found[0])


def migrate(conn: sqlite3.Connection, *, target: int | None = None) -> int:
    """Apply append-only schema steps. Existing snapshot rows are not rewritten."""
    goal = SCHEMA_VERSION if target is None else target
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version <= current or version > goal:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
        current = version
    conn.commit()
    return current


def _missing_required(prop: dict[str, Any]) -> list[str]:
    missing = []
    if not prop.get("player") and not prop.get("player_id") and not prop.get("player_name"):
        missing.append("player name or player_id")
    if not str(prop.get("sport") or "").strip():
        missing.append("sport")
    if not prop.get("event_id") and not prop.get("game_id"):
        missing.append("event_id")
    if not prop.get("event_start") and not prop.get("start_time"):
        missing.append("event_start")
    if not str(prop.get("market") or "").strip():
        missing.append("market")
    if prop.get("is_over") is None and prop.get("side") is None:
        missing.append("Over/Under")
    if prop.get("line") is None:
        missing.append("line")
    if prop.get("projected") is None:
        missing.append("projected")
    if not (prop.get("projection_source") or prop.get("source")):
        missing.append("projection_source")
    if not (prop.get("projection_at") or prop.get("generated_at")):
        missing.append("projection_at")
    if prop.get("odds") is None:
        missing.append("American odds")
    if not (prop.get("retrieved_at") or prop.get("odds_retrieved_at")):
        missing.append("odds retrieved_at")
    if not (prop.get("book") or prop.get("sportsbook")):
        missing.append("sportsbook")
    return missing


def _leaked_fields(prop: dict[str, Any]) -> list[str]:
    return sorted(key for key in LEAKED_INPUT_FIELDS if prop.get(key) not in (None, ""))


def _observation_key(row: dict[str, Any]) -> str:
    identity = {
        "sport": row["sport"],
        "event_id": row["event_id"],
        "player_id": row.get("player_id") or "",
        "player_name": (row.get("player_name") or "").casefold(),
        "market": row["canonical_market"],
        "side": row["side"],
        "line": round(float(row["line"]), 4),
        "odds": int(row["american_odds"]),
        "book": row["sportsbook"].casefold(),
        "projected": round(float(row["projected_value"]), 4),
        "projection_created_at": row["projection_created_at"],
        "odds_retrieved_at": row["odds_retrieved_at"],
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _side_probabilities(evaluation: dict[str, Any], side_over: bool) -> tuple[float | None, float | None]:
    if evaluation.get("push_handling") == "unsupported":
        return None, None
    win = evaluation.get("win_probability")
    loss = evaluation.get("loss_probability")
    if win is None or loss is None:
        return None, None
    if side_over:
        return float(win), float(loss)
    return float(loss), float(win)


def _build_row(prop: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Turn one supplied prop into a snapshot row. Raises ValueError when it must not be stored."""
    leaked = _leaked_fields(prop)
    if leaked:
        raise ValueError(
            "refusing to archive postgame fields on a pregame snapshot: " + ", ".join(leaked)
        )
    missing = _missing_required(prop)
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))
    archived_at = _utc(now)
    event_parsed, event_source = _parse_pair(
        prop.get("event_start") or prop.get("start_time"), "event_start"
    )
    projection_parsed, projection_source_text = _parse_pair(
        prop.get("projection_at") or prop.get("generated_at"), "projection_at"
    )
    odds_parsed, odds_source_text = _parse_pair(
        prop.get("retrieved_at") or prop.get("odds_retrieved_at"), "odds retrieved_at"
    )
    if event_parsed <= archived_at:
        raise ValueError("the game has already started, so this cannot be a pregame snapshot")
    if projection_parsed >= event_parsed or odds_parsed >= event_parsed:
        raise ValueError("a projection or price timestamp is not before the game")
    if projection_parsed > archived_at + CLOCK_SKEW or odds_parsed > archived_at + CLOCK_SKEW:
        raise ValueError("a timestamp is after the archive clock")
    if archived_at - _utc(projection_parsed) > MAX_PROJECTION_AGE:
        raise ValueError(
            "projection timestamp is too far before the archive time; it was not backdated"
        )
    side_over = parse_side(prop.get("is_over") if prop.get("is_over") is not None else prop.get("side"))
    line = parse_finite_number(prop.get("line"), "line")
    projected = parse_finite_number(prop.get("projected"), "projected")
    odds = parse_american_odds(prop.get("odds"))
    market_raw = str(prop.get("market"))
    canon = canonical_market(market_raw) or market_raw.strip().upper()
    region = prop.get("region")
    region_source = prop.get("region_source")
    region_verified = prop.get("region_verified") is True and bool(region) and bool(region_source)
    region_note = None
    if prop.get("region_verified") is True and not region_verified:
        region_note = "region_verified was set without a region and a source, so it was stored as unverified"
    elif str(prop.get("book") or prop.get("sportsbook") or "").casefold() in {"fanduel", "fanduel_in", "fanduel in"}:
        region_note = "FanDuel is not stored as a verified Indiana price unless region and region_source say so"
    input_at = None
    input_at_source = None
    if prop.get("projection_input_at"):
        parsed_input, input_at_source = _parse_pair(prop.get("projection_input_at"), "projection_input_at")
        if parsed_input > projection_parsed:
            raise ValueError("projection inputs are timestamped after the projection")
        input_at = _utc_text(parsed_input)
    evaluation = evaluate_prop(dict(prop), as_of=archived_at)
    p_over, p_under = _side_probabilities(evaluation, side_over)
    grade = evaluation.get("status")
    qualified = grade == "QUALIFIED"
    row = {
        "sport": str(prop.get("sport")).strip().lower(),
        "league": prop.get("league"),
        "event_id": str(prop.get("event_id") or prop.get("game_id")),
        "event_start": _utc_text(event_parsed),
        "event_start_source": event_source,
        "player_id": None if prop.get("player_id") in (None, "") else str(prop.get("player_id")),
        "player_name": prop.get("player") or prop.get("player_name"),
        "team": prop.get("team"),
        "opponent": prop.get("opponent"),
        "market": market_raw,
        "canonical_market": canon,
        "side": "Over" if side_over else "Under",
        "line": line,
        "projected_value": projected,
        "projection_source": prop.get("projection_source") or prop.get("source"),
        "projection_model_version": evaluation.get("model_version") or MODEL_VERSION,
        "projection_created_at": _utc_text(projection_parsed),
        "projection_created_at_source": projection_source_text,
        "projection_input_at": input_at,
        "projection_input_at_source": input_at_source,
        "sigma": evaluation.get("sigma"),
        "sigma_source": evaluation.get("sigma_source"),
        "sigma_calibrated": 1 if evaluation.get("sigma_calibrated") is True else 0,
        "distribution": evaluation.get("distribution"),
        "distribution_calibrated": 1 if evaluation.get("distribution_calibrated") is True else 0,
        "p_over": p_over,
        "p_under": p_under,
        "conditional_win_probability": evaluation.get("conditional_win_probability"),
        "sportsbook": str(prop.get("book") or prop.get("sportsbook")),
        "bookmaker_market_id": prop.get("bookmaker_market_id"),
        "american_odds": odds,
        "raw_implied_probability": evaluation.get("raw_implied_probability"),
        "no_vig_probability": evaluation.get("fair_market_probability"),
        "odds_retrieved_at": _utc_text(odds_parsed),
        "odds_retrieved_at_source": odds_source_text,
        "odds_provider": prop.get("odds_provider") or prop.get("price_source"),
        "region": region,
        "region_verified": 1 if region_verified else 0,
        "region_note": region_note,
        "legacy_model_probability": evaluation.get("model_prob"),
        "legacy_edge": evaluation.get("edge"),
        "legacy_positive_edge": 1 if evaluation.get("positive_edge") else 0,
        "no_vig_edge": evaluation.get("edge_vs_fair_market"),
        "expected_value_per_unit": evaluation.get("expected_value_per_unit"),
        "screening_grade": grade,
        "qualified": 1 if qualified else 0,
        "model_version": MODEL_VERSION,
        "archived_at": _utc_text(archived_at),
    }
    row["observation_key"] = _observation_key(row)
    stored_payload = dict(row)
    stored_payload["evaluation_status_reasons"] = evaluation.get("status_reasons")
    stored_payload["push_handling"] = evaluation.get("push_handling")
    row["payload_json"] = json.dumps(stored_payload, sort_keys=True)
    return row


def _normalize_pairing_aliases(prop: dict[str, Any]) -> dict[str, Any]:
    """Copy archive aliases onto the names the pairing helper reads.

    Caller-supplied ``opposite`` is dropped here. A no-vig price is attached
    later only from another row in the same batch. Original ``player_name``
    and ``odds_retrieved_at`` are kept so the stored source text is unchanged.
    """
    normalized = dict(prop)
    normalized.pop("opposite", None)
    if not normalized.get("player") and normalized.get("player_name"):
        normalized["player"] = normalized["player_name"]
    if not normalized.get("retrieved_at") and normalized.get("odds_retrieved_at"):
        normalized["retrieved_at"] = normalized["odds_retrieved_at"]
    if normalized.get("is_over") is None and normalized.get("side") is not None:
        normalized["is_over"] = normalized["side"]
    return normalized


def _pair_within_event(props: list[Any]) -> list[Any]:
    """Pair Over and Under only inside one event, using the existing price rules.

    Different books, lines, or price times are left unpaired by ``attach_opposites``.
    Splitting by event first stops two games from being treated as one market.
    Any ``opposite`` on the input is removed before that check.
    """
    prepared = [
        _normalize_pairing_aliases(prop) if isinstance(prop, dict) else prop for prop in props
    ]
    groups: dict[str, list[int]] = {}
    for index, prop in enumerate(prepared):
        if not isinstance(prop, dict):
            continue
        event = str(prop.get("event_id") or prop.get("game_id") or "")
        if not event:
            continue
        groups.setdefault(event, []).append(index)
    for indexes in groups.values():
        paired = attach_opposites([prepared[index] for index in indexes])
        for index, item in zip(indexes, paired):
            prepared[index] = item
    return prepared


def _insert_snapshot(path: Path, prop: dict[str, Any]) -> dict[str, Any]:
    """Insert one prepared prop. ``archived_at`` is the system clock in UTC."""
    moment = _utc_now()
    supplied = dict(prop)
    supplied.pop("archived_at", None)
    supplied.pop("attached_at", None)
    try:
        row = _build_row(supplied, now=moment)
    except ValueError as exc:
        return {"ok": False, "stored": False, "reason": str(exc)}
    archive_id = uuid.uuid4().hex
    conn = connect(path)
    try:
        existing = conn.execute(
            "SELECT archive_id FROM snapshots WHERE observation_key = ?",
            (row["observation_key"],),
        ).fetchone()
        if existing is not None:
            return {
                "ok": False,
                "stored": False,
                "reason": "this exact observation is already archived and was not changed",
                "archive_id": existing["archive_id"],
                "duplicate": True,
            }
        conn.execute(
            """
            INSERT INTO snapshots (
                archive_id, observation_key, schema_version, archived_at, sport, league,
                event_id, event_start, event_start_source, player_id, player_name, team,
                opponent, market, canonical_market, side, line, projected_value,
                projection_source, projection_model_version, projection_created_at,
                projection_created_at_source, projection_input_at, projection_input_at_source,
                sigma, sigma_source, sigma_calibrated, distribution, distribution_calibrated,
                p_over, p_under, conditional_win_probability, sportsbook, bookmaker_market_id,
                american_odds, raw_implied_probability, no_vig_probability, odds_retrieved_at,
                odds_retrieved_at_source, odds_provider, region, region_verified, region_note,
                legacy_model_probability, legacy_edge, legacy_positive_edge, no_vig_edge,
                expected_value_per_unit, screening_grade, qualified, model_version, payload_json
            ) VALUES (
                :archive_id, :observation_key, :schema_version, :archived_at, :sport, :league,
                :event_id, :event_start, :event_start_source, :player_id, :player_name, :team,
                :opponent, :market, :canonical_market, :side, :line, :projected_value,
                :projection_source, :projection_model_version, :projection_created_at,
                :projection_created_at_source, :projection_input_at, :projection_input_at_source,
                :sigma, :sigma_source, :sigma_calibrated, :distribution, :distribution_calibrated,
                :p_over, :p_under, :conditional_win_probability, :sportsbook, :bookmaker_market_id,
                :american_odds, :raw_implied_probability, :no_vig_probability, :odds_retrieved_at,
                :odds_retrieved_at_source, :odds_provider, :region, :region_verified, :region_note,
                :legacy_model_probability, :legacy_edge, :legacy_positive_edge, :no_vig_edge,
                :expected_value_per_unit, :screening_grade, :qualified, :model_version, :payload_json
            )
            """,
            {**row, "archive_id": archive_id, "schema_version": SCHEMA_VERSION},
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "stored": True, "archive_id": archive_id, "duplicate": False, "screening_grade": row["screening_grade"]}


def archive_evaluation(path: Path, prop: dict[str, Any]) -> dict[str, Any]:
    """Archive one prop. A supplied ``opposite`` is ignored.

    No-vig is filled only when ``archive_many`` finds the other side in the
    same batch. One prop on its own never invents the other price.
    """
    return _insert_snapshot(path, _normalize_pairing_aliases(prop))


def archive_many(path: Path, props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Archive each prop. One rejection does not remove the others.

    Compatible Over and Under prices in the same file are paired first.
    The archive clock is still the system clock for every row.
    """
    prepared = _pair_within_event(props)
    results = []
    for index, prop in enumerate(prepared):
        if not isinstance(prop, dict):
            results.append({"ok": False, "stored": False, "index": index, "reason": "prop must be an object"})
            continue
        item = _insert_snapshot(path, prop)
        item["index"] = index
        item["player"] = prop.get("player") or prop.get("player_name")
        results.append(item)
    return results


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def get_snapshot(path: Path, archive_id: str) -> dict[str, Any] | None:
    conn = connect(path)
    try:
        found = conn.execute("SELECT * FROM snapshots WHERE archive_id = ?", (archive_id,)).fetchone()
        if found is None:
            return None
        snapshot = _row_dict(found)
        snapshot["results"] = [
            _row_dict(item)
            for item in conn.execute(
                "SELECT * FROM results WHERE archive_id = ? ORDER BY attached_at, result_id",
                (archive_id,),
            )
        ]
        snapshot["corrections"] = [
            _row_dict(item)
            for item in conn.execute(
                "SELECT * FROM corrections WHERE archive_id = ? ORDER BY created_at, correction_id",
                (archive_id,),
            )
        ]
        return snapshot
    finally:
        conn.close()


def list_snapshots(
    path: Path,
    *,
    sport: str | None = None,
    player: str | None = None,
    event_id: str | None = None,
    market: str | None = None,
    sportsbook: str | None = None,
    archive_id: str | None = None,
    event_date: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    if sport:
        clauses.append("sport = ?")
        params.append(sport.strip().lower())
    if player:
        clauses.append("(lower(player_name) = lower(?) OR player_id = ?)")
        params.extend([player, player])
    if event_id:
        clauses.append("event_id = ?")
        params.append(event_id)
    if market:
        clauses.append("canonical_market = ?")
        params.append(canonical_market(market) or market)
    if sportsbook:
        clauses.append("lower(sportsbook) = lower(?)")
        params.append(sportsbook)
    if archive_id:
        clauses.append("archive_id = ?")
        params.append(archive_id)
    if event_date:
        clauses.append("substr(event_start, 1, 10) = ?")
        params.append(event_date)
    conn = connect(path)
    try:
        rows = conn.execute(
            f"SELECT * FROM snapshots WHERE {' AND '.join(clauses)} ORDER BY event_start, archived_at, archive_id",
            params,
        ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        conn.close()


def _current_result(conn: sqlite3.Connection, archive_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM results
        WHERE archive_id = ?
          AND result_id NOT IN (
              SELECT supersedes_result_id FROM results
              WHERE archive_id = ? AND supersedes_result_id IS NOT NULL
          )
        ORDER BY attached_at DESC, result_id DESC
        LIMIT 1
        """,
        (archive_id, archive_id),
    ).fetchone()


def settle_prop(
    *,
    market: str,
    line: float,
    side: str,
    actual: float | None,
    game_status: str,
    actual_provided: bool,
) -> tuple[str, str, float | None, bool]:
    """Return settlement, reason, stored actual, and whether the actual is missing.

    A did-not-play, postponement, or cancellation does not become a zero.
    """
    status = str(game_status or "").strip().lower().replace("-", "_")
    if actual_provided:
        try:
            actual = parse_finite_number(actual, "actual")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    if status in {"postponed", "canceled", "cancelled"}:
        return "Void", f"game status is {status}; no stat was stored", None, True
    if status in {"dnp", "did_not_play"}:
        return "Void", "player did not play; a missing stat was not stored as zero", None, True
    if status in {"incomplete", "suspended"}:
        return (
            "Unknown",
            f"game status is {status}; it is not final, so it was not graded",
            None,
            True,
        )
    if status not in {"final", "completed"}:
        return "Unknown", "game status is not a known final or void state", None, not actual_provided
    if not actual_provided or actual is None:
        return "Unknown", "actual stat is missing and was not treated as zero", None, True
    value = float(actual)
    side_over = parse_side(side)
    canon = canonical_market(market) or market
    kind = line_kind(float(line))
    same = abs(value - float(line)) <= 1e-9
    if canon in INTEGER_COUNT_MARKETS and kind == "whole" and same:
        return "Push", "whole-number line and the stat landed on the line", value, False
    if same and (canon not in INTEGER_COUNT_MARKETS or kind != "half"):
        return "Unknown", "no settlement rule for landing exactly on this line", value, False
    if kind == "other":
        return "Unknown", "this line is not a whole number or a half point", value, False
    won = (value > float(line)) if side_over else (value < float(line))
    if won:
        return "Win", "actual stat beat the archived line on the selected side", value, False
    return "Loss", "actual stat missed the archived line on the selected side", value, False


def attach_result(
    path: Path,
    archive_id: str,
    *,
    game_status: str,
    result_source: str,
    result_retrieved_at: str,
    actual: float | None = None,
    actual_provided: bool = False,
    event_id: str | None = None,
    player_id: str | None = None,
) -> dict[str, Any]:
    """Append a result. The pregame snapshot row is not updated."""
    if not str(result_source or "").strip():
        return {"ok": False, "reason": "result_source is required"}
    try:
        retrieved, retrieved_source = _parse_pair(result_retrieved_at, "result_retrieved_at")
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    moment = _utc(_utc_now())
    if _utc(retrieved) > moment + CLOCK_SKEW:
        return {"ok": False, "reason": "result timestamp is after the archive clock"}
    conn = connect(path)
    try:
        before = conn.execute("SELECT * FROM snapshots WHERE archive_id = ?", (archive_id,)).fetchone()
        if before is None:
            return {"ok": False, "reason": "archive id was not found"}
        if event_id is not None and str(event_id) != str(before["event_id"]):
            return {"ok": False, "reason": "event id does not match the archived snapshot"}
        if player_id is not None and str(player_id) != str(before["player_id"]):
            return {"ok": False, "reason": "player id does not match the archived snapshot"}
        kickoff = parse_aware_datetime(before["event_start"])
        if kickoff is None or _utc(retrieved) < _utc(kickoff):
            return {
                "ok": False,
                "reason": "result retrieval timestamp is before kickoff",
                "snapshot_unchanged": True,
            }
        snapshot_copy = _row_dict(before)
        try:
            settlement, reason, stored_actual, missing = settle_prop(
                market=before["canonical_market"],
                line=before["line"],
                side=before["side"],
                actual=actual,
                game_status=game_status,
                actual_provided=actual_provided,
            )
        except ValueError as exc:
            return {"ok": False, "reason": str(exc), "snapshot_unchanged": True}
        previous = _current_result(conn, archive_id)
        result_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO results (
                result_id, archive_id, attached_at, actual_stat, actual_missing, settlement,
                settlement_reason, game_status, result_source, result_retrieved_at,
                result_retrieved_at_source, supersedes_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                archive_id,
                _utc_text(moment),
                stored_actual,
                1 if missing else 0,
                settlement,
                reason,
                str(game_status),
                result_source,
                _utc_text(retrieved),
                retrieved_source,
                None if previous is None else previous["result_id"],
            ),
        )
        conn.commit()
        after = conn.execute("SELECT * FROM snapshots WHERE archive_id = ?", (archive_id,)).fetchone()
        if _row_dict(after) != snapshot_copy:
            raise RuntimeError("snapshot changed while attaching a result")
        return {
            "ok": True,
            "result_id": result_id,
            "settlement": settlement,
            "reason": reason,
            "archive_id": archive_id,
            "snapshot_unchanged": True,
        }
    finally:
        conn.close()


def add_correction(
    path: Path,
    archive_id: str,
    *,
    field_name: str,
    corrected_value: str,
    reason: str,
    source: str | None = None,
) -> dict[str, Any]:
    """Record a correction beside the snapshot. The snapshot value stays as archived."""
    if not field_name or not reason:
        return {"ok": False, "reason": "field_name and reason are required"}
    conn = connect(path)
    try:
        before = conn.execute("SELECT * FROM snapshots WHERE archive_id = ?", (archive_id,)).fetchone()
        if before is None:
            return {"ok": False, "reason": "archive id was not found"}
        if field_name not in before.keys():
            return {"ok": False, "reason": f"{field_name} is not a snapshot field"}
        original = before[field_name]
        snapshot_copy = _row_dict(before)
        correction_id = uuid.uuid4().hex
        moment = _utc(_utc_now())
        conn.execute(
            """
            INSERT INTO corrections (
                correction_id, archive_id, created_at, field_name, original_value,
                corrected_value, reason, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                correction_id,
                archive_id,
                _utc_text(moment),
                field_name,
                None if original is None else str(original),
                corrected_value,
                reason,
                source,
            ),
        )
        conn.commit()
        after = conn.execute("SELECT * FROM snapshots WHERE archive_id = ?", (archive_id,)).fetchone()
        if _row_dict(after) != snapshot_copy:
            raise RuntimeError("snapshot changed while recording a correction")
        return {"ok": True, "correction_id": correction_id, "snapshot_unchanged": True}
    finally:
        conn.close()


def list_ungraded(path: Path) -> list[dict[str, Any]]:
    """Eligible snapshots that still have no Win/Loss/Push/Void settlement."""
    conn = connect(path)
    try:
        rows = conn.execute("SELECT * FROM snapshots ORDER BY event_start, archive_id").fetchall()
        ungraded = []
        for row in rows:
            current = _current_result(conn, row["archive_id"])
            if current is None or current["settlement"] == "Unknown":
                item = _row_dict(row)
                item["current_settlement"] = None if current is None else current["settlement"]
                ungraded.append(item)
        return ungraded
    finally:
        conn.close()


def archive_stats(path: Path) -> dict[str, Any]:
    conn = connect(path)
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
        by_sport = {
            row["sport"]: row["n"]
            for row in conn.execute("SELECT sport, COUNT(*) AS n FROM snapshots GROUP BY sport")
        }
        qualified = conn.execute("SELECT COUNT(*) AS n FROM snapshots WHERE qualified = 1").fetchone()["n"]
        version = schema_version(conn)
    finally:
        conn.close()
    return {
        "snapshots": total,
        "by_sport": by_sport,
        "qualified": qualified,
        "ungraded": len(list_ungraded(path)),
        "schema_version": version,
        "model_version": MODEL_VERSION,
        "calibration_claimed": False,
        "predictive_accuracy_claimed": False,
        "note": "Saving pregame snapshots does not calibrate the model and does not show that edges win money.",
    }


def _export_rows(path: Path) -> dict[str, Any]:
    conn = connect(path)
    try:
        snapshots = [_row_dict(row) for row in conn.execute("SELECT * FROM snapshots ORDER BY archived_at, archive_id")]
        results = [_row_dict(row) for row in conn.execute("SELECT * FROM results ORDER BY attached_at, result_id")]
        corrections = [
            _row_dict(row) for row in conn.execute("SELECT * FROM corrections ORDER BY created_at, correction_id")
        ]
        return {
            "schema_version": schema_version(conn),
            "model_version": MODEL_VERSION,
            "calibration_claimed": False,
            "snapshots": snapshots,
            "results": results,
            "corrections": corrections,
        }
    finally:
        conn.close()


def export_archive(path: Path, destination: Path, *, file_format: str = "json") -> dict[str, Any]:
    payload = _export_rows(path)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if file_format == "json":
        destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    elif file_format == "csv":
        fields = list(payload["snapshots"][0].keys()) if payload["snapshots"] else ["archive_id"]
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(payload["snapshots"])
    else:
        return {"ok": False, "reason": "format must be json or csv"}
    return {"ok": True, "path": str(destination), "snapshots": len(payload["snapshots"])}


def import_archive(path: Path, source: Path) -> dict[str, Any]:
    """Restore sealed rows. Existing archive ids and observation keys are left untouched."""
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        return {"ok": False, "reason": "export file has no snapshots list"}
    conn = connect(path)
    inserted = 0
    skipped = 0
    try:
        for row in data["snapshots"]:
            if conn.execute("SELECT 1 FROM snapshots WHERE archive_id = ?", (row["archive_id"],)).fetchone():
                skipped += 1
                continue
            if conn.execute(
                "SELECT 1 FROM snapshots WHERE observation_key = ?", (row["observation_key"],)
            ).fetchone():
                skipped += 1
                continue
            columns = [key for key in row.keys()]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO snapshots ({', '.join(columns)}) VALUES ({placeholders})",
                [row[column] for column in columns],
            )
            inserted += 1
        for row in data.get("results") or []:
            if conn.execute("SELECT 1 FROM results WHERE result_id = ?", (row["result_id"],)).fetchone():
                continue
            columns = list(row.keys())
            conn.execute(
                f"INSERT INTO results ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [row[column] for column in columns],
            )
        for row in data.get("corrections") or []:
            if conn.execute(
                "SELECT 1 FROM corrections WHERE correction_id = ?", (row["correction_id"],)
            ).fetchone():
                continue
            columns = list(row.keys())
            conn.execute(
                f"INSERT INTO corrections ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [row[column] for column in columns],
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "inserted": inserted, "skipped_existing": skipped}


def pregame_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Snapshot fields only. Result columns are not part of what was known before the game."""
    hidden = {"results", "corrections", "current_settlement"}
    return {key: value for key, value in snapshot.items() if key not in hidden}


def load_props_file(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("props"), list):
        return raw["props"]
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    raise ValueError("props file must be an object or a list")
