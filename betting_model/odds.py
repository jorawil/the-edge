"""The Odds API skeleton. Never invents prices."""

from __future__ import annotations

import os
from typing import Any

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def fetch_live_odds(
    sport_key: str = "basketball_nba",
    *,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
    bookmakers: str | None = None,
) -> dict[str, Any]:
    """Fetch live odds from The Odds API.

    Requires env ODDS_API_KEY. If missing, returns
    {ok: False, reason: "no ODDS_API_KEY"} without inventing prices.

    Set later:
      export ODDS_API_KEY="your_key_here"
    Free tier: https://the-odds-api.com/
    """
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        return {
            "ok": False,
            "reason": "no ODDS_API_KEY",
            "hint": 'export ODDS_API_KEY="your_key"  # https://the-odds-api.com/',
            "sport_key": sport_key,
        }

    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
    }
    if bookmakers is None:
        # Default to Joey's books in order: FanDuel (IN), DraftKings, bet365, Fanatics.
        from .books import BOOK_KEYS, ODDS_API_BOOKMAKERS
        bookmakers = ",".join(ODDS_API_BOOKMAKERS[k] for k in BOOK_KEYS)
    if bookmakers:
        params["bookmakers"] = bookmakers
        params.pop("regions", None)
    try:
        resp = requests.get(url, params=params, timeout=30)
        remaining = resp.headers.get("x-requests-remaining")
        used = resp.headers.get("x-requests-used")
        if resp.status_code != 200:
            return {
                "ok": False,
                "reason": f"http_{resp.status_code}",
                "body": resp.text[:500],
                "sport_key": sport_key,
                "requests_remaining": remaining,
                "requests_used": used,
            }
        data = resp.json()
        return {
            "ok": True,
            "sport_key": sport_key,
            "events": data,
            "count": len(data) if isinstance(data, list) else None,
            "requests_remaining": remaining,
            "requests_used": used,
        }
    except requests.RequestException as e:
        return {
            "ok": False,
            "reason": f"request_error: {e}",
            "sport_key": sport_key,
        }
