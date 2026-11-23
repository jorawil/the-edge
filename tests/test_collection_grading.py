"""Collection and grading. Every prop, price, and result here is a fixture.

None of these numbers were collected from a sportsbook or a box score.
The tests do not open a socket.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from betting_model.archive import get_snapshot, list_snapshots
from betting_model.archive_analysis import analyze_archive
from betting_model.books import BOOKS
from betting_model.cli import main
from betting_model.collection import assemble_prices, collect_event_odds, collect_joined, collect_pregame
from betting_model.core import EDGE_THRESHOLD, SIGMA_DEFAULTS
from betting_model.grading import grade_archive
from betting_model.journal import Journal
from betting_model.odds import normalize_player_props
from betting_model.player_names import match_players
from betting_model.quality import quality_report, render_markdown
from betting_model.result_sources import EspnResultSource, interpret_status, load_result_fixture
from betting_model.schedule import load_schedule, run_once
from betting_model.version import MODEL_VERSION

NOW = datetime(2026, 11, 23, 17, 5, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
START = "2026-11-23T18:00:00Z"
PROJECTED_AT = "2026-11-23T17:00:00Z"
PRICE_AT = "2026-11-23T17:00:30Z"
FIXTURE = "unit-test fixture, not a real projection, price, or box score"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    state = {"moment": NOW}

    def _current():
        return state["moment"]

    monkeypatch.setattr("betting_model.archive._utc_now", _current)
    return state


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network was called")

    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    monkeypatch.setattr("urllib.request.urlopen", blocked)


def _prop(**overrides):
    prop = {
        "player": "Example Player",
        "player_id": "ex-1",
        "sport": "nfl",
        "event_id": "game-1",
        "event_start": START,
        "team": "HOM",
        "opponent": "AWY",
        "market": "passing_yards",
        "is_over": True,
        "line": 250.5,
        "odds": -110,
        "projected": 270.0,
        "projection_source": FIXTURE,
        "projection_at": PROJECTED_AT,
        "retrieved_at": PRICE_AT,
        "book": "draftkings",
    }
    prop.update(overrides)
    return prop


def _paths(tmp_path: Path):
    return tmp_path / "archive.sqlite", tmp_path / "journal.sqlite"


def _result_event(**overrides):
    event = {
        "event_id": "game-1",
        "status": "final",
        "start": START,
        "home": {"name": "Home Team", "abbr": "HOM"},
        "away": {"name": "Away Team", "abbr": "AWY"},
        "players": [
            {
                "name": "Example Player",
                "player_id": "ex-1",
                "team": "HOM",
                "dnp": False,
                "stats": {"PASS_YDS": 280},
            }
        ],
    }
    event.update(overrides)
    return event


def _grade(database, journal, events):
    document = {"kind": "result_fixture", "source_label": FIXTURE, "events": events}
    return grade_archive(database, load_result_fixture(document), journal_path=journal)


class _Response:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_safeguards_and_no_historical_odds_call():
    assert EDGE_THRESHOLD == 0.03
    assert [key for key, _label in BOOKS] == ["fanduel_in", "draftkings", "bet365", "fanatics"]
    assert SIGMA_DEFAULTS["PASS_YDS"] == 40.0
    source = Path("betting_model/collection.py").read_text(encoding="utf-8")
    assert "/historical/" not in source
    assert "place_bet" not in source


def test_valid_pregame_collection_keeps_the_supplied_record(tmp_path: Path):
    database, journal = _paths(tmp_path)
    summary = collect_pregame(database, [_prop()], journal_path=journal)
    assert summary["stored"] == 1
    assert summary["calibration_claimed"] is False
    assert summary["automatic_betting"] is False
    row = get_snapshot(database, summary["items"][0]["archive_id"])
    assert row["event_id"] == "game-1"
    assert row["event_start"] == START
    assert row["player_name"] == "Example Player"
    assert row["canonical_market"] == "PASS_YDS"
    assert row["side"] == "Over"
    assert row["line"] == 250.5
    assert row["sportsbook"] == "draftkings"
    assert row["american_odds"] == -110
    assert row["projected_value"] == 270.0
    assert row["projection_created_at"] == PROJECTED_AT
    assert row["odds_retrieved_at"] == PRICE_AT
    assert row["model_version"] == MODEL_VERSION
    assert row["qualified"] == 0
    stored = Journal(journal).events(action="stored")
    assert stored[0]["raw"]["player"] == "Example Player"
    assert stored[0]["raw"]["odds"] == -110


def test_missing_stale_and_post_event_records_are_rejected(tmp_path: Path):
    database, journal = _paths(tmp_path)
    summary = collect_pregame(
        database,
        [
            _prop(projected=None, event_id="missing"),
            _prop(event_id="started", event_start="2026-11-23T17:00:00Z"),
            _prop(event_id="leaked", actual=30),
            _prop(event_id="stale", freshness="stale", freshness_detail="provider last_update is old"),
            _prop(event_id="untimed", freshness="unverified"),
        ],
        journal_path=journal,
    )
    assert summary["stored"] == 0
    assert summary["ok"] is False
    assert list_snapshots(database) == []
    assert Journal(journal).runs("collect")[0]["status"] == "failed"
    reasons = " ".join(item["reason"] for item in summary["items"])
    assert "projected" in reasons
    assert "already started" in reasons
    assert summary["skipped"] == 1
    assert "postgame" in reasons
    assert "last_update" in reasons
    assert "could not be verified" in reasons


def test_duplicate_is_skipped_and_a_changed_price_is_kept(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    first = collect_pregame(database, [_prop()], journal_path=journal)
    again = collect_pregame(database, [_prop()], journal_path=journal)
    assert again["duplicates"] == 1
    assert again["stored"] == 0
    original = get_snapshot(database, first["items"][0]["archive_id"])
    clock["moment"] = NOW + timedelta(minutes=20)
    changed = collect_pregame(
        database,
        [_prop(line=255.5, odds=-115, retrieved_at="2026-11-23T17:20:00Z")],
        journal_path=journal,
    )
    assert changed["stored"] == 1
    assert len(list_snapshots(database)) == 2
    assert get_snapshot(database, first["items"][0]["archive_id"])["payload_json"] == original["payload_json"]
    assert get_snapshot(database, changed["items"][0]["archive_id"])["line"] == 255.5


def test_books_stay_distinct_and_unverified_fanduel_is_not_indiana(tmp_path: Path):
    database, journal = _paths(tmp_path)
    summary = collect_pregame(
        database,
        [
            _prop(book="draftkings", odds=-110),
            _prop(book="draftkings", odds=-110, is_over=False, projected=200),
            _prop(book="fanduel_in", odds=-105),
            _prop(
                book="fanduel_in",
                event_id="verified-game",
                player_id="ex-2",
                odds=-108,
                region="Indiana",
                region_source="copied from the Indiana FanDuel app",
                region_verified=True,
            ),
            _prop(book="bet365", event_id="typed-bet365", player_id="ex-3", odds=100),
            _prop(book="fanatics", event_id="typed-fanatics", player_id="ex-4", odds=-120),
        ],
        journal_path=journal,
    )
    assert summary["stored"] == 6
    rows = list_snapshots(database)
    draftkings = [row for row in rows if row["sportsbook"] == "draftkings"]
    assert len(draftkings) == 2
    assert all(row["no_vig_probability"] is not None for row in draftkings)
    generic = next(row for row in rows if row["sportsbook"] == "fanduel" and row["event_id"] == "game-1")
    assert generic["region_verified"] == 0
    assert generic["no_vig_probability"] is None
    verified = next(row for row in rows if row["event_id"] == "verified-game")
    assert verified["sportsbook"] == "fanduel_in"
    assert verified["region_verified"] == 1
    note = Journal(journal).events(action="stored")
    relabeled = [item for item in note if item["raw"].get("storage_note")]
    assert relabeled
    assert relabeled[0]["raw"]["submitted"]["book"] == "fanduel_in"
    assert next(row["sportsbook"] for row in rows if row["event_id"] == "typed-bet365") == "bet365"
    assert next(row["sportsbook"] for row in rows if row["event_id"] == "typed-fanatics") == "fanatics"


def test_joined_prices_keep_each_book_and_skip_a_duplicate_payload(tmp_path: Path):
    payload = {
        "id": "game-1",
        "sport_key": "americanfootball_nfl",
        "commence_time": START,
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_pass_yds",
                        "last_update": PRICE_AT,
                        "outcomes": [
                            {"name": "Over", "description": "Example Player", "price": -110, "point": 250.5},
                            {"name": "Over", "description": "Example Player", "price": -110, "point": 250.5},
                        ],
                    }
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "player_pass_yds",
                        "last_update": PRICE_AT,
                        "outcomes": [
                            {"name": "Over", "description": "Example Player", "price": -115, "point": 249.5},
                        ],
                    }
                ],
            },
        ],
    }
    prices = normalize_player_props(payload, retrieved_at=PRICE_AT)["records"]
    projection = _prop()
    projection.pop("line")
    projection.pop("odds")
    projection.pop("book")
    projection.pop("retrieved_at")
    database, journal = _paths(tmp_path)
    summary = collect_joined(database, [projection], prices, journal_path=journal)
    assert summary["stored"] == 2
    assert summary["duplicates"] == 1
    books = sorted(row["sportsbook"] for row in list_snapshots(database))
    assert books == ["draftkings", "fanduel"]
    assert all(row["region_verified"] == 0 for row in list_snapshots(database))
    assert "historical" not in json.dumps(summary)


def test_live_odds_does_not_request_bet365_or_invent_a_price(tmp_path: Path, monkeypatch):
    database, journal = _paths(tmp_path)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    missing = collect_event_odds(
        database,
        [_prop()],
        sport_key="americanfootball_nfl",
        event_id="game-1",
        markets="player_pass_yds",
        journal_path=journal,
        transport=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("called")),
    )
    assert missing["stored"] == 0
    assert missing["provider_failures"] == 1
    assert missing["called"] is False
    assert Journal(journal).runs("collect")[0]["status"] == "failed"
    assert list_snapshots(database) == []

    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    seen = {}

    def transport(url, params=None, timeout=None):
        seen["params"] = params
        return _Response(
            200,
            {
                "id": "game-1",
                "commence_time": START,
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "player_pass_yds",
                                "last_update": PRICE_AT,
                                "outcomes": [
                                    {"name": "Over", "description": "Example Player", "price": -110, "point": 250.5}
                                ],
                            }
                        ],
                    }
                ],
            },
        )

    projection = _prop()
    for key in ("line", "odds", "book", "retrieved_at"):
        projection.pop(key)
    found = collect_event_odds(
        database,
        [projection],
        sport_key="americanfootball_nfl",
        event_id="game-1",
        markets="player_pass_yds",
        bookmakers="bet365,draftkings,fanatics",
        journal_path=journal,
        transport=transport,
        sleep=lambda _seconds: None,
        as_of=PRICE_AT,
    )
    assert "bet365" not in seen["params"]["bookmakers"]
    assert seen["params"]["bookmakers"] == "draftkings,fanatics"
    assert found["stored"] == 1
    assert get_snapshot(database, found["items"][0]["archive_id"])["sportsbook"] == "draftkings"
    assert "test-key" not in json.dumps(found)


def test_a_price_that_matches_two_projections_is_not_assigned():
    prices = [
        {
            "event_id": "game-1",
            "player": "Example Player",
            "market": "player_pass_yds",
            "is_over": True,
            "line": 250.5,
            "american_odds": -110,
            "bookmaker_key": "draftkings",
            "retrieved_at": PRICE_AT,
            "freshness": "fresh",
            "supported_shape": True,
            "commence_time": START,
        }
    ]
    _props, extras = assemble_prices(
        [_prop(player="Example Player"), _prop(player="Example Player", player_id="ex-2")],
        prices,
    )
    assert _props == []
    assert any(item["action"] == "conflict" for item in extras)


def test_names_match_carefully_and_do_not_guess():
    players = [
        {"name": "Patrick Mahomes", "player_id": "15"},
        {"name": "Ken Griffey", "player_id": "1"},
        {"name": "Ken Griffey Jr.", "player_id": "2"},
        {"name": "A.J. Brown", "player_id": "3"},
    ]
    assert match_players("Patrick Mahomes II", None, players)["method"] == "name_without_suffix"
    assert match_players("P. Mahomes", None, players)["method"] == "first_initial_and_last_name"
    assert match_players("José Ramírez", None, [{"name": "Jose Ramirez"}])["ok"] is True
    assert match_players("Mahomes", None, players)["ok"] is False
    junior = match_players("Ken Griffey Jr", None, players)
    assert junior["ok"] is True and players[junior["index"]]["player_id"] == "2"
    assert match_players("Someone", "missing", players)["ok"] is False
    assert "name was not used" in match_players("Patrick Mahomes", "missing", players)["reason"]
    ambiguous = match_players("P. Mahomes", None, players + [{"name": "Preston Mahomes", "player_id": "9"}])
    assert ambiguous["ambiguous"] is True


def test_grade_win_push_zero_missing_and_leaves_the_snapshot(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    saved = collect_pregame(
        database,
        [
            _prop(),
            _prop(player="Zero Player", player_id="zero", market="points", line=0.5, projected=2, sport="nba"),
            _prop(
                player="Push Player",
                player_id="push",
                market="points",
                line=10,
                projected=12,
                sport="nba",
                event_id="game-2",
            ),
            _prop(player="No Assists", player_id="na", market="assists", line=4.5, projected=6, sport="nba"),
            _prop(
                player="Zero Player",
                player_id="zero",
                market="rebounds",
                line=0.5,
                projected=2,
                sport="nba",
                event_id="reb-game",
                team="HOM",
                opponent="REB",
            ),
        ],
        journal_path=journal,
    )
    assert saved["stored"] == 5
    before = get_snapshot(database, saved["items"][0]["archive_id"])["payload_json"]
    clock["moment"] = LATER
    graded = _grade(
        database,
        journal,
        [
            _result_event(
                players=[
                    {"name": "Example Player", "player_id": "ex-1", "team": "HOM", "stats": {"PASS_YDS": 280}},
                    {"name": "Zero Player", "player_id": "zero", "team": "HOM", "stats": {"PTS": 10, "REB": 0}},
                    {"name": "No Assists", "player_id": "na", "team": "HOM", "stats": {"PTS": 8, "REB": 3}},
                ]
            ),
            _result_event(
                event_id="game-2",
                players=[{"name": "Push Player", "player_id": "push", "team": "HOM", "stats": {"PTS": 10}}],
            ),
        ],
    )
    def one(event_id, player):
        found = [item for item in graded["items"] if item["event_id"] == event_id and item["player"] == player]
        assert len(found) == 1, graded["items"]
        return found[0]

    assert one("game-1", "Example Player")["settlement"] == "Win"
    assert one("game-1", "Zero Player")["settlement"] == "Win"
    zero = get_snapshot(database, one("game-1", "Zero Player")["archive_id"])
    assert one("game-2", "Push Player")["settlement"] == "Push"
    assert one("game-1", "No Assists")["action"] == "unresolved"
    assert "not treated as zero" in one("game-1", "No Assists")["reason"]
    assert get_snapshot(database, saved["items"][0]["archive_id"])["payload_json"] == before
    assert len(zero["results"]) == 1

    rebound_id = next(row["archive_id"] for row in list_snapshots(database) if row["event_id"] == "reb-game")
    reb_grade = _grade(
        database,
        journal,
        [
            _result_event(
                event_id="reb-game",
                team="HOM",
                opponent="REB",
                home={"name": "Home Team", "abbr": "HOM"},
                away={"name": "Rebound Opponent", "abbr": "REB"},
                players=[{"name": "Zero Player", "player_id": "zero", "team": "HOM", "stats": {"REB": 0, "PTS": 10}}],
            )
        ],
    )
    assert next(item for item in reb_grade["items"] if item["event_id"] == "reb-game")["settlement"] == "Loss"
    reb_row = get_snapshot(database, rebound_id)
    assert reb_row["results"][0]["actual_stat"] == 0
    assert reb_row["results"][0]["actual_missing"] == 0


def test_dnp_postponed_canceled_suspended_and_not_started(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    collect_pregame(
        database,
        [
            _prop(event_id="dnp", player_id="sit"),
            _prop(event_id="post", player_id="post"),
            _prop(event_id="cancel", player_id="cancel"),
            _prop(event_id="suspend", player_id="suspend"),
            _prop(event_id="future", player_id="ex-1"),
        ],
        journal_path=journal,
    )
    clock["moment"] = LATER
    graded = _grade(
        database,
        journal,
        [
            _result_event(
                event_id="dnp",
                players=[{"name": "Example Player", "player_id": "sit", "team": "HOM", "dnp": True, "stats": {}}],
            ),
            _result_event(event_id="post", status="postponed", players=[]),
            _result_event(event_id="cancel", status="canceled", players=[]),
            _result_event(event_id="suspend", status="suspended", players=[]),
            _result_event(event_id="future"),
        ],
    )
    by_event = {item["event_id"]: item for item in graded["items"]}
    assert by_event["dnp"]["settlement"] == "Void"
    assert by_event["post"]["settlement"] == "Void"
    assert by_event["cancel"]["settlement"] == "Void"
    assert by_event["suspend"]["action"] == "unresolved"
    assert by_event["future"]["settlement"] == "Win"
    dnp = next(row for row in list_snapshots(database) if row["event_id"] == "dnp")
    full = get_snapshot(database, dnp["archive_id"])
    assert full["results"][0]["actual_stat"] is None
    assert full["results"][0]["actual_missing"] == 1
    clock["moment"] = NOW
    early = collect_pregame(database, [_prop(event_id="early-final", player_id="early")], journal_path=journal)
    early_grade = _grade(database, journal, [_result_event(event_id="early-final", status="final")])
    early_item = next(item for item in early_grade["items"] if item["event_id"] == "early-final")
    assert early_item["action"] == "skipped"
    assert get_snapshot(database, early["items"][0]["archive_id"])["results"] == []
    postponed_early = collect_pregame(
        database, [_prop(event_id="early-post", player_id="early-post")], journal_path=journal
    )
    post_grade = _grade(database, journal, [_result_event(event_id="early-post", status="postponed", players=[])])
    post_item = next(item for item in post_grade["items"] if item["event_id"] == "early-post")
    assert post_item["action"] == "skipped"
    assert "scheduled start" in post_item["reason"]
    assert postponed_early["stored"] == 1
    assert get_snapshot(database, postponed_early["items"][0]["archive_id"])["results"] == []


def test_conflicts_are_not_chosen_and_a_win_is_not_replaced(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    saved = collect_pregame(
        database,
        [
            _prop(),
            _prop(event_id="teams", team="HOM", opponent="ZZZ"),
            _prop(event_id="kept", team="HOM", opponent="KPT"),
        ],
        journal_path=journal,
    )
    clock["moment"] = LATER
    graded = _grade(
        database,
        journal,
        [
            _result_event(
                players=[
                    {"name": "Example Player", "player_id": "ex-1", "team": "HOM", "stats": {"PASS_YDS": 280}},
                    {"name": "Example Player", "player_id": "ex-1", "team": "HOM", "stats": {"PASS_YDS": 10}},
                ]
            )
        ],
    )
    conflict = next(item for item in graded["items"] if item["event_id"] == "game-1")
    assert conflict["action"] == "conflict"
    game = next(row for row in list_snapshots(database) if row["event_id"] == "game-1")
    assert get_snapshot(database, game["archive_id"])["results"] == []
    assert saved["stored"] == 3

    other_id = next(row["archive_id"] for row in list_snapshots(database) if row["event_id"] == "teams")
    wrong_team = _grade(
        database,
        journal,
        [_result_event(event_id="teams", home={"name": "Other", "abbr": "OTH"}, away={"name": "Else", "abbr": "ELS"})],
    )
    teams = next(item for item in wrong_team["items"] if item["event_id"] == "teams")
    assert teams["action"] == "unresolved"
    assert get_snapshot(database, other_id)["results"] == []

    winner_id = next(row["archive_id"] for row in list_snapshots(database) if row["event_id"] == "kept")
    first = _grade(
        database,
        journal,
        [_result_event(event_id="kept", players=[{"name": "Example Player", "player_id": "ex-1", "team": "HOM", "stats": {"PASS_YDS": 300}}])],
    )
    kept_item = next(item for item in first["items"] if item["event_id"] == "kept")
    assert kept_item["settlement"] == "Win"
    _grade(
        database,
        journal,
        [_result_event(event_id="kept", players=[{"name": "Example Player", "player_id": "ex-1", "team": "HOM", "stats": {"PASS_YDS": 1}}])],
    )
    kept = get_snapshot(database, winner_id)
    assert len(kept["results"]) == 1
    assert kept["results"][0]["actual_stat"] == 300


def test_unknown_disagreement_is_kept_and_a_missing_unknown_can_be_superseded(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    clock["moment"] = NOW
    saved = collect_pregame(
        database,
        [
            _prop(event_id="odd", market="MIN", line=30, projected=32, player_id="odd", team="ODD", opponent="AWY"),
            _prop(event_id="blank", player_id="blank", team="BLK", opponent="AWY"),
        ],
        journal_path=journal,
    )
    odd_id = saved["items"][0]["archive_id"]
    blank_id = saved["items"][1]["archive_id"]
    clock["moment"] = LATER
    from betting_model.archive import attach_result

    attached = attach_result(
        database,
        odd_id,
        game_status="final",
        actual=30,
        actual_provided=True,
        result_source=FIXTURE,
        result_retrieved_at="2026-11-24T02:00:00Z",
    )
    assert attached["settlement"] == "Unknown"
    disagreed = _grade(
        database,
        journal,
        [
            _result_event(
                event_id="odd",
                home={"name": "Odd Team", "abbr": "ODD"},
                away={"name": "Away Team", "abbr": "AWY"},
                players=[{"name": "Example Player", "player_id": "odd", "team": "ODD", "stats": {"MIN": 31}}],
            )
        ],
    )
    assert next(item for item in disagreed["items"] if item["event_id"] == "odd")["action"] == "conflict"
    assert len(get_snapshot(database, odd_id)["results"]) == 1

    missing = attach_result(
        database,
        blank_id,
        game_status="final",
        actual_provided=False,
        result_source=FIXTURE,
        result_retrieved_at="2026-11-24T02:00:00Z",
    )
    assert missing["settlement"] == "Unknown"
    filled = _grade(
        database,
        journal,
        [
            _result_event(
                event_id="blank",
                home={"name": "Blank Team", "abbr": "BLK"},
                away={"name": "Away Team", "abbr": "AWY"},
                players=[{"name": "Example Player", "player_id": "blank", "team": "BLK", "stats": {"PASS_YDS": 280}}],
            )
        ],
    )
    assert next(item for item in filled["items"] if item["event_id"] == "blank")["settlement"] == "Win"
    rows = get_snapshot(database, blank_id)["results"]
    assert len(rows) == 2
    newer = [row for row in rows if row["supersedes_result_id"]]
    assert len(newer) == 1
    assert newer[0]["settlement"] == "Win"
    assert any(row["settlement"] == "Unknown" for row in rows)


def test_provider_failure_retries_without_a_duplicate_grade(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    collect_pregame(
        database,
        [_prop(event_id="flaky"), _prop(event_id="steady", player_id="steady")],
        journal_path=journal,
    )
    clock["moment"] = LATER

    class Flaky:
        def __init__(self):
            self.calls = {"flaky": 0}

        def fetch_event(self, snapshot):
            event_id = snapshot["event_id"]
            self.calls[event_id] = self.calls.get(event_id, 0) + 1
            if event_id == "flaky" and self.calls[event_id] == 1:
                return {"ok": False, "failure": True, "reason": "http_503", "source": FIXTURE}
            return load_result_fixture(
                {
                    "kind": "result_fixture",
                    "source_label": FIXTURE,
                    "events": [
                        _result_event(
                            event_id=event_id,
                            players=[
                                {
                                    "name": snapshot["player_name"],
                                    "player_id": snapshot["player_id"],
                                    "team": "HOM",
                                    "stats": {"PASS_YDS": 280},
                                }
                            ],
                        )
                    ],
                }
            ).fetch_event(snapshot)

    source = Flaky()
    first = grade_archive(database, source, journal_path=journal)
    actions = {item["event_id"]: item["action"] for item in first["items"]}
    assert actions["flaky"] == "provider_failure"
    assert actions["steady"] == "graded"
    second = grade_archive(database, source, journal_path=journal)
    assert {item["event_id"]: item["action"] for item in second["items"]} == {"flaky": "graded"}
    assert sum(len(get_snapshot(database, row["archive_id"])["results"]) for row in list_snapshots(database)) == 2
    third = grade_archive(database, source, journal_path=journal)
    assert third["processed"] == 0


def test_espn_parser_distinguishes_zero_dnp_and_status(tmp_path: Path, clock):
    assert interpret_status({"name": "STATUS_FINAL", "completed": True, "state": "post"}) == "final"
    assert interpret_status({"name": "STATUS_FINAL", "completed": False}) == "unknown"
    assert interpret_status({"name": "STATUS_POSTPONED", "completed": False}) == "postponed"
    assert interpret_status({"name": "STATUS_CANCELED", "completed": False}) == "canceled"
    assert interpret_status({"name": "STATUS_SUSPENDED", "completed": False}) == "suspended"
    assert interpret_status({"name": "STATUS_SOMETHING_ELSE", "completed": True}) == "unknown"

    summary = {
        "header": {
            "id": "555",
            "competitions": [
                {
                    "date": START,
                    "status": {"type": {"name": "STATUS_FINAL", "state": "post", "completed": True}},
                    "competitors": [
                        {"homeAway": "home", "team": {"abbreviation": "HOM", "displayName": "Home Team"}},
                        {"homeAway": "away", "team": {"abbreviation": "AWY", "displayName": "Away Team"}},
                    ],
                }
            ],
        },
        "boxscore": {
            "players": [
                {
                    "team": {"abbreviation": "HOM"},
                    "statistics": [
                        {
                            "name": None,
                            "keys": ["points", "rebounds", "assists", "threePointFieldGoalsMade-threePointFieldGoalsAttempted"],
                            "athletes": [
                                {
                                    "athlete": {"id": "ex-1", "displayName": "Example Player"},
                                    "stats": ["18", "0", "5", "0-4"],
                                },
                                {
                                    "athlete": {"id": "sit", "displayName": "Sat Out"},
                                    "didNotPlay": True,
                                    "stats": [],
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    }
    board = {
        "events": [
            {
                "id": "555",
                "date": START,
                "status": {"type": {"name": "STATUS_FINAL", "completed": True, "state": "post"}},
                "competitions": summary["header"]["competitions"],
            }
        ]
    }
    calls = []

    def transport(url, timeout=None):
        calls.append(url)
        if "summary?event=missing-id" in url:
            return _Response(404, {"message": "nope"})
        if "summary?event=555" in url:
            return _Response(200, summary)
        if "scoreboard" in url:
            return _Response(200, board)
        if "summary?event=429" in url:
            return _Response(429, {}, headers={"Retry-After": "1"})
        raise AssertionError(url)

    database, journal = _paths(tmp_path)
    collect_pregame(
        database,
        [
            _prop(event_id="555", market="rebounds", line=0.5, projected=3, sport="nba"),
            _prop(
                event_id="missing-id",
                team="Home Team",
                opponent="Away Team",
                market="threes",
                line=0.5,
                projected=2,
                sport="nba",
            ),
            _prop(event_id="429", sport="nba", market="points", line=10.5, projected=16),
        ],
        journal_path=journal,
    )
    clock["moment"] = LATER
    sleeps = []
    source = EspnResultSource(transport=transport, sleep=sleeps.append, delay=0, attempts=2)
    graded = grade_archive(database, source, journal_path=journal)
    by_event = {item["event_id"]: item for item in graded["items"]}
    assert by_event["555"]["settlement"] == "Loss"
    row = get_snapshot(database, by_event["555"]["archive_id"])
    assert row["results"][0]["actual_stat"] == 0
    assert "espn" in row["results"][0]["result_source"]
    assert FIXTURE not in row["results"][0]["result_source"]
    assert by_event["missing-id"]["settlement"] == "Loss"
    assert any("scoreboard" in url for url in calls)
    assert by_event["429"]["action"] == "provider_failure"

    def limited(url, timeout=None):
        if not hasattr(limited, "n"):
            limited.n = 0
        limited.n += 1
        if limited.n == 1:
            return _Response(429, {}, headers={"Retry-After": "1"})
        return _Response(200, summary | {"header": {**summary["header"], "id": "429"}})

    retried = EspnResultSource(transport=limited, sleep=sleeps.append, delay=0, attempts=3)
    # The summary id is rewritten above, but the nested competition is shared.
    # Ask for event 429 and accept the direct summary.
    outcome = grade_archive(database, retried, journal_path=journal)
    assert outcome["provider_failures"] == 0
    assert sleeps


def test_espn_football_slash_and_missing_combo(tmp_path: Path, clock):
    payload = {
        "header": {
            "id": "nfl-1",
            "competitions": [
                {
                    "date": START,
                    "status": {"type": {"name": "STATUS_FINAL", "completed": True, "state": "post"}},
                    "competitors": [
                        {"homeAway": "home", "team": {"abbreviation": "HOM", "displayName": "Home"}},
                        {"homeAway": "away", "team": {"abbreviation": "AWY", "displayName": "Away"}},
                    ],
                }
            ],
        },
        "boxscore": {
            "players": [
                {
                    "team": {"abbreviation": "HOM"},
                    "statistics": [
                        {
                            "name": "passing",
                            "keys": ["completions/passingAttempts", "passingYards", "passingTouchdowns"],
                            "athletes": [
                                {"athlete": {"id": "qb", "displayName": "Example QB"}, "stats": ["12/18", "129", "0"]}
                            ],
                        },
                        {
                            "name": "rushing",
                            "keys": ["rushingAttempts", "rushingYards"],
                            "athletes": [
                                {"athlete": {"id": "qb", "displayName": "Example QB"}, "stats": ["2", "4"]}
                            ],
                        },
                    ],
                }
            ]
        },
    }

    def transport(url, timeout=None):
        return _Response(200, payload)

    database, journal = _paths(tmp_path)
    collect_pregame(
        database,
        [
            _prop(event_id="nfl-1", player="Example QB", player_id="qb", market="passing_touchdowns", line=0.5, projected=1.2),
            _prop(event_id="nfl-1", player="Example QB", player_id="qb", market="rushing_receiving_yards", line=10.5, projected=20),
        ],
        journal_path=journal,
    )
    clock["moment"] = LATER
    graded = grade_archive(database, EspnResultSource(transport=transport, sleep=lambda _s: None, delay=0), journal_path=journal)
    by_market = {item["market"]: item for item in graded["items"]}
    assert by_market["PASS_TDS"]["settlement"] == "Loss"
    touchdowns = next(row for row in list_snapshots(database, event_id="nfl-1") if row["canonical_market"] == "PASS_TDS")
    assert get_snapshot(database, touchdowns["archive_id"])["results"][0]["actual_stat"] == 0
    assert by_market["RUSH_REC_YDS"]["action"] == "unresolved"


def test_repeated_runs_stay_append_only_and_analyze_still_works(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    saved = collect_pregame(database, [_prop(), _prop(book="bet365", odds=-120, event_id="other")], journal_path=journal)
    clock["moment"] = LATER
    _grade(database, journal, [_result_event(), _result_event(event_id="other")])
    _grade(database, journal, [_result_event(), _result_event(event_id="other")])
    conn = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE snapshots SET line = 0")
    conn.close()
    report = analyze_archive(database)
    assert report["ok"] is True
    assert report["calibration_claimed"] is False
    assert report["profit_reported"] is False
    assert report["roi_reported"] is False
    assert saved["items"][0]["archive_id"] in json.dumps(report)
    quality = quality_report(database, journal)
    assert quality["accuracy_claimed"] is False
    assert quality["calibration_claimed"] is False
    assert quality["profit_reported"] is False
    assert quality["archive"]["snapshots"] == 2
    assert quality["archive"]["by_sport"]["nfl"] == 2
    assert quality["archive"]["by_sportsbook"]["draftkings"] == 1
    assert quality["archive"]["by_model_version"][MODEL_VERSION] == 2
    assert quality["journal"]["stored"] == 2
    assert quality["journal"]["graded"] == 2
    assert quality["journal"]["duplicates_skipped"] == 0
    assert quality["journal"]["last_successful_collection_at"]
    assert quality["journal"]["last_successful_grading_at"]
    text = render_markdown(quality)
    assert "betting edge" in text
    assert "ROI" in text
    missing = quality_report(tmp_path / "absent.sqlite", tmp_path / "no-journal.sqlite")
    assert missing["journal"]["rejected"] is None
    assert "did not create" in missing["archive"]["note"]
    assert not (tmp_path / "absent.sqlite").exists()


def test_schedule_is_optional_and_retries_a_provider_failure(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    props = tmp_path / "props.json"
    props.write_text(json.dumps([_prop()]), encoding="utf-8")
    config_path = tmp_path / "schedule.json"
    config_path.write_text(
        json.dumps({"enabled": False, "props": str(props), "min_minutes_between_runs": 60}),
        encoding="utf-8",
    )
    assert run_once(load_schedule(config_path), database=database, journal_path=journal)["ran"] is False
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "props": str(props),
                "grade": True,
                "min_minutes_between_runs": 60,
                "api_key": "nope",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="API keys"):
        load_schedule(config_path)
    config_path.write_text(
        json.dumps({"enabled": True, "props": str(props), "grade": True, "min_minutes_between_runs": 60}),
        encoding="utf-8",
    )
    config = load_schedule(config_path)

    class Boom:
        def fetch_event(self, _snapshot):
            return {"ok": False, "failure": True, "reason": "http_503", "source": FIXTURE}

    class Ok:
        def fetch_event(self, snapshot):
            return load_result_fixture(
                {"kind": "result_fixture", "source_label": FIXTURE, "events": [_result_event()]}
            ).fetch_event(snapshot)

    first = run_once(config, database=database, journal_path=journal, grade_source=Boom())
    assert first["collect"]["stored"] == 1
    assert first["grade"]["skipped"] == 1
    clock["moment"] = LATER
    failed_then_retry = run_once(config, database=database, journal_path=journal, grade_source=Boom())
    assert failed_then_retry["grade"]["provider_failures"] == 1
    clock["moment"] = LATER + timedelta(minutes=10)
    second = run_once(config, database=database, journal_path=journal, grade_source=Ok())
    assert second["collect"].get("skipped") is True
    assert second["grade"]["graded"] == 1
    third = run_once(config, database=database, journal_path=journal, grade_source=Ok())
    assert third["grade"].get("skipped") is True or third["grade"].get("processed") == 0
    assert len(get_snapshot(database, first["collect"]["items"][0]["archive_id"])["results"]) == 1


def test_cli_collect_grade_and_quality_use_fixtures(tmp_path: Path, monkeypatch, clock):
    monkeypatch.chdir(tmp_path)
    props = tmp_path / "props.json"
    props.write_text(json.dumps([_prop()]), encoding="utf-8")
    database = tmp_path / "archive.sqlite"
    journal = tmp_path / "journal.sqlite"
    code = main(
        ["archive", "collect", "--props", str(props), "--db", str(database), "--journal", str(journal)]
    )
    assert code == 0
    clock["moment"] = LATER
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps({"kind": "result_fixture", "source_label": FIXTURE, "events": [_result_event()]}),
        encoding="utf-8",
    )
    assert main(
        [
            "archive",
            "grade",
            "--db",
            str(database),
            "--journal",
            str(journal),
            "--results-fixture",
            str(results),
        ]
    ) == 0
    assert main(["archive", "quality", "--db", str(database), "--journal", str(journal), "--format", "json"]) == 0
    assert main(["archive", "analyze", "--db", str(database), "--format", "json"]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"events": []}), encoding="utf-8")
    assert main(["archive", "grade", "--db", str(database), "--results-fixture", str(bad)]) == 2


def test_two_current_results_are_not_silently_reduced(tmp_path: Path, clock):
    database, journal = _paths(tmp_path)
    saved = collect_pregame(database, [_prop()], journal_path=journal)
    archive_id = saved["items"][0]["archive_id"]
    clock["moment"] = LATER
    conn = sqlite3.connect(database)
    for result_id in ("r1", "r2"):
        conn.execute(
            """
            INSERT INTO results (
                result_id, archive_id, attached_at, actual_stat, actual_missing, settlement,
                settlement_reason, game_status, result_source, result_retrieved_at,
                result_retrieved_at_source, supersedes_result_id
            ) VALUES (?, ?, ?, NULL, 1, 'Unknown', ?, 'final', ?, ?, ?, NULL)
            """,
            (
                result_id,
                archive_id,
                "2026-11-24T02:00:00Z",
                FIXTURE,
                FIXTURE,
                "2026-11-24T02:00:00Z",
                "2026-11-24T02:00:00Z",
            ),
        )
    conn.commit()
    conn.close()
    graded = _grade(database, journal, [_result_event()])
    assert graded["items"][0]["action"] == "conflict"
    assert len(get_snapshot(database, archive_id)["results"]) == 2


def test_partial_collection_is_not_recorded_as_a_success(tmp_path: Path):
    database, journal = _paths(tmp_path)
    summary = collect_pregame(
        database,
        [_prop(), _prop(projected=None, event_id="missing-projection")],
        journal_path=journal,
    )
    assert summary["stored"] == 1
    assert summary["rejected"] == 1
    assert summary["ok"] is False
    run = Journal(journal).runs("collect")[0]
    assert run["status"] == "partial"
    assert Journal(journal).last_success("collect") is None


def test_collect_cli_returns_nonzero_when_every_prop_is_rejected(tmp_path: Path):
    props = tmp_path / "props.json"
    props.write_text(json.dumps([_prop(projected=None)]), encoding="utf-8")
    code = main([
        "archive", "collect",
        "--props", str(props),
        "--db", str(tmp_path / "archive.sqlite"),
        "--journal", str(tmp_path / "journal.sqlite"),
    ])
    assert code == 2


def test_grading_with_unresolved_rows_is_partial_not_success(tmp_path: Path):
    database, journal = _paths(tmp_path)
    collect_pregame(database, [_prop()], journal_path=journal)

    class MissingPlayer:
        def fetch_event(self, _snapshot):
            return {
                "ok": True, "status": "final", "event_id": "game-1",
                "source": FIXTURE, "matched_by": "event_id",
                "players": [],
            }

    result = grade_archive(database, MissingPlayer(), journal_path=journal)
    assert result["ok"] is False
    assert result["unresolved"] == 1
    assert Journal(journal).runs("grade")[0]["status"] == "failed"


def test_missing_archive_is_recorded_as_failed_grade_run(tmp_path: Path):
    database = tmp_path / "missing.sqlite"
    journal = tmp_path / "journal.sqlite"

    class Noop:
        def fetch_event(self, _snapshot):
            raise AssertionError("source must not be called without an archive")

    result = grade_archive(database, Noop(), journal_path=journal)
    assert result["ok"] is False
    assert Journal(journal).runs("grade")[0]["status"] == "failed"

