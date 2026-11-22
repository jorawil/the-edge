"""Parlays are not approved by multiplying leg probabilities."""

import json
from datetime import datetime, timezone
from pathlib import Path

from betting_model.cli import main
from betting_model.parlays import evaluate_parlay

AS_OF = datetime(2026, 1, 1, 18, 10, tzinfo=timezone.utc)
WHEN = "2026-01-01T18:00:00+00:00"


def _leg(player, market, event_id="game-1", projected=280):
    return {
        "player": player,
        "sport": "nfl",
        "market": market,
        "event_id": event_id,
        "line": 200.5,
        "odds": -110,
        "is_over": True,
        "projected": projected,
        "projection_source": "example only",
        "projection_at": WHEN,
        "retrieved_at": WHEN,
        "book": "draftkings",
    }


def test_same_game_parlay_has_no_joint_probability_and_is_not_approved():
    result = evaluate_parlay(
        [
            _leg("Quarterback Example", "passing_yards"),
            _leg("Receiver Example", "receiving_yards"),
        ],
        quoted_american_odds=264,
        quoted_book="draftkings",
        as_of=AS_OF,
    )
    assert result["model_approved"] is False
    assert result["correlation"] == "correlation_not_modeled"
    assert result["joint_model_probability"] is None
    assert result["independent_product_shown"] is False
    assert result["status"] == "NOT_EVALUABLE"
    assert result["quoted_payout"]["american_odds"] == 264
    assert result["expected_value_per_unit"] is None
    assert any("PASS_YDS" in note and "REC_YDS" in note for note in result["warnings"])


def test_missing_quote_is_not_invented_and_a_bad_leg_does_not_crash():
    result = evaluate_parlay(
        [
            _leg("One", "rushing_yards", event_id="game-9", projected=10),
            {"player": "Two", "sport": "nfl", "odds": "nope"},
        ],
        as_of=AS_OF,
    )
    assert result["quoted_payout"]["american_odds"] is None
    assert "invented" in result["quoted_payout"]["note"]
    assert result["model_approved"] is False
    assert "NOT_EVALUABLE" in result["leg_statuses"]


def test_cli_parlay_writes_the_warning(tmp_path: Path):
    ticket = tmp_path / "ticket.json"
    ticket.write_text(
        json.dumps(
            {
                "quoted_american_odds": 250,
                "quoted_book": "draftkings",
                "legs": [
                    _leg("One", "rushing_yards", event_id="a"),
                    _leg("Two", "receiving_yards", event_id="b"),
                ],
            }
        ),
        encoding="utf-8",
    )
    code = main(["parlay", "--ticket", str(ticket), "--out", str(tmp_path / "out")])
    assert code == 0
    saved = json.loads((tmp_path / "out" / "parlay_latest.json").read_text(encoding="utf-8"))
    assert saved["joint_model_probability"] is None
    assert saved["model_approved"] is False
