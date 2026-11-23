"""Optional one-shot schedule. This does not start a daemon and it does not bet.

A config file says whether a run is enabled and how long to wait after a
clean run. Provider failures do not start that wait, so the next invocation
can retry. Put the process on cron, or run it by hand. API keys stay in the
environment, not in the config file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import archive as archive_mod
from .collection import collect_joined, collect_pregame
from .grading import grade_archive
from .journal import Journal, default_journal_path
from .odds import normalize_player_props
from .result_sources import EspnResultSource, load_result_fixture
from .timeutil import parse_aware_datetime

_SECRET_KEYS = {"odds_api_key", "apikey", "api_key", "token", "secret", "password"}


def load_schedule(path: Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("schedule config must be a JSON object")
    if _contains_secret(document):
        raise ValueError("schedule config must not contain API keys or tokens; use environment variables")
    enabled = document.get("enabled") is True
    try:
        minutes = float(document.get("min_minutes_between_runs", 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("min_minutes_between_runs must be a number") from exc
    if minutes < 0:
        raise ValueError("min_minutes_between_runs cannot be negative")
    return {
        "enabled": enabled,
        "min_minutes_between_runs": minutes,
        "props": document.get("props"),
        "projections": document.get("projections"),
        "odds_fixture": document.get("odds_fixture"),
        "grade": document.get("grade", True) is not False,
        "results_fixture": document.get("results_fixture"),
        "database": document.get("database"),
        "journal": document.get("journal"),
    }


def _contains_secret(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _SECRET_KEYS and item not in (None, ""):
                return True
            if _contains_secret(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _due(journal: Journal, kind: str, minutes: float) -> bool:
    """True when there was no clean run, the last run failed open, or the wait has elapsed."""
    runs = journal.runs(kind)
    if not runs:
        return True
    latest = runs[0]
    summary = latest.get("summary") or {}
    if latest.get("status") != "completed" or int(summary.get("provider_failures") or 0) > 0:
        return True
    finished = parse_aware_datetime(latest.get("finished_at"))
    if finished is None:
        return True
    now = archive_mod._utc_now().astimezone(timezone.utc)
    elapsed = (now - finished.astimezone(timezone.utc)).total_seconds() / 60.0
    return elapsed >= minutes


def run_once(
    config: dict[str, Any],
    *,
    database: Path | None = None,
    journal_path: Path | None = None,
    grade_source: Any | None = None,
) -> dict[str, Any]:
    """Run collection and grading once when the config says to."""
    if not config.get("enabled"):
        return {
            "ok": True,
            "ran": False,
            "reason": "scheduling is off",
            "automatic_betting": False,
            "calibration_claimed": False,
        }
    database = Path(database or config.get("database") or archive_mod.default_db_path())
    journal_path = Path(journal_path or config.get("journal") or default_journal_path())
    journal = Journal(journal_path)
    minutes = float(config.get("min_minutes_between_runs") or 0)
    outcome: dict[str, Any] = {
        "ok": True,
        "ran": True,
        "automatic_betting": False,
        "calibration_claimed": False,
        "profit_reported": False,
    }
    if config.get("props") or config.get("projections"):
        if _due(journal, "collect", minutes):
            outcome["collect"] = _collect_from_config(
                config,
                database=database,
                journal_path=journal_path,
            )
            items = outcome["collect"].get("items") or []
            if items and all(item.get("action") == "skipped" for item in items):
                outcome["collect"]["skipped"] = True
        else:
            outcome["collect"] = {"skipped": True, "reason": "last successful collection is inside the wait"}
    if config.get("grade", True):
        if _due(journal, "grade", minutes):
            source = grade_source
            if source is None and config.get("results_fixture"):
                document = json.loads(Path(config["results_fixture"]).read_text(encoding="utf-8"))
                source = load_result_fixture(document)
            if source is None:
                source = EspnResultSource()
            outcome["grade"] = grade_archive(database, source, journal_path=journal_path)
        else:
            outcome["grade"] = {"skipped": True, "reason": "last successful grading is inside the wait"}
    for key in ("collect", "grade"):
        part = outcome.get(key)
        if isinstance(part, dict) and part.get("ok") is False:
            outcome["ok"] = False
    return outcome


def _collect_from_config(
    config: dict[str, Any],
    *,
    database: Path,
    journal_path: Path,
) -> dict[str, Any]:
    if config.get("props"):
        props = archive_mod.load_props_file(Path(config["props"]))
        return collect_pregame(database, props, journal_path=journal_path)
    projections = archive_mod.load_props_file(Path(config["projections"]))
    if not config.get("odds_fixture"):
        return {
            "ok": False,
            "stored": 0,
            "provider_failures": 1,
            "reason": "projections need an odds fixture in the schedule, or use a props file with prices already included",
        }
    payload = json.loads(Path(config["odds_fixture"]).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("bookmakers"):
        normalized = normalize_player_props(
            payload,
            retrieved_at=str(payload.get("as_of") or archive_mod._utc_now().astimezone(timezone.utc).isoformat()),
        )
        prices = normalized.get("records") or []
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        prices = payload["records"]
    else:
        return {
            "ok": False,
            "stored": 0,
            "provider_failures": 0,
            "rejected": 1,
            "reason": "odds fixture must be an event payload or a records list",
        }
    return collect_joined(database, projections, prices, journal_path=journal_path)
