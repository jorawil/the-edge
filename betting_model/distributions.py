"""How a prop settles.

The only probability model in use is the existing normal curve. It is a
fallback, not a claim that normal is the best shape for every stat.

Whole-number lines on integer stats (points, yards, receptions, and so on)
can push. A continuous normal curve gives that exact number probability zero,
which treats a push as a win or a loss. For those lines this module reports
win, loss, and push with a continuity correction. That correction is still
the normal curve. It has not been calibrated on forecast errors.

Half-point lines cannot push for an integer stat. Their win probability is
the same number the original model produced, so old edges stay comparable.
"""

from __future__ import annotations

import math


# Stats that are counted in whole numbers in a box score.
# Minutes are left out on purpose: they are not always whole numbers.
INTEGER_COUNT_MARKETS = frozenset(
    {
        "PTS",
        "REB",
        "AST",
        "PR",
        "PRA",
        "PA",
        "THREES",
        "RUSH_YDS",
        "REC_YDS",
        "RUSH_REC_YDS",
        "PASS_YDS",
        "RECEPTIONS",
        "COMPLETIONS",
        "PASS_ATT",
        "RUSH_ATT",
        "PASS_TDS",
        "RUSH_TDS",
        "REC_TDS",
    }
)

DISTRIBUTION_NAME = "normal"


def line_kind(line: float) -> str:
    """Return 'whole', 'half', or 'other'."""
    if abs(line - round(line)) <= 1e-9:
        return "whole"
    if abs((line * 2) - round(line * 2)) <= 1e-9:
        return "half"
    return "other"


def _norm_cdf(z: float) -> float:
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _clamp_prob(value: float) -> float:
    if value < 0.0 and value > -1e-12:
        return 0.0
    if value > 1.0 and value < 1.0 + 1e-12:
        return 1.0
    return value


def continuous_over_probability(projected: float, line: float, sigma: float) -> float:
    """P(stat > line) under Normal(projected, sigma). Original formula."""
    if sigma <= 0 or not math.isfinite(sigma):
        raise ValueError("sigma must be a positive finite number")
    z = (line - projected) / sigma
    return float(1.0 - _norm_cdf(z))


def describe_settlement(
    *,
    projected: float,
    line: float,
    sigma: float,
    is_over: bool,
    canonical_market: str | None,
) -> dict[str, float | str | None | bool]:
    """Win, loss, and push for the selected side.

    `model_prob` is always the original continuous probability so older
    reports stay comparable. `win_probability` is set only when push rules
    for this market and line are defined.
    """
    p_over = continuous_over_probability(projected, line, sigma)
    legacy = p_over if is_over else (1.0 - p_over)
    kind = line_kind(line)
    market = canonical_market or ""
    integer_market = market in INTEGER_COUNT_MARKETS

    base = {
        "distribution": DISTRIBUTION_NAME,
        "distribution_calibrated": False,
        "line_kind": kind,
        "legacy_model_prob": legacy,
        "win_probability": None,
        "loss_probability": None,
        "push_probability": None,
        "push_handling": "unsupported",
        "model_probability_ignores_push": True,
    }

    if not integer_market:
        base["push_reason"] = (
            "push rules are not defined for this market, so win/loss/push "
            "was not calculated"
        )
        return base

    if kind == "half":
        base.update(
            {
                "win_probability": legacy,
                "loss_probability": 1.0 - legacy,
                "push_probability": 0.0,
                "push_handling": "no_push_half_point",
                "model_probability_ignores_push": False,
                "push_reason": "a half-point line cannot push for an integer stat",
            }
        )
        return base

    if kind != "whole":
        base["push_reason"] = (
            "this line is neither a whole number nor a half point, so "
            "settlement was not guessed"
        )
        return base

    # Continuity correction: the stat lands on `line` when the normal
    # curve falls between line-0.5 and line+0.5.
    z_hi = (line + 0.5 - projected) / sigma
    z_lo = (line - 0.5 - projected) / sigma
    p_push = _clamp_prob(_norm_cdf(z_hi) - _norm_cdf(z_lo))
    p_over_win = _clamp_prob(1.0 - _norm_cdf(z_hi))
    p_under_win = _clamp_prob(_norm_cdf(z_lo))
    if is_over:
        win, loss = p_over_win, p_under_win
    else:
        win, loss = p_under_win, p_over_win
    base.update(
        {
            "win_probability": win,
            "loss_probability": loss,
            "push_probability": p_push,
            "push_handling": "continuity_correction",
            "model_probability_ignores_push": False,
            "push_reason": (
                "whole-number line: push probability is the normal curve "
                "between line-0.5 and line+0.5. This is not a calibrated "
                "forecast-error distribution."
            ),
        }
    )
    return base
