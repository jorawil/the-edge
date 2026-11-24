"""Read-only lookup of one archived pregame forecast.

This is the first, smallest connection between the ``opportunity-rate-v2``
pregame projection engine and live evaluation — and it deliberately changes
no evaluation behaviour.

What it does
------------
Return the newest fresh archived forecast snapshot for one
(event_id, player_id, market) triple, or an explicit, stable rejection
reason when no snapshot may be used. Freshness follows the same rules the
manual scan already applies (``scripts/scan_event_props.py``):

- the event must not have started;
- the projection must be strictly before the event start;
- the projection must not be more than six hours old;
- the projection must not be in the future (beyond the two-minute clock
  skew the archive allows).

What it does not do
-------------------
- It never opens the archive for writing. The SQLite file is opened
  read-only (``mode=ro``) and no migration is applied, so a lookup cannot
  change the archive, create it, or repair it.
- It never computes a probability, an edge, an expected value, or a
  qualification. Every successful result carries
  ``probability_permitted: False``.
- It never calls the network, the Odds API, or any sportsbook.
- It never averages, reconciles, or otherwise combines projections. If
  two snapshots share the newest projection timestamp but disagree on the
  projected value, the lookup refuses with ``ambiguous_snapshots``.
- It never falls back to a recent average, a manual number, or a name
  match. Identity is by stable ``player_id`` / ``event_id`` / canonical
  market only.
- Candidate timestamps are normalized to UTC before any ordering or
  comparison. The archive stores the projection timestamp exactly as
  supplied, and equivalent instants may be written with different UTC
  offsets, so lexicographic ordering of the stored text is never used
  to decide which snapshot is newest — two snapshots at the same UTC
  instant with different projected values are ambiguous, even when
  their stored texts differ.

Rejection reasons (stable strings, safe to branch on):

- ``missing_identifier`` — event_id, player_id, or market is blank.
- ``archive_not_found`` — the archive file does not exist.
- ``archive_invalid`` — the file is not a readable forecast archive.
- ``not_found`` — no snapshot matches the triple.
- ``invalid_event_start`` / ``invalid_projection_time`` — a stored
  timestamp on the newest candidate cannot be parsed.
- ``event_started`` — the event start is not after the lookup time.
- ``projection_not_before_event_start`` — the projection timestamp is
  not strictly before the event start.
- ``projection_in_future`` — the projection timestamp is more than two
  minutes after the lookup time.
- ``stale_projection`` — the projection is more than six hours old.
- ``ambiguous_snapshots`` — the newest projection timestamp has more
  than one distinct projected value.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .forecast_archive import CLOCK_SKEW, MAX_PROJECTION_AGE
from .timeutil import parse_aware_datetime, utc_now

REASONS = frozenset({
    "missing_identifier",
    "archive_not_found",
    "archive_invalid",
    "archive_needs_recovery",
    "not_found",
    "invalid_event_start",
    "invalid_projection_time",
    "event_started",
    "projection_not_before_event_start",
    "projection_in_future",
    "stale_projection",
    "ambiguous_snapshots",
})


def _read_only_connection(path: Path) -> sqlite3.Connection:
    quoted = urllib.parse.quote(str(path), safe="/")
    conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _is_readonly_recovery_error(exc: sqlite3.Error) -> bool:
    """Identify only SQLite extended codes that explicitly require read-only recovery."""
    recovery_codes = {
        getattr(sqlite3, "SQLITE_READONLY_RECOVERY", 264),
        getattr(sqlite3, "SQLITE_READONLY_ROLLBACK", 776),
    }
    recovery_names = {"SQLITE_READONLY_RECOVERY", "SQLITE_READONLY_ROLLBACK"}
    return (
        getattr(exc, "sqlite_errorcode", None) in recovery_codes
        or getattr(exc, "sqlite_errorname", None) in recovery_names
    )


def _forecast_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        payload = json.loads(item.pop("payload_json"))
        if not isinstance(payload, dict):
            payload = {}
    except (TypeError, json.JSONDecodeError, KeyError):
        payload = {}
    try:
        warnings = json.loads(item.pop("warnings_json"))
        if not isinstance(warnings, list):
            warnings = []
    except (TypeError, json.JSONDecodeError, KeyError):
        warnings = ["stored snapshot warnings could not be decoded"]
    item["warnings"] = warnings
    item["payload"] = payload
    # Windows live in the snapshot payload, not in schema columns. Surface
    # them when present so a caller never has to guess the configuration
    # that produced the forecast.
    for key in ("opportunity_window_games", "efficiency_window_games",
                "n_efficiency_history"):
        if key in payload:
            item[key] = payload[key]
    item["calibrated"] = bool(item.get("calibrated"))
    item["probability_permitted"] = False
    item["probability_permitted_reason"] = (
        "Forecast probabilities are uncalibrated; this lookup never enables "
        "probability, edge, EV, or QUALIFIED output."
    )
    return item


def get_latest_forecast(
    db_path: str | Path,
    *,
    event_id: str,
    player_id: str,
    market: str,
    sport: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Return the newest fresh archived forecast for one player/market/event.

    Returns ``{"ok": True, "forecast": {...}, "reason": None}`` on success,
    or ``{"ok": False, "forecast": None, "reason": "<stable reason>"}``.
    ``as_of`` must be timezone-aware when supplied; naive values raise
    ``ValueError`` rather than being guessed.
    """
    event = str(event_id or "").strip()
    player = str(player_id or "").strip()
    canonical_market = str(market or "").strip().upper()
    if not event or not player or not canonical_market:
        return {"ok": False, "forecast": None, "reason": "missing_identifier"}

    moment = as_of or utc_now()
    if moment.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    moment = moment.astimezone(timezone.utc)

    path = Path(db_path)
    if not path.exists():
        return {"ok": False, "forecast": None, "reason": "archive_not_found"}

    sql = (
        "SELECT * FROM forecast_snapshots "
        "WHERE event_id=? AND player_id=? AND market=?"
    )
    params: list[Any] = [event, player, canonical_market]
    if sport is not None:
        sql += " AND sport=?"
        params.append(str(sport).strip().lower())
    # NOTE: no ORDER BY on projection_at. Stored projection timestamps
    # keep their original UTC offset, so their text order is not their
    # chronological order. Ordering happens below, on UTC-normalized
    # datetimes. archived_at is always stored in UTC ("...Z"), so its
    # text order is chronological and is a safe deterministic tiebreak.
    sql += " ORDER BY archived_at DESC, snapshot_id ASC"

    try:
        with _read_only_connection(path) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        if _is_readonly_recovery_error(exc):
            # Report journal presence without opening, modifying, or recovering it.
            journal_present = Path(f"{path}-journal").exists()
            return {
                "ok": False,
                "forecast": None,
                "reason": "archive_needs_recovery",
                "journal_present": journal_present,
            }
        return {"ok": False, "forecast": None, "reason": "archive_invalid"}

    if not rows:
        return {"ok": False, "forecast": None, "reason": "not_found"}

    # Normalize every candidate's timestamps to UTC, then order newest
    # first. Unparseable timestamps sort last: they cannot be placed
    # chronologically, and a usable snapshot must win over one that
    # cannot even be dated.
    entries: list[tuple[dict[str, Any], datetime | None, datetime | None]] = []
    for row in rows:
        item = _forecast_dict(row)
        projection_utc = parse_aware_datetime(item.get("projection_at"))
        if projection_utc is not None:
            projection_utc = projection_utc.astimezone(timezone.utc)
        event_utc = parse_aware_datetime(item.get("event_start"))
        if event_utc is not None:
            event_utc = event_utc.astimezone(timezone.utc)
        entries.append((item, projection_utc, event_utc))
    entries.sort(key=lambda entry: entry[0].get("snapshot_id") or "")
    entries.sort(key=lambda entry: entry[0].get("archived_at") or "", reverse=True)
    entries.sort(
        key=lambda entry: (
            entry[1] is not None,
            entry[1] or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )

    # Evaluate candidates newest-first. The first reason encountered on
    # the newest candidate is the reported reason when nothing is usable.
    first_reason: str | None = None
    valid: list[tuple[dict[str, Any], datetime]] = []
    for item, projection_at, event_start in entries:
        if event_start is None:
            reason = "invalid_event_start"
        elif projection_at is None:
            reason = "invalid_projection_time"
        elif event_start <= moment:
            reason = "event_started"
        elif projection_at >= event_start:
            reason = "projection_not_before_event_start"
        elif projection_at > moment + CLOCK_SKEW:
            reason = "projection_in_future"
        elif moment - projection_at > MAX_PROJECTION_AGE:
            reason = "stale_projection"
        elif item.get("projected_value") is None:
            reason = "invalid_projection_time"
        else:
            reason = None
        if first_reason is None:
            first_reason = reason
        if reason is None and projection_at is not None:
            valid.append((item, projection_at))

    if not valid:
        return {"ok": False, "forecast": None, "reason": first_reason or "not_found"}

    # Ambiguity is judged on the normalized UTC instant, not the stored
    # text: the same instant written with a different offset is the
    # same instant.
    newest_projection_at = valid[0][1]
    newest = [item for item, instant in valid if instant == newest_projection_at]
    distinct_values = {float(item["projected_value"]) for item in newest}
    if len(distinct_values) > 1:
        return {"ok": False, "forecast": None, "reason": "ambiguous_snapshots"}

    forecast = newest[0]
    forecast["lookup_as_of"] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ok": True, "forecast": forecast, "reason": None}


__all__ = ["REASONS", "get_latest_forecast"]
