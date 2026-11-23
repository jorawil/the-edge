"""The Odds API client. Never invents prices.

Checked against the public docs on November 22, 2026. No live call is made
from tests. See docs/ODDS_PROVIDER.md.

A missing book in a response means "not returned by this source." It does
not mean the sportsbook has no price.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

import requests

from .core import implied_probability
from .timeutil import parse_aware_datetime, utc_now

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_TIMEOUT_SECONDS = 30
# Keys that appear on the provider's US bookmaker list.
# bet365 is intentionally absent: the list has no US/UK/EU bet365 key.
DOCUMENTED_BOOK_KEYS = {
    "fanduel": {
        "paid_only": False,
        "region_verified": False,
        "note": "US FanDuel feed. The provider does not publish an Indiana-specific key.",
    },
    "draftkings": {
        "paid_only": False,
        "region_verified": False,
        "note": "US DraftKings feed. The provider does not name the state.",
    },
    "fanatics": {
        "paid_only": True,
        "region_verified": False,
        "note": "US Fanatics key. The provider says it is only on paid subscriptions.",
    },
}
# Featured /odds markets. Player props are a different endpoint.
FEATURED_MARKETS = {"h2h", "spreads", "totals", "outrights"}
DEFAULT_REQUESTED_BOOKS = "fanduel,draftkings"
_KEY_PATTERN = re.compile(r"(apiKey=)[^&\s]+", re.IGNORECASE)


def redact(text: str) -> str:
    """Remove an API key if a library error quoted the request URL."""
    return _KEY_PATTERN.sub(r"\1REDACTED", text)


def _no_key(sport_key: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "no ODDS_API_KEY",
        "hint": 'export ODDS_API_KEY="your_key"  # https://the-odds-api.com/',
        "sport_key": sport_key,
        "events": None,
        "called": False,
    }


def _select_books(bookmakers: str | None) -> tuple[str, list[str]]:
    """Books that are safe to put on a request, plus notes for the ones left off."""
    notes = []
    if bookmakers is None:
        requested = [part.strip() for part in DEFAULT_REQUESTED_BOOKS.split(",") if part.strip()]
        notes.append(
            "bet365 was not requested. The Odds API bookmaker list checked on "
            "2026-11-22 has no bet365 key for US, UK, or EU. bet365_au is a paid "
            "Australian feed limited to AFL and NRL game markets."
        )
        notes.append(
            "Fanatics was not requested. Its key is paid-only. Pass "
            "bookmakers='fanatics' only if the account includes that plan."
        )
    else:
        requested = [part.strip() for part in str(bookmakers).split(",") if part.strip()]
    kept = []
    for key in requested:
        if key in DOCUMENTED_BOOK_KEYS:
            kept.append(key)
            if DOCUMENTED_BOOK_KEYS[key]["paid_only"]:
                notes.append(f"{key} is documented as paid-only. A refusal from the provider is not a price.")
        else:
            notes.append(
                f"{key} is not a documented bookmaker key, so it was not sent. "
                "That does not mean the sportsbook has no line."
            )
    return ",".join(kept), notes


def _request(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float,
    attempts: int,
    transport: Callable[..., Any],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    last_error = "request failed"
    for attempt in range(attempts):
        try:
            response = transport(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_error = redact(str(exc))
            if attempt + 1 >= attempts:
                return {"ok": False, "reason": f"request_error: {last_error}"}
            sleep(min(2 ** attempt, 2))
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
            retry_after = response.headers.get("Retry-After") if hasattr(response, "headers") else None
            try:
                delay = float(retry_after) if retry_after is not None else float(2 ** attempt)
            except (TypeError, ValueError):
                delay = float(2 ** attempt)
            sleep(min(delay, 2))
            last_error = f"http_{response.status_code}"
            continue
        return {"ok": True, "response": response}
    return {"ok": False, "reason": last_error}


def _quota(response: Any) -> dict[str, Any]:
    headers = getattr(response, "headers", {}) or {}
    return {
        "requests_remaining": headers.get("x-requests-remaining"),
        "requests_used": headers.get("x-requests-used"),
        "requests_last": headers.get("x-requests-last"),
    }


def _read_json(response: Any) -> tuple[Any, str | None]:
    try:
        return response.json(), None
    except ValueError as exc:
        return None, f"malformed provider response: {exc}"


def fetch_live_odds(
    sport_key: str = "basketball_nba",
    *,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
    bookmakers: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = 3,
    transport: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Featured game odds (moneyline, spread, total). Not player props.

    Requires env ODDS_API_KEY. If missing, returns
    {ok: False, reason: "no ODDS_API_KEY"} without inventing prices.
    """
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        return _no_key(sport_key)
    market_names = [part.strip() for part in markets.split(",") if part.strip()]
    if any(name.startswith("player_") or name not in FEATURED_MARKETS for name in market_names):
        return {
            "ok": False,
            "reason": "this endpoint only documents h2h, spreads, totals, and outrights",
            "hint": "Player props use fetch_event_player_props, one event at a time.",
            "sport_key": sport_key,
            "called": False,
        }
    selected, notes = _select_books(bookmakers)
    if not selected:
        return {
            "ok": False,
            "reason": "no documented bookmaker keys to request",
            "bookmaker_notes": notes,
            "sport_key": sport_key,
            "called": False,
        }
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": key,
        "markets": markets,
        "oddsFormat": odds_format,
        "bookmakers": selected,
    }
    if not bookmakers:
        # Bookmaker list replaces regions, matching the previous client.
        pass
    else:
        params["regions"] = regions
    fetched = _request(
        url,
        params,
        timeout=timeout,
        attempts=attempts,
        transport=transport or requests.get,
        sleep=sleep,
    )
    if not fetched["ok"]:
        return {
            "ok": False,
            "reason": fetched["reason"],
            "sport_key": sport_key,
            "bookmaker_notes": notes,
            "called": True,
        }
    response = fetched["response"]
    quota = _quota(response)
    if response.status_code != 200:
        body = redact(getattr(response, "text", "") or "")[:500]
        return {
            "ok": False,
            "reason": f"http_{response.status_code}",
            "body": body,
            "sport_key": sport_key,
            "bookmaker_notes": notes,
            "called": True,
            **quota,
        }
    data, error = _read_json(response)
    if error:
        return {
            "ok": False,
            "reason": error,
            "sport_key": sport_key,
            "bookmaker_notes": notes,
            "called": True,
            **quota,
        }
    return {
        "ok": True,
        "sport_key": sport_key,
        "events": data,
        "count": len(data) if isinstance(data, list) else None,
        "bookmakers_requested": selected,
        "bookmaker_notes": notes,
        "player_props": "not available from this endpoint",
        "called": True,
        **quota,
    }


def fetch_sport_events(
    sport_key: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = 3,
    transport: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """List upcoming provider events so an ESPN event can be matched safely.

    This metadata endpoint does not request player markets. It still requires
    ODDS_API_KEY and returns the provider's quota headers when present.
    """
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        return _no_key(sport_key)
    url = f"{ODDS_API_BASE}/sports/{sport_key}/events"
    fetched = _request(
        url, {"apiKey": key}, timeout=timeout, attempts=attempts,
        transport=transport or requests.get, sleep=sleep,
    )
    if not fetched.get("ok"):
        return {
            "ok": False, "reason": fetched.get("reason"), "sport_key": sport_key,
            "events": None, "called": True,
        }
    response = fetched["response"]
    payload, error = _read_json(response)
    if error:
        return {
            "ok": False, "reason": error, "sport_key": sport_key,
            "events": None, "called": True, **_quota(response),
        }
    if not isinstance(payload, list):
        return {
            "ok": False, "reason": "invalid events response shape",
            "sport_key": sport_key, "events": None, "called": True,
            **_quota(response),
        }
    events = [
        event for event in payload
        if isinstance(event, dict) and event.get("id")
    ]
    return {
        "ok": True, "sport_key": sport_key, "events": events,
        "event_count": len(events), "called": True, **_quota(response),
    }


def fetch_event_player_props(
    sport_key: str,
    event_id: str,
    markets: str,
    *,
    regions: str = "us",
    odds_format: str = "american",
    bookmakers: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = 3,
    transport: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Player props for one event. Does not invent a price the provider left out."""
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        return _no_key(sport_key)
    if not event_id or not markets:
        return {"ok": False, "reason": "event_id and markets are required", "called": False}
    selected, notes = _select_books(bookmakers)
    if not selected:
        return {
            "ok": False,
            "reason": "no documented bookmaker keys to request",
            "bookmaker_notes": notes,
            "called": False,
        }
    url = f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "bookmakers": selected,
    }
    fetched = _request(
        url,
        params,
        timeout=timeout,
        attempts=attempts,
        transport=transport or requests.get,
        sleep=sleep,
    )
    if not fetched["ok"]:
        return {"ok": False, "reason": fetched["reason"], "sport_key": sport_key, "called": True, "bookmaker_notes": notes}
    response = fetched["response"]
    quota = _quota(response)
    if response.status_code != 200:
        return {
            "ok": False,
            "reason": f"http_{response.status_code}",
            "body": redact(getattr(response, "text", "") or "")[:500],
            "sport_key": sport_key,
            "called": True,
            "bookmaker_notes": notes,
            **quota,
        }
    data, error = _read_json(response)
    if error or not isinstance(data, dict):
        return {
            "ok": False,
            "reason": error or "malformed provider response",
            "sport_key": sport_key,
            "called": True,
            "bookmaker_notes": notes,
            **quota,
        }
    normalized = normalize_player_props(
        data,
        requested_books=selected.split(","),
        retrieved_at=as_of or utc_now().isoformat(),
    )
    normalized.update(
        {
            "ok": True,
            "sport_key": sport_key,
            "event_id": event_id,
            "bookmaker_notes": notes,
            "called": True,
            **quota,
        }
    )
    return normalized


def normalize_player_props(
    payload: dict[str, Any],
    *,
    requested_books: list[str] | None = None,
    retrieved_at: str,
    max_age_seconds: float = 30 * 60,
) -> dict[str, Any]:
    """Turn one event payload into price records. Same-book pairs get a no-vig probability."""
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "malformed provider response", "records": []}
    retrieved = parse_aware_datetime(retrieved_at)
    records: list[dict[str, Any]] = []
    seen: dict[tuple, int] = {}
    present_books = set()
    for book in payload.get("bookmakers") or []:
        book_key = str(book.get("key") or "")
        present_books.add(book_key)
        info = DOCUMENTED_BOOK_KEYS.get(book_key, {})
        for market in book.get("markets") or []:
            last_update = market.get("last_update")
            updated = parse_aware_datetime(last_update) if isinstance(last_update, str) else None
            age = None if updated is None or retrieved is None else (retrieved - updated).total_seconds()
            if updated is None:
                freshness, freshness_detail = "unverified", "provider did not give last_update"
            elif age is not None and age > max_age_seconds:
                freshness, freshness_detail = "stale", f"provider last_update is {age / 60:.0f} minutes before retrieval"
            elif age is not None and age < 0:
                freshness, freshness_detail = "unverified", "provider last_update is after retrieval"
            else:
                freshness, freshness_detail = "fresh", "provider last_update is inside the age limit"
            for outcome in market.get("outcomes") or []:
                name = str(outcome.get("name") or "")
                if name not in {"Over", "Under"}:
                    # Yes/No and other shapes are not graded as Over/Under here.
                    records.append(
                        {
                            "bookmaker_key": book_key,
                            "market": market.get("key"),
                            "player": outcome.get("description"),
                            "side": name or None,
                            "supported_shape": False,
                            "reason": "not an Over/Under price, so it was not graded",
                            "freshness": freshness,
                        }
                    )
                    continue
                identity = (
                    book_key,
                    market.get("key"),
                    outcome.get("description"),
                    name,
                    outcome.get("point"),
                )
                duplicate = identity in seen
                seen[identity] = seen.get(identity, 0) + 1
                records.append(
                    {
                        "bookmaker_key": book_key,
                        "book_display": book.get("title") or book_key,
                        "region_verified": False,
                        "region_note": info.get("note")
                        or "This bookmaker key does not prove a state. Indiana is not verified.",
                        "event_id": payload.get("id"),
                        "sport_key": payload.get("sport_key"),
                        "commence_time": payload.get("commence_time"),
                        "player": outcome.get("description"),
                        "market": market.get("key"),
                        "side": name,
                        "is_over": name == "Over",
                        "line": outcome.get("point"),
                        "american_odds": outcome.get("price"),
                        "provider": "the_odds_api",
                        "retrieved_at": retrieved_at,
                        "provider_last_update": last_update,
                        "freshness": freshness,
                        "freshness_detail": freshness_detail,
                        "duplicate": duplicate,
                        "supported_shape": True,
                        "source_note": "not available as an Indiana-verified price from this feed",
                    }
                )
    _attach_fair_prices(records)
    missing = []
    for book_key in requested_books or []:
        if book_key not in present_books:
            missing.append(
                {
                    "bookmaker_key": book_key,
                    "reason": "not available from this source",
                    "detail": "The provider did not return this book. That is not a claim that the book has no market.",
                }
            )
    return {
        "records": records,
        "duplicate_count": sum(1 for record in records if record.get("duplicate")),
        "books_not_returned": missing,
        "home_team": payload.get("home_team"),
        "away_team": payload.get("away_team"),
    }


def _attach_fair_prices(records: list[dict[str, Any]]) -> None:
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for record in records:
        if not record.get("supported_shape") or record.get("duplicate"):
            continue
        key = (
            record.get("bookmaker_key"),
            record.get("market"),
            record.get("player"),
            record.get("line"),
            record.get("provider_last_update"),
        )
        groups.setdefault(key, []).append(record)
    for group in groups.values():
        overs = [row for row in group if row.get("side") == "Over"]
        unders = [row for row in group if row.get("side") == "Under"]
        if len(overs) != 1 or len(unders) != 1:
            for row in group:
                row["fair_market_probability"] = None
                row["fair_market_reason"] = "the two sides were not a single matching pair"
            continue
        try:
            over_implied = implied_probability(overs[0]["american_odds"])
            under_implied = implied_probability(unders[0]["american_odds"])
        except (ValueError, TypeError):
            for row in group:
                row["fair_market_probability"] = None
                row["fair_market_reason"] = "one of the prices was not usable American odds"
            continue
        total = over_implied + under_implied
        overs[0]["fair_market_probability"] = round(over_implied / total, 6)
        unders[0]["fair_market_probability"] = round(under_implied / total, 6)
        for row in group:
            row["overround"] = round(total - 1.0, 6)
            row["fair_market_reason"] = None

