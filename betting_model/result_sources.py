"""Final stats from a source that can be checked.

The live source is ESPN's public site API. It needs no key. Tests pass a
transport and never open a socket. A local result file is labeled as a file.
It is not described as a live ESPN fetch.

A stat is stored only when the source has that field. A missing field stays
missing. The string ``"0"`` stays zero. A did-not-play flag is not turned
into zero. Combo stats are filled only when every piece was present.

Mapped keys were checked against public scoreboards and summaries on
November 22, 2026: NBA box keys, NFL passing/rushing/receiving keys, NHL
skater and goalie keys, and MLB batting versus pitching keys. WNBA uses the
NBA key map. The WNBA scoreboard path responded, and a WNBA box was not
downloaded. A key that is absent stays absent.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from .player_names import normalize_label
from .timeutil import parse_aware_datetime

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/"
START_TOLERANCE_SECONDS = 120.0
SOURCE_NAME = "espn"

SPORT_PATHS = {
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "nfl": "football/nfl",
    "cfb": "football/college-football",
    "nhl": "hockey/nhl",
    "mlb": "baseball/mlb",
}

_BASKETBALL = {
    "points": ("number", "PTS"),
    "rebounds": ("number", "REB"),
    "assists": ("number", "AST"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("made", "THREES"),
}
_FOOTBALL = {
    "passingYards": ("number", "PASS_YDS"),
    "rushingYards": ("number", "RUSH_YDS"),
    "receivingYards": ("number", "REC_YDS"),
    "receptions": ("number", "RECEPTIONS"),
    "passingTouchdowns": ("number", "PASS_TDS"),
    "rushingTouchdowns": ("number", "RUSH_TDS"),
    "receivingTouchdowns": ("number", "REC_TDS"),
    "rushingAttempts": ("number", "RUSH_ATT"),
    "completions/passingAttempts": ("slash_pair", "COMPLETIONS", "PASS_ATT"),
}
_HOCKEY = {
    "goals": ("number", "GOALS"),
    "assists": ("number", "ASSISTS"),
    "shotsTotal": ("number", "SOG"),
    "saves": ("number", "SAVES"),
}
_MLB_BATTING = {
    "hits": ("number", "HITS"),
    "atBats": ("number", "AT_BATS"),
    "runs": ("number", "RUNS"),
    "RBIs": ("number", "RBIS"),
    "homeRuns": ("number", "HOME_RUNS"),
    "walks": ("number", "BATTER_WALKS"),
    "strikeouts": ("number", "BATTER_STRIKEOUTS"),
}
_MLB_PITCHING = {
    "strikeouts": ("number", "PITCHER_STRIKEOUTS"),
    "hits": ("number", "HITS_ALLOWED"),
    "earnedRuns": ("number", "EARNED_RUNS"),
    "walks": ("number", "WALKS_ALLOWED"),
    "homeRuns": ("number", "HOME_RUNS_ALLOWED"),
}

_STATUS_NAMES = {
    "STATUS_FINAL": "final",
    "STATUS_FINAL_OVERTIME": "final",
    "STATUS_FULL_TIME": "final",
    "STATUS_FINAL_PENALTIES": "final",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "canceled",
    "STATUS_CANCELLED": "canceled",
    "STATUS_SUSPENDED": "suspended",
    "STATUS_DELAYED": "delayed",
    "STATUS_RAIN_DELAY": "delayed",
    "STATUS_IN_PROGRESS": "in_progress",
    "STATUS_HALFTIME": "in_progress",
    "STATUS_END_PERIOD": "in_progress",
    "STATUS_FIRST_HALF": "in_progress",
    "STATUS_SECOND_HALF": "in_progress",
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_PREGAME": "scheduled",
}


def interpret_status(status_type: object) -> str:
    """Map an ESPN status object. An unknown name stays unknown."""
    if not isinstance(status_type, dict):
        return "unknown"
    name = str(status_type.get("name") or "").upper()
    completed = status_type.get("completed") is True
    mapped = _STATUS_NAMES.get(name)
    if mapped is None:
        return "unknown"
    if mapped == "final" and not completed:
        return "unknown"
    return mapped


def _plain_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not text or ":" in text or any(char.isalpha() for char in text):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _split_pair(value: object, separator: str) -> tuple[float, float] | None:
    if not isinstance(value, str) or separator not in value:
        return None
    left, right = value.split(separator, 1)
    made = _plain_number(left.strip())
    other = _plain_number(right.strip())
    if made is None or other is None:
        return None
    return made, other


def _maps_for(sport: str, category: object) -> dict[str, tuple]:
    if sport in {"nba", "wnba"}:
        return _BASKETBALL
    if sport in {"nfl", "cfb"}:
        return _FOOTBALL
    if sport == "nhl":
        return _HOCKEY
    if sport == "mlb":
        name = str(category or "").strip().casefold()
        if name == "batting":
            return _MLB_BATTING
        if name == "pitching":
            return _MLB_PITCHING
    return {}


def _put(stats: dict[str, float], conflicts: set[str], key: str, value: float) -> None:
    if key in conflicts:
        return
    if key not in stats:
        stats[key] = value
        return
    if abs(stats[key] - value) <= 1e-9:
        return
    conflicts.add(key)
    stats.pop(key, None)


def _apply_spec(stats: dict[str, float], conflicts: set[str], spec: tuple, raw: object) -> None:
    kind = spec[0]
    if kind == "number":
        number = _plain_number(raw)
        if number is None:
            return
        _put(stats, conflicts, spec[1], number)
        return
    if kind == "made":
        pair = _split_pair(raw, "-")
        if pair is None:
            return
        _put(stats, conflicts, spec[1], pair[0])
        return
    if kind == "slash_pair":
        pair = _split_pair(raw, "/")
        if pair is None:
            return
        _put(stats, conflicts, spec[1], pair[0])
        _put(stats, conflicts, spec[2], pair[1])


def _derive(stats: dict[str, float], conflicts: set[str], sport: str) -> None:
    if sport in {"nba", "wnba"}:
        if all(key in stats and key not in conflicts for key in ("PTS", "REB", "AST")):
            _put(stats, conflicts, "PRA", stats["PTS"] + stats["REB"] + stats["AST"])
            _put(stats, conflicts, "PR", stats["PTS"] + stats["REB"])
            _put(stats, conflicts, "PA", stats["PTS"] + stats["AST"])
    if sport in {"nfl", "cfb"}:
        if all(key in stats and key not in conflicts for key in ("RUSH_YDS", "REC_YDS")):
            _put(stats, conflicts, "RUSH_REC_YDS", stats["RUSH_YDS"] + stats["REC_YDS"])


def parse_boxscore(payload: dict[str, Any], sport: str) -> list[dict[str, Any]]:
    """Players from an ESPN summary. Equal repeated values are one stat."""
    grouped: dict[str, dict[str, Any]] = {}
    for team_block in (payload.get("boxscore") or {}).get("players") or []:
        team = (team_block.get("team") or {}).get("abbreviation")
        for category in team_block.get("statistics") or []:
            mapping = _maps_for(sport, category.get("name") or category.get("type"))
            keys = category.get("keys") or category.get("names") or []
            if not isinstance(keys, list):
                continue
            for athlete in category.get("athletes") or []:
                person = athlete.get("athlete") or {}
                player_id = person.get("id")
                name = person.get("displayName") or person.get("fullName")
                identity = str(player_id) if player_id not in (None, "") else normalize_label(name)
                if not identity:
                    continue
                slot = grouped.setdefault(
                    identity,
                    {
                        "name": name,
                        "player_id": None if player_id in (None, "") else str(player_id),
                        "team": team,
                        "dnp_rows": 0,
                        "played_rows": 0,
                        "stats": {},
                        "conflicts": set(),
                    },
                )
                if athlete.get("didNotPlay") is True:
                    slot["dnp_rows"] += 1
                    continue
                slot["played_rows"] += 1
                values = athlete.get("stats") or []
                if not isinstance(values, list):
                    continue
                for index, key in enumerate(keys):
                    if key not in mapping or index >= len(values):
                        continue
                    _apply_spec(slot["stats"], slot["conflicts"], mapping[key], values[index])
    players = []
    for slot in grouped.values():
        _derive(slot["stats"], slot["conflicts"], sport)
        dnp = slot["dnp_rows"] > 0 and slot["played_rows"] == 0
        conflict = slot["dnp_rows"] > 0 and slot["played_rows"] > 0
        players.append(
            {
                "name": slot["name"],
                "player_id": slot["player_id"],
                "team": slot["team"],
                "dnp": dnp,
                "dnp_conflict": conflict,
                "stats": {} if conflict else {key: value for key, value in slot["stats"].items() if key not in slot["conflicts"]},
                "stat_conflicts": sorted(slot["conflicts"]),
            }
        )
    return players


def _team_side(competitor: dict[str, Any]) -> dict[str, str | None]:
    team = competitor.get("team") or {}
    return {
        "name": team.get("displayName") or team.get("name"),
        "abbr": team.get("abbreviation"),
    }


def _labels(side: dict[str, Any] | None) -> set[str]:
    if not isinstance(side, dict):
        return set()
    found = set()
    for key in ("name", "abbr", "abbreviation", "display_name"):
        label = normalize_label(side.get(key))
        if label:
            found.add(label)
    return found


def team_relation(
    team: object,
    opponent: object,
    home: dict[str, Any] | None,
    away: dict[str, Any] | None,
) -> str:
    """Return confirmed, unconfirmed, or contradiction.

    Both archived sides must equal the home and away labels, in either
    order, to be confirmed. No shared label is a contradiction. One shared
    label, or a full name beside an abbreviation, stays unconfirmed.
    """
    left = normalize_label(team)
    right = normalize_label(opponent)
    if not left or not right:
        return "unconfirmed"
    home_labels = _labels(home)
    away_labels = _labels(away)
    if not home_labels or not away_labels:
        return "unconfirmed"
    if (left in home_labels and right in away_labels) or (left in away_labels and right in home_labels):
        return "confirmed"
    pool = home_labels | away_labels
    if left not in pool and right not in pool:
        return "contradiction"
    return "unconfirmed"


def starts_match(left: object, right: object, *, tolerance: float = START_TOLERANCE_SECONDS) -> bool:
    first = parse_aware_datetime(left) if not isinstance(left, datetime) else left
    second = parse_aware_datetime(right) if not isinstance(right, datetime) else right
    if first is None or second is None:
        return False
    return abs((first - second).total_seconds()) <= tolerance


def _competition(payload: dict[str, Any]) -> dict[str, Any]:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    competitions = header.get("competitions") if isinstance(header, dict) else None
    if isinstance(competitions, list) and competitions and isinstance(competitions[0], dict):
        return competitions[0]
    if isinstance(payload.get("competitions"), list) and payload["competitions"]:
        return payload["competitions"][0]
    return {}


def parse_event_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Event id, start, status, and teams from a summary or a scoreboard event."""
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    competition = _competition(payload)
    status_type = ((competition.get("status") or header.get("status") or payload.get("status") or {}).get("type"))
    home = away = None
    for competitor in competition.get("competitors") or []:
        if not isinstance(competitor, dict):
            continue
        side = _team_side(competitor)
        if competitor.get("homeAway") == "home":
            home = side
        elif competitor.get("homeAway") == "away":
            away = side
    event_id = header.get("id") or payload.get("id")
    start = competition.get("date") or header.get("date") or payload.get("date")
    return {
        "event_id": None if event_id in (None, "") else str(event_id),
        "status": interpret_status(status_type),
        "start": start if isinstance(start, str) else None,
        "home": home or {"name": None, "abbr": None},
        "away": away or {"name": None, "abbr": None},
    }


def _failure(reason: str, *, source: str) -> dict[str, Any]:
    return {
        "ok": False,
        "failure": True,
        "reason": reason,
        "source": source,
        "event_id": None,
        "matched_by": None,
        "status": "unknown",
        "start": None,
        "home": {"name": None, "abbr": None},
        "away": {"name": None, "abbr": None},
        "players": [],
    }


def _unverified(reason: str, *, source: str) -> dict[str, Any]:
    result = _failure(reason, source=source)
    result["failure"] = False
    return result


def _envelope(
    *,
    source: str,
    identity: dict[str, Any],
    players: list[dict[str, Any]],
    matched_by: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "failure": False,
        "reason": None,
        "source": source,
        "event_id": identity.get("event_id"),
        "matched_by": matched_by,
        "status": identity.get("status") or "unknown",
        "start": identity.get("start"),
        "home": identity.get("home") or {"name": None, "abbr": None},
        "away": identity.get("away") or {"name": None, "abbr": None},
        "players": players,
    }


def _default_transport(url: str, timeout: float = 30):
    return requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "the-edge-results/0.2 (public box score; no bets)"},
    )


class EspnResultSource:
    """Public ESPN summaries. A 404 can fall back to one unique team-and-start match."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        transport: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        delay: float = 0.25,
        attempts: int = 3,
        timeout: float = 30,
    ) -> None:
        self.transport = transport or _default_transport
        self.sleep = sleep or time.sleep
        self.delay = delay
        self.attempts = max(1, attempts)
        self.timeout = timeout
        self._calls = 0
        self._cache: dict[tuple, dict[str, Any]] = {}

    def summary_url(self, sport: str, event_id: str) -> str:
        return ESPN_BASE + SPORT_PATHS[sport] + f"/summary?event={event_id}"

    def scoreboard_url(self, sport: str, day: datetime) -> str:
        groups = "&groups=80" if sport == "cfb" else ""
        return ESPN_BASE + SPORT_PATHS[sport] + f"/scoreboard?dates={day:%Y%m%d}&limit=500{groups}"

    def fetch_event(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        sport = str(snapshot.get("sport") or "").strip().lower()
        event_id = str(snapshot.get("event_id") or "")
        key = (
            sport,
            event_id,
            normalize_label(snapshot.get("team")),
            normalize_label(snapshot.get("opponent")),
            str(snapshot.get("event_start") or ""),
        )
        if key in self._cache:
            return self._cache[key]
        if sport not in SPORT_PATHS:
            result = _unverified(
                f"{sport or 'missing sport'} is not a mapped ESPN sport, so no result was guessed",
                source=SOURCE_NAME,
            )
            self._cache[key] = result
            return result
        if not event_id:
            result = _unverified("archived event id is missing", source=SOURCE_NAME)
            self._cache[key] = result
            return result
        loaded = self._get(self.summary_url(sport, event_id))
        if loaded.get("failure"):
            self._cache[key] = loaded
            return loaded
        if loaded.get("ok"):
            parsed = self._from_summary(loaded["data"], sport, requested_id=event_id)
            if parsed.get("ok"):
                relation = team_relation(
                    snapshot.get("team"),
                    snapshot.get("opponent"),
                    parsed.get("home"),
                    parsed.get("away"),
                )
                if relation == "contradiction":
                    parsed = _unverified(
                        "event id matched a game whose teams contradict the archived teams",
                        source=parsed["source"],
                    )
                self._cache[key] = parsed
                return parsed
            if not parsed.get("not_found"):
                self._cache[key] = parsed
                return parsed
        fallback = self._by_team_and_start(snapshot, sport)
        self._cache[key] = fallback
        return fallback

    def _from_summary(self, payload: object, sport: str, *, requested_id: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return _failure("malformed provider response", source=SOURCE_NAME)
        identity = parse_event_identity(payload)
        source = self.summary_url(sport, requested_id)
        if identity.get("event_id") and identity["event_id"] != str(requested_id):
            result = _unverified(
                "summary event id did not match the requested event",
                source=source,
            )
            result["not_found"] = False
            return result
        if identity.get("event_id") is None and not payload.get("boxscore") and not payload.get("header"):
            result = _unverified("summary did not contain that event", source=source)
            result["not_found"] = True
            return result
        identity["event_id"] = identity.get("event_id") or str(requested_id)
        return _envelope(
            source=source,
            identity=identity,
            players=parse_boxscore(payload, sport),
            matched_by="event_id",
        )

    def _by_team_and_start(self, snapshot: dict[str, Any], sport: str) -> dict[str, Any]:
        start = parse_aware_datetime(snapshot.get("event_start"))
        if start is None:
            return _unverified(
                "event id was not found and the archived start time could not be read",
                source=SOURCE_NAME,
            )
        found = []
        for offset in (-1, 0, 1):
            day = (start.astimezone(timezone.utc) + timedelta(days=offset))
            loaded = self._get(self.scoreboard_url(sport, day))
            if not loaded.get("ok"):
                if loaded.get("failure"):
                    return loaded
                continue
            data = loaded["data"]
            events = data.get("events") if isinstance(data, dict) else None
            for event in events or []:
                if not isinstance(event, dict):
                    continue
                identity = parse_event_identity(event)
                if not identity.get("event_id"):
                    continue
                if not starts_match(snapshot.get("event_start"), identity.get("start")):
                    continue
                if team_relation(snapshot.get("team"), snapshot.get("opponent"), identity.get("home"), identity.get("away")) != "confirmed":
                    continue
                found.append(identity["event_id"])
        unique = list(dict.fromkeys(found))
        if len(unique) != 1:
            return _unverified(
                "event id was not found and team plus start time did not identify one game",
                source=SOURCE_NAME,
            )
        loaded = self._get(self.summary_url(sport, unique[0]))
        if not loaded.get("ok"):
            return loaded if loaded.get("failure") else _unverified(loaded.get("reason") or "summary was not available", source=SOURCE_NAME)
        parsed = self._from_summary(loaded["data"], sport, requested_id=unique[0])
        if not parsed.get("ok"):
            return parsed
        relation = team_relation(snapshot.get("team"), snapshot.get("opponent"), parsed.get("home"), parsed.get("away"))
        if relation != "confirmed":
            return _unverified(
                "the scoreboard match was not confirmed by the summary teams",
                source=parsed.get("source") or SOURCE_NAME,
            )
        parsed["matched_by"] = "team_and_start"
        parsed["source"] = parsed["source"] + " (matched by team and start, not by the archived event id)"
        return parsed

    def _get(self, url: str) -> dict[str, Any]:
        last = "request failed"
        retry_after = 0.0
        for attempt in range(self.attempts):
            if attempt:
                self.sleep(max(retry_after, min(2 ** (attempt - 1), 2.0)))
            elif self._calls and self.delay:
                self.sleep(self.delay)
            retry_after = 0.0
            self._calls += 1
            try:
                response = self.transport(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last = str(exc)[:300]
                continue
            status = getattr(response, "status_code", None)
            if status == 404:
                return {"ok": False, "failure": False, "not_found": True, "reason": "http_404", "source": url}
            if status in {429, 500, 502, 503, 504}:
                last = f"http_{status}"
                headers = getattr(response, "headers", {}) or {}
                if status == 429 and headers.get("Retry-After") not in (None, ""):
                    try:
                        retry_after = min(float(headers.get("Retry-After")), 5.0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                continue
            if status != 200:
                return _failure(f"http_{status}", source=url)
            try:
                data = response.json()
            except ValueError:
                return _failure("malformed provider response", source=url)
            return {"ok": True, "data": data, "source": url}
        return _failure(last, source=url)


def load_result_fixture(document: dict[str, Any]) -> "FixtureResultSource":
    """A local file of results. The kind flag keeps it from being called a live feed."""
    if not isinstance(document, dict) or document.get("kind") != "result_fixture":
        raise ValueError(
            "results file must set kind to result_fixture so a local file is not treated as a live feed"
        )
    label = str(document.get("source_label") or "").strip()
    if not label:
        raise ValueError("source_label is required on a result fixture")
    if label.casefold().startswith("espn"):
        label = "local file, not a live ESPN fetch: " + label
    events = document.get("events")
    if isinstance(events, dict):
        rows = list(events.values())
    elif isinstance(events, list):
        rows = events
    else:
        raise ValueError("result fixture events must be a list or an object")
    return FixtureResultSource(rows, label=label)


class FixtureResultSource:
    """Normalized events supplied by the caller. Stats are used as written, including zero."""

    name = "fixture"

    def __init__(self, events: list[dict[str, Any]], *, label: str) -> None:
        self.events = [event for event in events if isinstance(event, dict)]
        self.label = label

    def fetch_event(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        wanted = str(snapshot.get("event_id") or "")
        by_id = [event for event in self.events if str(event.get("event_id") or "") == wanted and wanted]
        if len(by_id) == 1:
            return self._convert(by_id[0], matched_by="event_id", snapshot=snapshot)
        if len(by_id) > 1:
            return _unverified("more than one fixture event uses this event id", source=self.label)
        found = []
        for event in self.events:
            if not starts_match(snapshot.get("event_start"), event.get("start")):
                continue
            if team_relation(snapshot.get("team"), snapshot.get("opponent"), event.get("home"), event.get("away")) != "confirmed":
                continue
            found.append(event)
        if len(found) != 1:
            return _unverified(
                "fixture has no verified event for this id, and team plus start time did not identify one",
                source=self.label,
            )
        converted = self._convert(found[0], matched_by="team_and_start", snapshot=snapshot)
        converted["source"] = self.label + " (matched by team and start, not by the archived event id)"
        return converted

    def _convert(self, event: dict[str, Any], *, matched_by: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        status = str(event.get("status") or "unknown").strip().lower().replace("-", "_")
        if status == "cancelled":
            status = "canceled"
        allowed = {
            "final",
            "postponed",
            "canceled",
            "suspended",
            "delayed",
            "in_progress",
            "scheduled",
            "incomplete",
            "unknown",
        }
        if status not in allowed:
            status = "unknown"
        if status == "incomplete":
            status = "suspended"
        players = []
        for player in event.get("players") or []:
            if not isinstance(player, dict):
                continue
            stats: dict[str, float] = {}
            conflicts: set[str] = set()
            raw_stats = player.get("stats") if isinstance(player.get("stats"), dict) else {}
            for key, value in raw_stats.items():
                number = _plain_number(value)
                if number is None:
                    continue
                _put(stats, conflicts, str(key), number)
            dnp = player.get("dnp") is True
            has_stats = bool(stats)
            players.append(
                {
                    "name": player.get("name") or player.get("player"),
                    "player_id": None if player.get("player_id") in (None, "") else str(player.get("player_id")),
                    "team": player.get("team"),
                    "dnp": dnp and not has_stats,
                    "dnp_conflict": dnp and has_stats,
                    "stats": {} if dnp and has_stats else stats,
                    "stat_conflicts": sorted(conflicts),
                }
            )
        identity = {
            "event_id": None if event.get("event_id") in (None, "") else str(event.get("event_id")),
            "status": status,
            "start": event.get("start"),
            "home": event.get("home") or {"name": None, "abbr": None},
            "away": event.get("away") or {"name": None, "abbr": None},
        }
        if matched_by == "event_id":
            relation = team_relation(snapshot.get("team"), snapshot.get("opponent"), identity["home"], identity["away"])
            if relation == "contradiction":
                return _unverified(
                    "event id matched a fixture whose teams contradict the archived teams",
                    source=self.label,
                )
        return _envelope(source=self.label, identity=identity, players=players, matched_by=matched_by)
