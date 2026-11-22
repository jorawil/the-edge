"""A recent average is not a projection. Qualification is stricter than positive_edge."""

from datetime import datetime, timezone

from betting_model.assessment import assess
from betting_model.pairing import attach_opposites
from betting_model.sports import evaluate_prop

AS_OF = datetime(2026, 1, 1, 18, 10, tzinfo=timezone.utc)
WHEN = "2026-01-01T18:00:00+00:00"


def _priced(**overrides):
    prop = {
        "player": "Example Player",
        "player_id": "ex-1",
        "event_id": "game-1",
        "sport": "nfl",
        "market": "passing_yards",
        "line": 200.5,
        "odds": -110,
        "is_over": True,
        "projected": 280.0,
        "projection_source": "desk note, example only",
        "projection_at": WHEN,
        "retrieved_at": WHEN,
        "book": "draftkings",
    }
    prop.update(overrides)
    return prop


def test_complete_fresh_prop_can_qualify_and_says_sigma_is_not_calibrated():
    row = evaluate_prop(_priced(), as_of=AS_OF)
    assert row["status"] == "SCREEN_PASS_UNCALIBRATED"
    assert row["qualified"] is False
    assert "not QUALIFIED" in " ".join(row["status_reasons"])
    assert row["projection_kind"] == "manual"
    assert row["projection"]["provenance_complete"] is True
    assert row["projection"]["model_projection"] is None
    assert row["projection"]["season_average"] is None
    assert row["sigma_calibrated"] is False
    assert "not calibrated" in row["explanation"] or "uncalibrated" in row["explanation"]
    assert "vig" in row["explanation"]
    assert row["fair_market_probability"] is None


def test_qualified_requires_a_calibrated_sigma_and_distribution():
    prop = _priced()
    row = evaluate_prop(prop, as_of=AS_OF)
    row["sigma_calibrated"] = True
    row["distribution_calibrated"] = True
    graded = assess(prop, row, as_of=AS_OF)
    assert graded["status"] == "QUALIFIED"
    assert graded["qualified"] is True


def test_missing_timestamp_is_not_qualified_even_when_the_old_flag_is_true():
    row = evaluate_prop(_priced(projection_at=None, retrieved_at=None), as_of=AS_OF)
    assert row["positive_edge"] is True
    assert row["status"] == "WATCH"
    assert row["qualified"] is False
    assert row["data_quality"] == "uncertain"


def test_stale_price_is_not_a_current_opportunity():
    row = evaluate_prop(
        _priced(retrieved_at="2026-01-01T12:00:00+00:00"),
        as_of=AS_OF,
    )
    assert row["freshness"] == "stale"
    assert row["status"] != "QUALIFIED"


def test_uncertain_role_is_not_qualified():
    row = evaluate_prop(_priced(role_uncertain=True), as_of=AS_OF)
    assert row["status"] == "WATCH"
    assert "role" in " ".join(row["status_reasons"])


def test_recent_average_is_not_a_forecast(monkeypatch):
    def fake(name, n=10):
        return {
            "ok": True,
            "player": name,
            "player_id": 7,
            "season": "2025-26",
            "games": 10,
            "PTS": 22.0,
            "REB": 5.0,
            "AST": 4.0,
            "PR": 27.0,
            "PRA": 31.0,
            "PA": 26.0,
            "MIN": 30.0,
        }

    monkeypatch.setattr("betting_model.sports.get_nba_player_recent_avg", fake)
    row = evaluate_prop(
        {
            "player": "Example Player",
            "sport": "nba",
            "market": "PTS",
            "line": 19.5,
            "odds": -110,
            "is_over": True,
            "retrieved_at": WHEN,
            "projection_at": WHEN,
            "projection_source": "should not be used",
        },
        as_of=AS_OF,
    )
    assert row["evaluated"] is True
    assert row["projection_kind"] == "recent_average"
    assert row["illustrative_only"] is True
    assert row["status"] == "NOT_EVALUABLE"
    assert row["qualified"] is False
    assert "not a prediction" in row["explanation"]
    assert "Season average: unavailable" in row["projection"]["explanation"]
    assert row["projection"]["model_version"] == "0.2.0"


def test_supplied_projection_keeps_a_separate_recent_average():
    row = evaluate_prop(_priced(recent_average=210.0), as_of=AS_OF)
    assert row["projection_kind"] == "manual"
    assert row["projection"]["recent_average"] == 210.0
    assert row["projected"] == 280.0


def test_pairs_only_the_same_book_line_and_close_timestamps():
    over = _priced(is_over=True, odds=-120)
    under = _priced(is_over=False, odds=100, projected=180.0)
    paired = attach_opposites([over, under])
    assert paired[0]["fair_pair"] == "paired"
    row = evaluate_prop(paired[0], as_of=AS_OF)
    assert row["fair_market_probability"] is not None
    assert row["threshold_edge_metric"] == "edge_vs_fair_market"

    other_book = _priced(is_over=False, odds=100, book="fanatics")
    unpaired = attach_opposites([_priced(), other_book])
    assert "opposite" not in unpaired[0]

    no_book = _priced()
    no_book.pop("book")
    assert "opposite" not in attach_opposites([no_book, under])[0]


def test_historical_rush_sample_is_not_qualified():
    import json
    from pathlib import Path

    props = json.loads((Path(__file__).resolve().parents[1] / "props_rush_w11_sat.json").read_text())
    for prop in props:
        row = evaluate_prop(prop)
        assert row["status"] != "QUALIFIED"
        assert row["projection_kind"] == "manual"
