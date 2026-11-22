"""Core probability and edge math. Probability-first only; never places bets."""

from __future__ import annotations

from typing import Any

import math

import numpy as np

# Default residual sigma by market family (document for desks).
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

EDGE_THRESHOLD = 0.03  # 3% — positive_edge True only above this


def implied_probability(american_odds: int) -> float:
    """Convert American odds to implied probability in [0, 1].

    Negative odds: |odds| / (|odds| + 100)
    Positive odds: 100 / (odds + 100)
    """
    odds = int(american_odds)
    if odds == 0:
        raise ValueError("american_odds cannot be 0")
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _norm_cdf(z: float) -> float:
    """Standard normal CDF via numpy erf (no scipy required)."""
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _model_prob_over(projected: float, line: float, sigma: float) -> float:
    """P(stat > line) under Normal(projected, sigma)."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    z = (line - projected) / sigma
    return float(1.0 - _norm_cdf(z))


def resolve_sigma(market: str | None = None, sigma: float | None = None) -> float:
    """Pick sigma from explicit override, market key, or DEFAULT."""
    if sigma is not None:
        return float(sigma)
    if not market:
        return SIGMA_DEFAULTS["DEFAULT"]
    key = market.strip().upper().replace(" ", "_").replace("+", "_")
    aliases = {
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
        # Odds-API style keys: without these, player_receiving_yards fell to DEFAULT sigma 6 (old bug).
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
    key = aliases.get(key, key)
    return float(SIGMA_DEFAULTS.get(key, SIGMA_DEFAULTS["DEFAULT"]))


def calculate_edge(
    projected: float,
    line: float,
    odds: int,
    is_over: bool,
    *,
    market: str | None = None,
    sigma: float | None = None,
) -> dict[str, Any]:
    """Rough edge: model_prob vs vig-implied.

    model_prob uses Normal(projected, sigma) approximation.
    residual = projected - line; sigma defaults documented in SIGMA_DEFAULTS.
    edge = model_prob - implied_prob
    positive_edge = edge > 0.03 (3%)
    """
    implied = implied_probability(odds)
    sig = resolve_sigma(market, sigma)
    p_over = _model_prob_over(float(projected), float(line), sig)
    model_prob = p_over if is_over else (1.0 - p_over)
    edge = model_prob - implied
    return {
        "projected": float(projected),
        "line": float(line),
        "odds": int(odds),
        "is_over": bool(is_over),
        "implied_prob": round(implied, 6),
        "model_prob": round(model_prob, 6),
        "edge": round(edge, 6),
        "positive_edge": bool(edge > EDGE_THRESHOLD),
        "sigma": sig,
        "market": market,
        "edge_threshold": EDGE_THRESHOLD,
    }
