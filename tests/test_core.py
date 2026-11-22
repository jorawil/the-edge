
import pytest

from betting_model.core import (
    implied_probability,
    calculate_edge,
    resolve_sigma,
    EDGE_THRESHOLD,
)


def test_negative_american_odds():
    assert implied_probability(-110) == pytest.approx(110 / 210)


def test_positive_american_odds():
    assert implied_probability(150) == pytest.approx(100 / 250)


def test_zero_odds_rejected():
    with pytest.raises(ValueError):
        implied_probability(0)


def test_over_probability():
    result = calculate_edge(
        projected=20,
        line=19.5,
        odds=-110,
        is_over=True,
        sigma=5,
    )
    assert 0 <= result["model_prob"] <= 1
    assert result["model_prob"] > 0.5


def test_under_probability():
    result = calculate_edge(
        projected=20,
        line=19.5,
        odds=-110,
        is_over=False,
        sigma=5,
    )
    assert 0 <= result["model_prob"] <= 1
    assert result["model_prob"] < 0.5


def test_over_and_under_probabilities_complement():
    over = calculate_edge(20, 19.5, -110, True, sigma=5)
    under = calculate_edge(20, 19.5, -110, False, sigma=5)

    assert over["model_prob"] + under["model_prob"] == pytest.approx(1.0)


def test_zero_sigma_rejected():
    with pytest.raises(ValueError):
        calculate_edge(20, 19.5, -110, True, sigma=0)


def test_sigma_default():
    assert resolve_sigma("PTS", None) == 6.0


def test_edge_threshold():
    assert EDGE_THRESHOLD == pytest.approx(0.03)


def test_edge_calculation():
    result = calculate_edge(
        projected=25,
        line=20.5,
        odds=100,
        is_over=True,
        sigma=5,
    )

    assert result["edge"] == pytest.approx(
        result["model_prob"] - result["implied_prob"]
    )
