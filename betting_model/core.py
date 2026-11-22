"""Core probability and edge math. Probability-first only; never places bets.

Legacy fields keep their original meaning:

- ``implied_prob`` and ``raw_implied_probability`` are the vig-inclusive
  price. They are not a fair probability.
- ``model_prob`` is P(side) from Normal(projection, sigma), with Under as
  the complement. That formula ignores pushes.
- ``edge`` and ``edge_vs_raw_implied`` are ``model_prob - implied_prob``.
- ``positive_edge`` is true only when that legacy edge is greater than the
  threshold. Exactly 0.03 does not pass. The default threshold is 0.03,
  meaning 3 percentage points, not 3 percent profit.

New fields (``model_probability``, ``fair_market_probability``,
``edge_vs_fair_market``, ``expected_value_per_unit``) are omitted instead
of filled with a guess when the extra information is missing.
"""

from __future__ import annotations

import math
from typing import Any

from .distributions import describe_settlement
from .timeutil import parse_aware_datetime, seconds_apart

# Default residual sigma by market family (document for desks).
# These are uncalibrated starting points, not measured forecast errors.
# PTS/PR-ish ≈ 6; REB/AST ≈ 3; rush/rec yards ≈ 25; pass yards ≈ 40; PRA ≈ 7.
SIGMA_DEFAULTS: dict[str, float] = {
    "PTS": 6.0,
    "PR": 6.0,
    "PRA": 7.0,
    "PA": 6.0,  # points+assists-ish
    "REB": 3.0,
    "AST": 3.0,
    "MIN": 5.0,
    "RUSH_YDS": 25.0,
    "REC_YDS": 25.0,
    "RUSH_REC_YDS": 30.0,
    "PASS_YDS": 40.0,
    "DEFAULT": 6.0,
}

EDGE_THRESHOLD = 0.03  # 3 percentage points — positive_edge True only above this
MAX_AMERICAN_ODDS = 100_000
DEFAULT_MAX_SIDE_SKEW_SECONDS = 120.0

# Aliases used by resolve_sigma. Do not point a count market at a yards sigma.
_SIGMA_ALIASES = {
    "POINTS": "PTS",
    "REBOUNDS": "REB",
    "ASSISTS": "AST",
    "POINTS_REBOUNDS": "PR",
    "PTS_REB": "PR",
    "POINTS_REBOUNDS_ASSISTS": "PRA",
    "PTS_REB_AST": "PRA",
    "POINTS_ASSISTS": "PA",
    "PTS_AST": "PA",
    "RUSHING_YARDS": "RUSH_YDS",
    "RUSH_YARDS": "RUSH_YDS",
    "RUSH": "RUSH_YDS",
    "RUSH_REC": "RUSH_REC_YDS",
    "RUSHING_RECEIVING_YARDS": "RUSH_REC_YDS",
    "RR": "RUSH_REC_YDS",
    "RECEIVING_YARDS": "REC_YDS",
    # Odds-API style keys: without these, player_receiving_yards fell to DEFAULT sigma 6.
    "PLAYER_RECEIVING_YARDS": "REC_YDS",
    "PLAYER_RECEPTION_YDS": "REC_YDS",
    "PLAYER_RUSHING_YARDS": "RUSH_YDS",
    "PLAYER_RUSH_YDS": "RUSH_YDS",
    "PLAYER_PASSING_YARDS": "PASS_YDS",
    "PLAYER_PASS_YDS": "PASS_YDS",
    "REC_YARDS": "REC_YDS",
    "REC": "REC_YDS",
    "PASSING_YARDS": "PASS_YDS",
    "PASS_YARDS": "PASS_YDS",
    "PASS": "PASS_YDS",
}

# Names that need a canonical key for push rules but must NOT gain a new sigma.
# Giving them a sigma would change historical edges. They keep DEFAULT sigma.
_CANONICAL_ONLY = {
    "COMPLETIONS": "COMPLETIONS",
    "PASS_COMPLETIONS": "COMPLETIONS",
    "PLAYER_PASS_COMPLETIONS": "COMPLETIONS",
    "PASS_ATTEMPTS": "PASS_ATT",
    "PLAYER_PASS_ATTEMPTS": "PASS_ATT",
    "RECEPTIONS": "RECEPTIONS",
    "PLAYER_RECEPTIONS": "RECEPTIONS",
    "RUSH_ATTEMPTS": "RUSH_ATT",
    "PLAYER_RUSH_ATTEMPTS": "RUSH_ATT",
    "PASS_TDS": "PASS_TDS",
    "PASSING_TOUCHDOWNS": "PASS_TDS",
    "PLAYER_PASS_TDS": "PASS_TDS",
    "RUSH_TDS": "RUSH_TDS",
    "RUSHING_TOUCHDOWNS": "RUSH_TDS",
    "REC_TDS": "REC_TDS",
    "RECEIVING_TOUCHDOWNS": "REC_TDS",
    "THREES": "THREES",
    "3PM": "THREES",
    "PLAYER_THREES": "THREES",
    "PLAYER_POINTS": "PTS",
    "PLAYER_REBOUNDS": "REB",
    "PLAYER_ASSISTS": "AST",
    "PLAYER_POINTS_REBOUNDS_ASSISTS": "PRA",
    "PLAYER_POINTS_REBOUNDS": "PR",
    "PLAYER_POINTS_ASSISTS": "PA",
}


def _market_key(market: str | None) -> str | None:
    if not market:
        return None
    return market.strip().upper().replace(" ", "_").replace("+", "_").replace("-", "_")


def canonical_market(market: str | None) -> str | None:
    """Stable market key. Does not by itself choose a sigma."""
    key = _market_key(market)
    if key is None:
        return None
    if key in _SIGMA_ALIASES:
        return _SIGMA_ALIASES[key]
    if key in _CANONICAL_ONLY:
        return _CANONICAL_ONLY[key]
    return key


def parse_finite_number(value: Any, label: str) -> float:
    """A real finite number. Booleans and NaN are rejected."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a number, got {value!r}")
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"{label} must be a number, got {value!r}") from exc
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ValueError(f"{label} must be a number, got {value!r}")
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return number


def parse_american_odds(value: Any) -> int:
    """American odds as a whole number from -100000..-100 or 100..100000.

    Zero and prices inside -99..99 are not American odds this program supports.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"american odds must be a number, got {value!r}")
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.lower() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity"}:
            raise ValueError(f"american odds must be a finite number, got {value!r}")
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"american odds must be a number, got {value!r}") from exc
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ValueError(f"american odds must be a number, got {value!r}")
    if not math.isfinite(number) or not float(number).is_integer():
        raise ValueError(f"american odds must be a whole number, got {value!r}")
    odds = int(number)
    if odds == 0 or abs(odds) < 100 or abs(odds) > MAX_AMERICAN_ODDS:
        raise ValueError(
            f"american odds {odds} are outside the supported range "
            f"({-MAX_AMERICAN_ODDS} to -100, or 100 to {MAX_AMERICAN_ODDS})"
        )
    return odds


def parse_side(value: Any) -> bool:
    """True for Over, False for Under.

    The string ``"false"`` is Under. ``bool("false")`` is True in Python,
    so that conversion is not used.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        key = value.strip().lower()
        if key in {"over", "o", "yes", "true", "1"}:
            return True
        if key in {"under", "u", "no", "false", "0"}:
            return False
    raise ValueError(
        f"invalid Over/Under value {value!r}. Use true/false or over/under."
    )


def implied_probability(american_odds: int) -> float:
    """Convert American odds to vig-inclusive implied probability in (0, 1).

    Negative odds: |odds| / (|odds| + 100)
    Positive odds: 100 / (odds + 100)
    """
    odds = parse_american_odds(american_odds)
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def profit_per_unit(american_odds: int) -> float:
    """Profit on a winning 1-unit stake. The stake itself is not included."""
    odds = parse_american_odds(american_odds)
    if odds > 0:
        return odds / 100.0
    return 100.0 / abs(odds)


def expected_value_per_unit(
    win_probability: float,
    loss_probability: float,
    push_probability: float,
    american_odds: int,
) -> float:
    """Expected profit per 1 unit staked.

    A push returns the stake, so it adds 0. This is not a guaranteed profit.
    """
    for label, value in (
        ("win probability", win_probability),
        ("loss probability", loss_probability),
        ("push probability", push_probability),
    ):
        number = parse_finite_number(value, label)
        if number < 0.0 or number > 1.0:
            raise ValueError(f"{label} must be between 0 and 1")
    total = win_probability + loss_probability + push_probability
    if abs(total - 1.0) > 1e-6:
        raise ValueError("win, loss, and push probabilities must sum to 1")
    return win_probability * profit_per_unit(american_odds) - loss_probability


def clears_threshold(edge: float, threshold: float | None = None) -> bool:
    """True only when edge is strictly greater than the threshold.

    Exactly 0.03 does not clear the default 3-point threshold.
    """
    thr = EDGE_THRESHOLD if threshold is None else parse_finite_number(threshold, "threshold")
    if thr < 0 or thr >= 1:
        raise ValueError("threshold must be between 0 and 1, not including 1")
    return edge > thr


def resolve_sigma(market: str | None = None, sigma: float | None = None) -> float:
    """Pick sigma from an explicit override, a documented market, or DEFAULT.

    An explicit sigma must be a positive finite number. Documented defaults
    are not calibrated forecast errors.
    """
    if sigma is not None:
        value = parse_finite_number(sigma, "sigma")
        if value <= 0:
            raise ValueError("sigma must be positive")
        return value
    canon = canonical_market(market)
    if canon is None:
        return SIGMA_DEFAULTS["DEFAULT"]
    return float(SIGMA_DEFAULTS.get(canon, SIGMA_DEFAULTS["DEFAULT"]))


def explain_sigma(market: str | None = None, sigma: float | None = None) -> dict[str, Any]:
    """Sigma plus where it came from. ``sigma_calibrated`` stays false."""
    value = resolve_sigma(market, sigma)
    canon = canonical_market(market)
    if sigma is not None:
        source = "explicit"
    elif canon in SIGMA_DEFAULTS and canon != "DEFAULT":
        source = "documented_default"
    else:
        source = "unverified_default"
    return {
        "sigma": value,
        "sigma_source": source,
        "sigma_calibrated": False,
        "canonical_market": canon,
    }


def _round6(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _fair_side(
    *,
    odds: int,
    line: float,
    raw_implied: float,
    opposite_odds: Any,
    opposite_line: Any,
    price_retrieved_at: str | None,
    opposite_retrieved_at: str | None,
    max_side_skew_seconds: float,
) -> tuple[float | None, str | None, dict[str, Any]]:
    """No-vig probability of this side, or (None, reason, extras)."""
    if opposite_odds is None:
        return None, "the other side of this price was not provided", {}
    try:
        other = parse_american_odds(opposite_odds)
    except ValueError as exc:
        return None, f"the other side's odds are not usable ({exc})", {}
    if opposite_line is not None:
        other_line = parse_finite_number(opposite_line, "opposite line")
        if abs(other_line - line) > 1e-6:
            return None, "the other side is a different line", {}
    own_ts = parse_aware_datetime(price_retrieved_at)
    other_ts = parse_aware_datetime(opposite_retrieved_at)
    if own_ts is None or other_ts is None:
        return (
            None,
            "no-vig probability needs a timezone-aware timestamp on both sides",
            {},
        )
    skew = seconds_apart(own_ts, other_ts)
    if skew > max_side_skew_seconds:
        return (
            None,
            (
                f"the two prices are {skew:.0f} seconds apart, past the "
                f"{max_side_skew_seconds:.0f}-second limit"
            ),
            {},
        )
    other_implied = implied_probability(other)
    total = raw_implied + other_implied
    if total <= 0:
        return None, "the two implied probabilities could not be normalized", {}
    fair = raw_implied / total
    return fair, None, {
        "opposite_odds": other,
        "overround": _round6(total - 1.0),
        "price_skew_seconds": round(skew, 3),
    }


def fair_probability_for_side(
    *,
    odds: int,
    line: float,
    opposite_odds: Any = None,
    opposite_line: Any = None,
    price_retrieved_at: str | None = None,
    opposite_retrieved_at: str | None = None,
    max_side_skew_seconds: float = DEFAULT_MAX_SIDE_SKEW_SECONDS,
) -> tuple[float | None, str | None, dict[str, Any]]:
    """No-vig probability of ``odds``, or a reason it was left blank."""
    odds_value = parse_american_odds(odds)
    line_value = parse_finite_number(line, "line")
    return _fair_side(
        odds=odds_value,
        line=line_value,
        raw_implied=implied_probability(odds_value),
        opposite_odds=opposite_odds,
        opposite_line=opposite_line,
        price_retrieved_at=price_retrieved_at,
        opposite_retrieved_at=opposite_retrieved_at,
        max_side_skew_seconds=max_side_skew_seconds,
    )


def calculate_edge(
    projected: float,
    line: float,
    odds: int,
    is_over: bool,
    *,
    market: str | None = None,
    sigma: float | None = None,
    threshold: float | None = None,
    opposite_odds: Any = None,
    opposite_line: Any = None,
    price_retrieved_at: str | None = None,
    opposite_retrieved_at: str | None = None,
    max_side_skew_seconds: float = DEFAULT_MAX_SIDE_SKEW_SECONDS,
) -> dict[str, Any]:
    """Edge of a normal model against the sportsbook price.

    ``edge`` is model probability minus the raw, vig-inclusive implied
    probability. It is not profit, ROI, or a fair-market edge.
    """
    projected_value = parse_finite_number(projected, "projected")
    line_value = parse_finite_number(line, "line")
    odds_value = parse_american_odds(odds)
    side_over = parse_side(is_over)
    sigma_info = explain_sigma(market, sigma)
    thr = EDGE_THRESHOLD if threshold is None else parse_finite_number(threshold, "threshold")
    if thr < 0 or thr >= 1:
        raise ValueError("threshold must be between 0 and 1, not including 1")

    raw_implied = implied_probability(odds_value)
    settlement = describe_settlement(
        projected=projected_value,
        line=line_value,
        sigma=sigma_info["sigma"],
        is_over=side_over,
        canonical_market=sigma_info["canonical_market"],
    )
    legacy_model = float(settlement["legacy_model_prob"])
    legacy_edge = legacy_model - raw_implied
    push_handled = settlement["push_handling"] != "unsupported"
    win = settlement["win_probability"] if push_handled else None
    loss = settlement["loss_probability"] if push_handled else None
    push = settlement["push_probability"] if push_handled else None

    fair, fair_reason, fair_extra = _fair_side(
        odds=odds_value,
        line=line_value,
        raw_implied=raw_implied,
        opposite_odds=opposite_odds,
        opposite_line=opposite_line,
        price_retrieved_at=price_retrieved_at,
        opposite_retrieved_at=opposite_retrieved_at,
        max_side_skew_seconds=max_side_skew_seconds,
    )

    # The sportsbook price is the chance of winning given that the bet does not
    # push. Compare that price with win / (win + loss). Expected value still
    # uses the unconditional win, loss, and push chances.
    conditional = None
    if win is not None and loss is not None and (float(win) + float(loss)) > 0:
        conditional = float(win) / (float(win) + float(loss))
    settlement_edge = None if conditional is None else conditional - raw_implied
    fair_edge = None if fair is None or conditional is None else conditional - fair

    ev = None
    ev_reason = None
    if win is None or loss is None or push is None:
        ev_reason = "expected value was omitted because win/loss/push is not defined for this market and line"
    else:
        ev = expected_value_per_unit(float(win), float(loss), float(push), odds_value)

    if fair_edge is not None:
        threshold_edge = fair_edge
        threshold_metric = "edge_vs_fair_market"
    elif settlement_edge is not None:
        threshold_edge = settlement_edge
        threshold_metric = (
            "edge_vs_raw_implied"
            if settlement["push_handling"] == "no_push_half_point"
            else "settlement_edge_vs_raw_implied"
        )
    else:
        threshold_edge = None
        threshold_metric = None

    return {
        "projected": projected_value,
        "line": line_value,
        "odds": odds_value,
        "is_over": side_over,
        "implied_prob": _round6(raw_implied),
        "model_prob": _round6(legacy_model),
        "edge": _round6(legacy_edge),
        "positive_edge": clears_threshold(legacy_edge, thr),
        "sigma": sigma_info["sigma"],
        "market": market,
        "edge_threshold": thr,
        "canonical_market": sigma_info["canonical_market"],
        "sigma_source": sigma_info["sigma_source"],
        "sigma_calibrated": False,
        "distribution": settlement["distribution"],
        "distribution_calibrated": False,
        "line_kind": settlement["line_kind"],
        "push_handling": settlement["push_handling"],
        "push_reason": settlement.get("push_reason"),
        "model_probability": _round6(win),
        "model_probability_ignores_push": settlement["model_probability_ignores_push"],
        "win_probability": _round6(win),
        "loss_probability": _round6(loss),
        "push_probability": _round6(push),
        "conditional_win_probability": _round6(conditional),
        "raw_implied_probability": _round6(raw_implied),
        "edge_vs_raw_implied": _round6(legacy_edge),
        "settlement_edge_vs_raw_implied": _round6(settlement_edge),
        "fair_market_probability": _round6(fair),
        "edge_vs_fair_market": _round6(fair_edge),
        "fair_market_reason": fair_reason,
        "overround": fair_extra.get("overround"),
        "opposite_odds": fair_extra.get("opposite_odds"),
        "price_skew_seconds": fair_extra.get("price_skew_seconds"),
        "expected_value_per_unit": _round6(ev),
        "ev_reason": ev_reason,
        "threshold_edge": _round6(threshold_edge),
        "threshold_edge_metric": threshold_metric,
        "threshold_cleared": (
            clears_threshold(threshold_edge, thr) if threshold_edge is not None else False
        ),
        "positive_edge_basis": "legacy model_prob minus vig-inclusive implied probability",
    }
