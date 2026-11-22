"""Probability math: vig, pushes, threshold, and expected value.

Numbers here are fixtures for the formulas. They are not sportsbook prices
and not player projections.
"""

import math

import pytest

from betting_model.core import (
    EDGE_THRESHOLD,
    calculate_edge,
    clears_threshold,
    expected_value_per_unit,
    implied_probability,
    profit_per_unit,
    resolve_sigma,
)


WHEN = "2026-01-01T18:00:00+00:00"


def test_negative_and_positive_odds_match_the_formula():
    assert implied_probability(-110) == pytest.approx(110 / 210)
    assert implied_probability("+150") == pytest.approx(100 / 250)
    assert implied_probability(-100) == pytest.approx(0.5)
    assert implied_probability(100) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "bad",
    [0, 50, -50, 99, -99, 100_001, True, False, None, "nope", float("nan"), float("inf"), "-110.5"],
)
def test_invalid_odds_are_rejected(bad):
    with pytest.raises(ValueError):
        implied_probability(bad)


def test_probabilities_stay_inside_zero_and_one():
    result = calculate_edge(25, 20.5, -110, True, market="PTS")
    assert 0.0 <= result["model_prob"] <= 1.0
    assert 0.0 <= result["raw_implied_probability"] <= 1.0
    assert result["model_probability"] == result["model_prob"]


def test_half_point_over_and_under_still_sum_to_one():
    over = calculate_edge(20, 19.5, -110, True, sigma=5, market="PTS")
    under = calculate_edge(20, 19.5, -110, False, sigma=5, market="PTS")
    assert over["model_prob"] + under["model_prob"] == pytest.approx(1.0)
    assert over["push_probability"] == 0.0
    assert over["push_handling"] == "no_push_half_point"
    assert over["edge"] == over["edge_vs_raw_implied"]
    assert over["raw_implied_probability"] == over["implied_prob"]
    assert over["conditional_win_probability"] == pytest.approx(over["model_probability"])


def test_whole_number_line_reports_a_push_without_changing_legacy_edge():
    over = calculate_edge(20, 20, -110, True, sigma=5, market="PTS")
    under = calculate_edge(20, 20, -110, False, sigma=5, market="PTS")
    assert over["model_prob"] + under["model_prob"] == pytest.approx(1.0)
    assert over["push_probability"] > 0
    win = over["win_probability"] + over["loss_probability"] + over["push_probability"]
    assert win == pytest.approx(1.0)
    assert over["model_probability"] < over["model_prob"]
    assert over["model_probability_ignores_push"] is False
    # The old field still ignores the push, and it is labeled that way.
    assert over["edge"] == pytest.approx(over["model_prob"] - over["implied_prob"])
    conditional = over["win_probability"] / (over["win_probability"] + over["loss_probability"])
    assert over["conditional_win_probability"] == pytest.approx(conditional, abs=1e-6)
    assert over["settlement_edge_vs_raw_implied"] == pytest.approx(
        over["conditional_win_probability"] - over["raw_implied_probability"],
        abs=1e-6,
    )
    assert over["settlement_edge_vs_raw_implied"] != pytest.approx(
        over["win_probability"] - over["raw_implied_probability"]
    )
    profit = 100 / 110
    assert over["expected_value_per_unit"] == pytest.approx(
        over["win_probability"] * profit - over["loss_probability"],
        abs=1e-6,
    )


def test_unknown_market_on_a_whole_line_does_not_invent_a_push():
    result = calculate_edge(10, 10, -110, True, sigma=3, market="fantasy_points")
    assert result["push_handling"] == "unsupported"
    assert result["model_probability"] is None
    assert result["expected_value_per_unit"] is None
    assert result["model_prob"] > 0


def test_zero_and_negative_and_nan_sigma_are_rejected():
    with pytest.raises(ValueError):
        calculate_edge(20, 19.5, -110, True, sigma=0)
    with pytest.raises(ValueError):
        calculate_edge(20, 19.5, -110, True, sigma=-1)
    with pytest.raises(ValueError):
        calculate_edge(20, 19.5, -110, True, sigma=float("nan"))
    with pytest.raises(ValueError):
        calculate_edge(float("nan"), 19.5, -110, True, sigma=5)
    with pytest.raises(ValueError):
        calculate_edge(20, float("inf"), -110, True, sigma=5)


def test_sigma_defaults_are_unchanged_and_labeled_uncalibrated():
    assert resolve_sigma("PTS", None) == 6.0
    assert resolve_sigma("player_receiving_yards") == 25.0
    assert resolve_sigma("REC") == 25.0
    # Receptions must not inherit the yards sigma. They stay on the old default.
    receptions = calculate_edge(5, 4.5, -110, True, market="receptions")
    assert receptions["sigma"] == 6.0
    assert receptions["sigma_source"] == "unverified_default"
    assert receptions["sigma_calibrated"] is False
    points = calculate_edge(20, 19.5, -110, True, market="PTS")
    assert points["sigma_source"] == "documented_default"


def test_threshold_is_strictly_greater_than_three_points():
    assert EDGE_THRESHOLD == pytest.approx(0.03)
    assert clears_threshold(0.029999) is False
    assert clears_threshold(0.03) is False
    assert clears_threshold(0.030001) is True
    below = calculate_edge(19.5, 19.5, -110, True, sigma=5, market="PTS")
    assert below["model_prob"] == pytest.approx(0.5, abs=0.01)
    assert below["positive_edge"] is False
    above = calculate_edge(40, 19.5, 100, True, sigma=5, market="PTS")
    assert above["positive_edge"] is True
    custom = calculate_edge(40, 19.5, 100, True, sigma=5, market="PTS", threshold=0.99)
    assert custom["edge_threshold"] == pytest.approx(0.99)
    assert custom["positive_edge"] is False


def test_legacy_edge_matches_model_minus_raw_implied():
    result = calculate_edge(25, 20.5, 100, True, sigma=5, market="PTS")
    assert result["edge"] == pytest.approx(result["model_prob"] - result["implied_prob"])
    assert result["positive_edge_basis"].startswith("legacy")


def test_string_false_is_the_under():
    under = calculate_edge(20, 19.5, -110, "false", sigma=5, market="PTS")
    over = calculate_edge(20, 19.5, -110, "true", sigma=5, market="PTS")
    assert under["is_over"] is False
    assert over["is_over"] is True
    assert under["model_prob"] < 0.5 < over["model_prob"]


def test_no_vig_probability_needs_both_sides_the_same_line_and_a_close_time():
    matched = calculate_edge(
        22,
        19.5,
        -110,
        True,
        sigma=5,
        market="PTS",
        opposite_odds=-110,
        opposite_line=19.5,
        price_retrieved_at=WHEN,
        opposite_retrieved_at="2026-01-01T18:00:30+00:00",
    )
    assert matched["fair_market_probability"] == pytest.approx(0.5)
    assert matched["overround"] == pytest.approx((110 / 210) * 2 - 1, abs=1e-6)
    assert matched["threshold_edge_metric"] == "edge_vs_fair_market"
    assert matched["edge_vs_fair_market"] > matched["edge_vs_raw_implied"]

    missing = calculate_edge(22, 19.5, -110, True, sigma=5, market="PTS")
    assert missing["fair_market_probability"] is None
    assert "other side" in missing["fair_market_reason"]

    mismatched = calculate_edge(
        22,
        19.5,
        -110,
        True,
        sigma=5,
        market="PTS",
        opposite_odds=-110,
        opposite_line=21.5,
        price_retrieved_at=WHEN,
        opposite_retrieved_at=WHEN,
    )
    assert mismatched["fair_market_probability"] is None
    assert "different line" in mismatched["fair_market_reason"]

    stale_pair = calculate_edge(
        22,
        19.5,
        -110,
        True,
        sigma=5,
        market="PTS",
        opposite_odds=-110,
        opposite_line=19.5,
        price_retrieved_at=WHEN,
        opposite_retrieved_at="2026-01-01T18:10:00+00:00",
    )
    assert stale_pair["fair_market_probability"] is None
    assert "seconds apart" in stale_pair["fair_market_reason"]

    no_clock = calculate_edge(
        22,
        19.5,
        -110,
        True,
        sigma=5,
        market="PTS",
        opposite_odds=-110,
        opposite_line=19.5,
    )
    assert no_clock["fair_market_probability"] is None
    assert "timestamp" in no_clock["fair_market_reason"]


def test_expected_value_is_zero_at_the_raw_price_and_negative_after_vig():
    odds = -110
    raw = implied_probability(odds)
    # A win probability equal to the vig-inclusive price has EV of about 0
    # before rounding. That is not a fair 50/50.
    assert expected_value_per_unit(raw, 1 - raw, 0.0, odds) == pytest.approx(0.0, abs=1e-12)
    vig_ev = expected_value_per_unit(0.5, 0.5, 0.0, odds)
    assert vig_ev == pytest.approx(0.5 * (100 / 110) - 0.5)
    assert vig_ev < 0
    plus = expected_value_per_unit(0.6, 0.4, 0.0, 150)
    assert plus == pytest.approx(0.6 * profit_per_unit(150) - 0.4)

    half = calculate_edge(22, 19.5, -110, True, sigma=5, market="PTS")
    assert half["expected_value_per_unit"] is not None
    # Push stake is returned, so it does not add or subtract.
    with_push = expected_value_per_unit(0.4, 0.4, 0.2, 100)
    assert with_push == pytest.approx(0.0)


def test_expected_value_rejects_probabilities_outside_zero_one():
    with pytest.raises(ValueError):
        expected_value_per_unit(1.2, -0.2, 0.0, -110)
    with pytest.raises(ValueError):
        expected_value_per_unit(0.6, 0.6, 0.0, -110)


def test_normal_cdf_tails_stay_in_range():
    huge = calculate_edge(100, 0.5, -110, True, sigma=1, market="PTS")
    tiny = calculate_edge(0, 100.5, -110, True, sigma=1, market="PTS")
    assert huge["model_prob"] == pytest.approx(1.0, abs=1e-6)
    assert tiny["model_prob"] == pytest.approx(0.0, abs=1e-6)
    assert math.isfinite(huge["edge"])
