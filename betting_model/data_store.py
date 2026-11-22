"""Local game/box-score store (data/<sport>/games.json, filled by scripts/backfill_espn.py
from ESPN public API). Read-only helpers; never invent stats — missing => ok False.

A missing stat is not stored as zero. A real zero from the source stays zero.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SPORTS = ("nba", "nhl", "nfl", "cfb")


def _root(root: Path | None) -> Path:
    return Path(root) if root is not None else DATA_DIR


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).split("-")[0].split("/")[0].replace("+", ""))
    except (TypeError, ValueError):
        return None


def games_path(sport: str, root: Path | None = None) -> Path:
    return _root(root) / sport / "games.json"


def load_store(sport: str, root: Path | None = None) -> dict[str, Any]:
    """Read a games file. A missing file is unavailable, not an empty season."""
    path = games_path(sport, root)
    if not path.exists():
        return {
            "ok": False,
            "reason": "unavailable",
            "detail": f"no game file at {path}",
            "path": str(path),
            "games": {},
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "reason": "invalid_json",
            "detail": f"games file is not valid JSON ({exc})",
            "path": str(path),
            "games": {},
        }
    games = data.get("games") if isinstance(data, dict) else None
    if not isinstance(games, dict):
        return {
            "ok": False,
            "reason": "invalid_shape",
            "detail": "games file has no games object",
            "path": str(path),
            "games": {},
        }
    return {"ok": True, "reason": None, "path": str(path), "games": games, "raw": data}


def load_games(sport: str, root: Path | None = None) -> list[dict[str, Any]]:
    store = load_store(sport, root)
    if not store["ok"]:
        if store["reason"] == "unavailable":
            return []
        raise ValueError(store["detail"])
    return sorted(store["games"].values(), key=lambda g: g.get("date_utc") or "")


MARKETS = {
    "nfl": {"PASS_YDS": ("passing", "passingYards"), "RUSH_YDS": ("rushing", "rushingYards"),
            "REC_YDS": ("receiving", "receivingYards"), "REC": ("receiving", "receptions")},
    "cfb": {"PASS_YDS": ("passing", "passingYards"), "RUSH_YDS": ("rushing", "rushingYards"),
            "REC_YDS": ("receiving", "receivingYards")},
    "nba": {"PTS": ("", "points"), "REB": ("", "rebounds"), "AST": ("", "assists"),
            "3PM": ("", "threePointFieldGoalsMade-threePointFieldGoalsAttempted")},
    "nhl": {"SOG": (None, "shotsTotal"), "G": (None, "goals"), "A": (None, "assists"),
            "SAVES": ("goalies", "saves")},
}


def _stat_value(player: dict[str, Any], sport: str, market: str) -> float | None:
    spec = MARKETS.get(sport, {}).get(market)
    if spec is None:
        return None
    category, key = spec
    if category is not None and player.get("category") != category:
        return None
    stats = player.get("stats") or {}
    if key not in stats:
        return None
    return _num(stats[key])


def player_log(sport: str, player: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Per-game rows for a player, oldest first. Did-not-play games are omitted."""
    rows = []
    for game in load_games(sport, root):
        row: dict[str, Any] = {}
        for entry in game.get("players") or []:
            if str(entry.get("player") or "").lower() != player.lower() or entry.get("dnp"):
                continue
            row.setdefault("team", entry.get("team"))
            if entry.get("athlete_id") and "player_id" not in row:
                row["player_id"] = entry.get("athlete_id")
            elif entry.get("athlete_id") and str(entry.get("athlete_id")) != str(row.get("player_id")):
                row.setdefault("conflicts", []).append("player_id")
            for market in MARKETS[sport]:
                value = _stat_value(entry, sport, market)
                if value is None:
                    continue
                if market in row and row[market] != value:
                    # Keep the first value and say the box had two different numbers.
                    row.setdefault("conflicts", []).append(market)
                    continue
                row[market] = value
        if row:
            row.update(
                date=str(game.get("date_utc") or "")[:10],
                game=game.get("name"),
                event_id=game.get("id"),
                week=game.get("week"),
            )
            if sport == "nba":
                parts = [row.get(k) for k in ("PTS", "REB", "AST")]
                if all(part is not None for part in parts):
                    row["PRA"] = parts[0] + parts[1] + parts[2]
                    row["DD"] = int(sum(1 for part in parts if part >= 10) >= 2)
            if sport == "nhl" and row.get("G") is not None and row.get("A") is not None:
                row["PTS"] = row["G"] + row["A"]
            rows.append(row)
    return rows


def player_dnp(sport: str, player: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Games the source marked did-not-play. These are not zeros."""
    found = []
    for game in load_games(sport, root):
        for entry in game.get("players") or []:
            if str(entry.get("player") or "").lower() == player.lower() and entry.get("dnp"):
                found.append(
                    {
                        "event_id": game.get("id"),
                        "date": str(game.get("date_utc") or "")[:10],
                        "player_id": entry.get("athlete_id"),
                        "dnp": True,
                    }
                )
    return found


def recent_avg(sport: str, player: str, n: int = 5, root: Path | None = None) -> dict[str, Any]:
    log = player_log(sport, player, root)[-n:]
    if not log:
        return {"ok": False, "player": player, "error": "no games in local store"}
    keys = {
        k
        for row in log
        for k, value in row.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and k != "week"
    }
    avg = {}
    for key in keys:
        present = [row[key] for row in log if row.get(key) is not None]
        if not present:
            continue
        avg[key] = round(sum(present) / len(present), 2)
    return {
        "ok": True,
        "player": player,
        "player_id": log[-1].get("player_id"),
        "games": len(log),
        "last": log[-1]["date"],
        "note": "recent average, not a projection",
        **avg,
    }


def actual_stat(
    sport: str,
    *,
    event_id: str | None,
    market: str,
    player: str | None = None,
    player_id: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Final stat for one player in one event.

    Missing and did-not-play are not returned as zero.
    """
    store = load_store(sport, root)
    if not store["ok"]:
        return {"ok": False, "reason": store["reason"], "detail": store["detail"]}
    if not event_id or event_id not in store["games"]:
        return {"ok": False, "reason": "event_not_in_store", "detail": "event id was not in the local game file"}
    game = store["games"][event_id]
    matches = []
    for entry in game.get("players") or []:
        id_match = player_id is not None and str(entry.get("athlete_id")) == str(player_id)
        name_match = player is not None and str(entry.get("player") or "").lower() == player.lower()
        if id_match or (player_id is None and name_match):
            matches.append(entry)
    if not matches:
        return {"ok": False, "reason": "player_not_in_event", "detail": "player was not in this event's box score"}
    if any(entry.get("dnp") for entry in matches) and all(entry.get("dnp") for entry in matches):
        return {"ok": False, "reason": "dnp", "detail": "source marked the player did not play. This is not a zero."}
    value = None
    for entry in matches:
        if entry.get("dnp"):
            continue
        found = _stat_value(entry, sport, market)
        if found is None:
            continue
        if value is None:
            value = found
        elif value != found:
            return {
                "ok": False,
                "reason": "conflicting_stats",
                "detail": f"more than one {market} value for this player in the event",
            }
    if value is None:
        return {
            "ok": False,
            "reason": "stat_missing",
            "detail": f"{market} was not in the box score. It was not treated as zero.",
        }
    return {
        "ok": True,
        "actual": value,
        "event_id": event_id,
        "matched_on": "player_id" if player_id else "name",
        "source": game.get("source"),
    }


def inspect_store(sport: str, root: Path | None = None) -> dict[str, Any]:
    """Data-quality notes. Does not repair the file."""
    store = load_store(sport, root)
    if not store["ok"]:
        return store
    issues = []
    for event_id, game in store["games"].items():
        if not game.get("date_utc"):
            issues.append({"event_id": event_id, "issue": "missing date_utc"})
        seen: dict[tuple, int] = {}
        for entry in game.get("players") or []:
            key = (str(entry.get("athlete_id") or entry.get("player")), str(entry.get("category")))
            seen[key] = seen.get(key, 0) + 1
            if not entry.get("athlete_id"):
                issues.append({"event_id": event_id, "issue": "missing athlete_id", "player": entry.get("player")})
        for key, count in seen.items():
            if count > 1:
                issues.append({"event_id": event_id, "issue": "duplicate player row", "player": key[0], "count": count})
    return {
        "ok": True,
        "sport": sport,
        "games": len(store["games"]),
        "issues": issues,
        "path": store["path"],
    }
