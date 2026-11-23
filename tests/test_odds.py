"""Odds client tests use fake responses. They do not call The Odds API."""

import json

import requests

from betting_model.odds import fetch_event_player_props, fetch_live_odds, fetch_sport_events, normalize_player_props


class _Response:
    def __init__(self, status, payload, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_missing_key_does_not_call_the_network(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("network was called")

    result = fetch_live_odds(transport=boom)
    assert result["ok"] is False
    assert result["reason"] == "no ODDS_API_KEY"
    assert result["called"] is False


def test_default_request_skips_bet365_and_player_props(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    seen = {}

    def transport(url, params=None, timeout=None):
        seen["params"] = params
        seen["url"] = url
        return _Response(200, [], headers={"x-requests-remaining": "9"})

    result = fetch_live_odds(transport=transport, sleep=lambda _seconds: None)
    assert result["ok"] is True
    assert "bet365" not in seen["params"]["bookmakers"]
    assert "fanatics" not in seen["params"]["bookmakers"]
    assert seen["params"]["bookmakers"] == "fanduel,draftkings"
    assert "test-key" not in json.dumps({k: v for k, v in result.items() if k != "events"})
    blocked = fetch_live_odds(markets="player_points", transport=transport, sleep=lambda _seconds: None)
    assert blocked["ok"] is False
    assert blocked["called"] is False


def test_errors_do_not_echo_the_key_and_malformed_json_is_rejected(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "super-secret")

    def transport(url, params=None, timeout=None):
        raise requests.ConnectionError("GET https://example.test?apiKey=super-secret failed")

    result = fetch_live_odds(transport=transport, attempts=1, sleep=lambda _seconds: None)
    assert result["ok"] is False
    assert "super-secret" not in result["reason"]
    assert "REDACTED" in result["reason"]

    def bad_json(url, params=None, timeout=None):
        return _Response(200, ValueError("not json"))

    broken = fetch_live_odds(transport=bad_json, sleep=lambda _seconds: None)
    assert broken["ok"] is False
    assert "malformed" in broken["reason"]
    assert "super-secret" not in broken["reason"]


def test_player_prop_fixture_pairs_a_price_and_names_a_missing_book(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    payload = {
        "id": "event-1",
        "sport_key": "americanfootball_nfl",
        "commence_time": "2026-01-01T18:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_pass_tds",
                        "last_update": "2026-01-01T17:50:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example QB", "price": -120, "point": 1.5},
                            {"name": "Under", "description": "Example QB", "price": 100, "point": 1.5},
                            {"name": "Over", "description": "Example QB", "price": -120, "point": 1.5},
                        ],
                    }
                ],
            }
        ],
    }

    def transport(url, params=None, timeout=None):
        return _Response(200, payload, headers={"x-requests-remaining": "8", "x-requests-used": "1"})

    result = fetch_event_player_props(
        "americanfootball_nfl",
        "event-1",
        "player_pass_tds",
        bookmakers="draftkings,fanduel",
        transport=transport,
        sleep=lambda _seconds: None,
        as_of="2026-01-01T17:55:00+00:00",
    )
    assert result["ok"] is True
    assert result["requests_remaining"] == "8"
    assert result["duplicate_count"] == 1
    overs = [
        row
        for row in result["records"]
        if row.get("side") == "Over" and not row.get("duplicate")
    ]
    assert overs[0]["fair_market_probability"] is not None
    assert overs[0]["region_verified"] is False
    assert "state" in overs[0]["region_note"].lower()
    assert overs[0]["freshness"] == "fresh"
    assert result["books_not_returned"][0]["bookmaker_key"] == "fanduel"
    assert result["books_not_returned"][0]["reason"] == "not available from this source"


def test_stale_provider_timestamp_is_marked():
    payload = {
        "id": "event-1",
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "player_points",
                        "last_update": "2026-01-01T12:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example", "price": -110, "point": 20.5}
                        ],
                    }
                ],
            }
        ],
    }
    result = normalize_player_props(
        payload,
        requested_books=["fanduel"],
        retrieved_at="2026-01-01T18:00:00+00:00",
    )
    assert result["records"][0]["freshness"] == "stale"
    assert result["records"][0]["fair_market_probability"] is None



def test_fetch_sport_events_uses_metadata_endpoint_and_returns_quota(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    seen = {}

    def transport(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _Response(200, [{
            "id": "provider-event-1",
            "home_team": "Indiana Colts",
            "away_team": "Houston Texans",
            "commence_time": "2026-11-23T18:00:00Z",
        }], headers={"x-requests-remaining": "499", "x-requests-used": "1", "x-requests-last": "0"})

    result = fetch_sport_events(
        "americanfootball_nfl", transport=transport, sleep=lambda _seconds: None,
    )
    assert result["ok"] is True
    assert result["called"] is True
    assert result["events"][0]["id"] == "provider-event-1"
    assert result["requests_remaining"] == "499"
    assert result["requests_last"] == "0"
    assert seen["url"].endswith("/sports/americanfootball_nfl/events")
    assert seen["params"] == {"apiKey": "test-key"}
