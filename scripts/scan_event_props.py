"""Scan one selected event's live player props against archived point forecasts.

This command makes at most one event-specific Odds API request. It refuses to
call the provider if the event has started, the forecast archive is missing,
the selected market has no fresh forecast, or the market key is unsupported.
It never places bets and never claims calibrated probabilities.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from betting_model.collection import collect_joined
from betting_model.forecast_archive import pending_forecasts
from betting_model.odds import fetch_event_player_props
from betting_model.timeutil import parse_aware_datetime

SPORT_KEYS = {"nba": "basketball_nba", "nfl": "americanfootball_nfl"}
MARKET_MAP = {
    "nba": {
        "player_points": "PTS",
        "player_rebounds": "REB",
        "player_assists": "AST",
        "player_threes": "3PM",
        "player_points_rebounds_assists": "PRA",
    },
    "nfl": {
        "player_pass_yds": "PASS_YDS",
        "player_rush_yds": "RUSH_YDS",
        "player_reception_yds": "REC_YDS",
        "player_receptions": "REC",
    },
}
MAX_PROJECTION_AGE = timedelta(hours=6)


def build_projection_rows(
    forecasts: list[dict[str, Any]],
    *,
    sport: str,
    event_id: str,
    markets: list[str],
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Select the newest fresh snapshot per player/market and add both sides."""
    if sport not in SPORT_KEYS:
        raise ValueError(f"unsupported sport: {sport}")
    if not str(event_id).strip():
        raise ValueError("event_id is required")
    if not markets:
        raise ValueError("at least one market is required")
    unsupported = [market for market in markets if market not in MARKET_MAP[sport]]
    if unsupported:
        raise ValueError(f"unsupported {sport} Odds API markets: {', '.join(unsupported)}")

    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    clock = clock.astimezone(timezone.utc)
    wanted_forecast_markets = set(MARKET_MAP[sport][market] for market in markets)

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    counts = {
        "event_rows": 0, "wrong_sport_or_event": 0, "unrequested_market": 0,
        "stale_or_invalid_time": 0, "selected": 0,
    }
    for row in forecasts:
        if not isinstance(row, dict):
            continue
        if str(row.get("event_id") or "") != str(event_id) or str(row.get("sport") or "").lower() != sport:
            counts["wrong_sport_or_event"] += 1
            continue
        counts["event_rows"] += 1
        market = str(row.get("market") or "").upper()
        if market not in wanted_forecast_markets:
            counts["unrequested_market"] += 1
            continue
        event_start = parse_aware_datetime(row.get("event_start"))
        projection_at = parse_aware_datetime(row.get("projection_at"))
        if event_start is None or projection_at is None:
            counts["stale_or_invalid_time"] += 1
            continue
        if event_start <= clock or projection_at >= event_start:
            counts["stale_or_invalid_time"] += 1
            continue
        age = clock - projection_at.astimezone(timezone.utc)
        if age < timedelta(seconds=0) or age > MAX_PROJECTION_AGE:
            counts["stale_or_invalid_time"] += 1
            continue
        player_id = str(row.get("player_id") or "").strip()
        if (not player_id or row.get("projected_value") is None
                or not row.get("home_team_name") or not row.get("away_team_name")):
            counts["stale_or_invalid_time"] += 1
            continue
        key = (player_id, market)
        previous = latest.get(key)
        if previous is None or projection_at > parse_aware_datetime(previous["projection_at"]):
            latest[key] = row

    projections = []
    for row in latest.values():
        market = str(row["market"]).upper()
        # The legacy evaluator uses REC for receiving yards. The forecast
        # engine's REC market is catches, so match it as RECEPTIONS.
        collection_market = "RECEPTIONS" if market == "REC" else market
        for side in ("Over", "Under"):
            projections.append({
                "player": row.get("player_name"),
                "player_id": row.get("player_id"),
                "sport": sport,
                "event_id": str(event_id),
                "event_start": row.get("event_start"),
                "team": row.get("team"),
                "opponent": row.get("opponent"),
                "home_team_name": row.get("home_team_name"),
                "away_team_name": row.get("away_team_name"),
                "market": collection_market,
                "side": side,
                "projected": row.get("projected_value"),
                "projection_source": row.get("model_version"),
                "projection_at": row.get("projection_at"),
                "projection_input_at": row.get("projection_input_at"),
            })
    counts["selected"] = len(latest)
    if not projections:
        raise ValueError(
            "no fresh archived forecasts match this event and market selection; "
            "the Odds API was not called"
        )
    return projections, counts


def _team_key(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def verify_event_matchup(fetched: dict[str, Any], projections: list[dict[str, Any]]) -> dict[str, Any]:
    """Require exact normalized home/away team names before attaching provider prices."""
    if not projections:
        return {"ok": False, "reason": "no projection rows to verify"}
    expected_home = projections[0].get("home_team_name")
    expected_away = projections[0].get("away_team_name")
    provider_home = fetched.get("home_team")
    provider_away = fetched.get("away_team")
    if not expected_home or not expected_away or not provider_home or not provider_away:
        return {"ok": False, "reason": "full home/away team names are missing; prices were not archived"}
    if _team_key(expected_home) != _team_key(provider_home) or _team_key(expected_away) != _team_key(provider_away):
        return {
            "ok": False,
            "reason": "Odds API home/away teams do not exactly match the ESPN forecast matchup; prices were not archived",
            "expected_home": expected_home, "expected_away": expected_away,
            "provider_home": provider_home, "provider_away": provider_away,
        }
    return {
        "ok": True,
        "expected_home": expected_home, "expected_away": expected_away,
        "provider_home": provider_home, "provider_away": provider_away,
    }


def remap_price_event_ids(prices: list[dict[str, Any]], espn_event_id: str) -> list[dict[str, Any]]:
    """Keep provider IDs for audit while matching archive rows on ESPN IDs."""
    remapped = []
    for source in prices:
        if not isinstance(source, dict):
            continue
        row = dict(source)
        row["provider_event_id"] = row.get("event_id")
        row["event_id"] = str(espn_event_id)
        remapped.append(row)
    return remapped


def scan_event(
    *,
    sport: str,
    event_id: str,
    odds_event_id: str,
    markets: str,
    bookmakers: str,
    forecast_archive: Path,
    odds_archive: Path,
    journal_path: Path,
) -> dict[str, Any]:
    market_list = list(dict.fromkeys(value.strip() for value in markets.split(",") if value.strip()))
    book_list = ",".join(dict.fromkeys(value.strip() for value in bookmakers.split(",") if value.strip()))
    if not book_list:
        raise ValueError("at least one bookmaker key is required")
    forecasts = pending_forecasts(forecast_archive)
    projections, counts = build_projection_rows(
        forecasts, sport=sport, event_id=event_id, markets=market_list,
    )
    fetched = fetch_event_player_props(
        SPORT_KEYS[sport],
        odds_event_id,
        ",".join(market_list),
        bookmakers=book_list,
    )
    quota = {
        "requests_remaining": fetched.get("requests_remaining"),
        "requests_used": fetched.get("requests_used"),
        "requests_last": fetched.get("requests_last"),
    }
    matchup = verify_event_matchup(fetched, projections) if fetched.get("ok") else {
        "ok": False, "reason": fetched.get("reason") or "Odds API did not return event props",
    }
    if not fetched.get("ok"):
        result = {
            "ok": False,
            "reason": fetched.get("reason") or "Odds API did not return event props",
            "called": fetched.get("called"),
            "bookmaker_notes": fetched.get("bookmaker_notes"),
            "quota": quota,
            "stored": 0,
            "duplicates": 0,
            "rejected": 0,
            "conflicts": 0,
            "provider_failures": 1,
        }
    elif not matchup.get("ok"):
        result = {
            "ok": False,
            "reason": matchup.get("reason"),
            "called": True,
            "quota": quota,
            "stored": 0,
            "duplicates": 0,
            "rejected": len(fetched.get("records") or []),
            "conflicts": 0,
            "provider_failures": 0,
        }
    else:
        # Odds API event IDs differ from ESPN event IDs. The user supplies
        # both IDs; archive matching uses the ESPN ID, while the provider ID
        # remains in the raw price record for audit.
        prices = remap_price_event_ids(fetched.get("records") or [], event_id)
        result = collect_joined(
            odds_archive, projections, prices, journal_path=journal_path,
        )
        result["bookmaker_notes"] = fetched.get("bookmaker_notes")
        result["books_not_returned"] = fetched.get("books_not_returned")
        result["called"] = True
        result["quota"] = quota
    return {
        "ok": bool(result.get("ok")),
        "sport": sport,
        "sport_key": SPORT_KEYS[sport],
        "event_id": str(event_id),
        "odds_event_id": str(odds_event_id),
        "markets_requested": market_list,
        "bookmakers_requested": book_list.split(","),
        "forecast_selection": counts,
        "projections_supplied_to_matcher": len(projections),
        "collection": result,
        "matchup_verification": matchup,
        "quota": result.get("quota"),
        "odds_api_calls": 1 if result.get("called") else 0,
        "calibration_claimed": False,
        "automatic_betting": False,
        "warnings": [
            "The generic FanDuel US feed is not verified as FanDuel Indiana.",
            "Point forecasts remain uncalibrated; any screen result is not a qualified bet.",
            "A missing book or market means it was not returned by this source, not that the sportsbook has no line.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", choices=("nba", "nfl"), required=True)
    parser.add_argument("--event-id", required=True, help="ESPN event ID from the forecast report.")
    parser.add_argument("--odds-event-id", required=True, help="The Odds API event ID for the same matchup.")
    parser.add_argument("--markets", required=True, help="Comma-separated Odds API player market keys.")
    parser.add_argument("--bookmakers", default="fanduel,draftkings")
    parser.add_argument("--forecast-archive", type=Path, required=True)
    parser.add_argument("--odds-archive", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = scan_event(
            sport=args.sport,
            event_id=args.event_id,
            odds_event_id=args.odds_event_id,
            markets=args.markets,
            bookmakers=args.bookmakers,
            forecast_archive=args.forecast_archive,
            odds_archive=args.odds_archive,
            journal_path=args.journal,
        )
    except (ValueError, OSError) as exc:
        report = {
            "ok": False, "sport": args.sport, "event_id": args.event_id,
            "reason": str(exc), "odds_api_calls": 0,
            "calibration_claimed": False, "automatic_betting": False,
        }
    rendered = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    # A refusal before a provider call is a valid report, not a workflow crash.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
