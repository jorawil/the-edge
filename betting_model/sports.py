"""Sport-specific prop edge helpers. Reuse calculate_edge; NBA-first for auto-proj."""

from __future__ import annotations

from typing import Any

from .books import BOOK_KEYS, BOOK_LABELS, normalize_book
from .core import (
    EDGE_THRESHOLD,
    calculate_edge,
    clears_threshold,
    expected_value_per_unit,
    explain_sigma,
    fair_probability_for_side,
    implied_probability,
    parse_american_odds,
    parse_finite_number,
    parse_side,
)
from .assessment import apply_assessment
from .nba import get_nba_player_recent_avg, map_market_to_avg
from .validation import price_errors, prop_label


def _opposite_kwargs(prop: dict[str, Any]) -> dict[str, Any]:
    opposite = prop.get("opposite")
    kwargs: dict[str, Any] = {}
    if prop.get("retrieved_at"):
        kwargs["price_retrieved_at"] = prop.get("retrieved_at")
    if prop.get("threshold") is not None:
        kwargs["threshold"] = prop.get("threshold")
    if isinstance(opposite, dict):
        kwargs["opposite_odds"] = opposite.get("odds")
        kwargs["opposite_line"] = opposite.get("line", prop.get("line"))
        kwargs["opposite_retrieved_at"] = opposite.get("retrieved_at")
    return kwargs


def _edge_from_prop(prop: dict[str, Any], projected: float) -> dict[str, Any]:
    """Honor eval_mode=binary_ml (win_prob - implied) and optional sigma."""
    mode = str(prop.get("eval_mode") or "").strip().lower()
    is_over = parse_side(prop["is_over"])
    market = str(prop.get("market") or "")
    sigma = prop.get("sigma")
    extra = _opposite_kwargs(prop)
    if mode == "binary_ml":
        return _binary_edge(prop, float(projected), is_over=is_over, extra=extra)
    kw: dict[str, Any] = {"market": market, **extra}
    if sigma is not None:
        kw["sigma"] = sigma
    return calculate_edge(float(projected), prop["line"], prop["odds"], is_over, **kw)


def _binary_edge(
    prop: dict[str, Any],
    projected: float,
    *,
    is_over: bool,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """A supplied win probability. Assumes no push, and says so."""
    supplied = parse_finite_number(projected, "projected")
    win_p = supplied
    if win_p > 1.0:
        if win_p > 100.0:
            raise ValueError(
                f"{prop_label(prop)}: binary win probability {supplied} is above 100"
            )
        win_p = win_p / 100.0
    if win_p < 0.0 or win_p > 1.0:
        raise ValueError(
            f"{prop_label(prop)}: binary win probability must be between 0 and 1"
        )
    odds = parse_american_odds(prop["odds"])
    line = parse_finite_number(prop["line"], "line")
    threshold = extra.get("threshold", EDGE_THRESHOLD)
    implied = implied_probability(odds)
    edge = win_p - implied
    fair, fair_reason, fair_extra = fair_probability_for_side(
        odds=odds,
        line=line,
        opposite_odds=extra.get("opposite_odds"),
        opposite_line=extra.get("opposite_line"),
        price_retrieved_at=extra.get("price_retrieved_at"),
        opposite_retrieved_at=extra.get("opposite_retrieved_at"),
    )
    fair_edge = None if fair is None else win_p - fair
    ev = expected_value_per_unit(win_p, 1.0 - win_p, 0.0, odds)
    if fair_edge is not None:
        threshold_edge, threshold_metric = fair_edge, "edge_vs_fair_market"
    else:
        threshold_edge, threshold_metric = edge, "edge_vs_raw_implied"
    sigma_info = explain_sigma(str(prop.get("market") or ""), None)
    shown = supplied if supplied <= 1.0 else win_p
    return {
        "projected": shown,
        "line": line,
        "odds": odds,
        "is_over": is_over,
        "implied_prob": round(implied, 6),
        "model_prob": round(win_p, 6),
        "edge": round(edge, 6),
        "positive_edge": clears_threshold(edge, threshold),
        "sigma": None,
        "market": prop.get("market"),
        "edge_threshold": threshold,
        "eval_mode": "binary_ml",
        "canonical_market": sigma_info["canonical_market"],
        "sigma_source": "not_used",
        "sigma_calibrated": False,
        "distribution": "supplied_win_probability",
        "distribution_calibrated": False,
        "line_kind": None,
        "push_handling": "binary_no_push",
        "push_reason": "binary mode uses the supplied win probability and assumes no push",
        "model_probability": round(win_p, 6),
        "model_probability_ignores_push": False,
        "win_probability": round(win_p, 6),
        "loss_probability": round(1.0 - win_p, 6),
        "push_probability": 0.0,
        "raw_implied_probability": round(implied, 6),
        "edge_vs_raw_implied": round(edge, 6),
        "settlement_edge_vs_raw_implied": round(edge, 6),
        "fair_market_probability": None if fair is None else round(fair, 6),
        "edge_vs_fair_market": None if fair_edge is None else round(fair_edge, 6),
        "fair_market_reason": fair_reason,
        "overround": fair_extra.get("overround"),
        "opposite_odds": fair_extra.get("opposite_odds"),
        "price_skew_seconds": fair_extra.get("price_skew_seconds"),
        "expected_value_per_unit": round(ev, 6),
        "ev_reason": None,
        "threshold_edge": round(threshold_edge, 6),
        "threshold_edge_metric": threshold_metric,
        "threshold_cleared": clears_threshold(threshold_edge, threshold),
        "positive_edge_basis": "legacy model_prob minus vig-inclusive implied probability",
    }


def _base_result(
    prop: dict[str, Any],
    edge: dict[str, Any] | None,
    *,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    out = {
        "player": prop.get("player"),
        "sport": prop.get("sport"),
        "market": prop.get("market"),
        "line": prop.get("line"),
        "odds": prop.get("odds"),
        "is_over": prop.get("is_over"),
        "source": prop.get("source"),
        "notes": prop.get("notes"),
        "retrieved_at": prop.get("retrieved_at"),
        "evaluated": edge is not None,
        "skipped": edge is None,
    }
    if edge:
        out.update(edge)
    out.update(extra)
    return apply_assessment(prop, out, file_meta=file_meta, as_of=as_of)


def _rejected(
    prop: dict[str, Any],
    errors: list[str],
    *,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    return _base_result(
        prop,
        None,
        file_meta=file_meta,
        as_of=as_of,
        skip_reason="; ".join(errors),
    )


def evaluate_nba_prop(
    prop: dict[str, Any],
    *,
    n: int = 10,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Evaluate one NBA prop; auto-fill projected from recent avg if missing.

    A filled recent average is labeled as a recent average and is not a forecast.
    """
    errors = price_errors(prop)
    if errors:
        return _rejected(prop, errors, file_meta=file_meta, as_of=as_of)
    projected = prop.get("projected")
    proj_source = "manual" if projected is not None else None
    avg_info = None
    if projected is None:
        avg_info = get_nba_player_recent_avg(str(prop.get("player") or ""), n=n)
        if avg_info.get("ok"):
            mapped = map_market_to_avg(avg_info, str(prop.get("market") or ""))
            if mapped is not None:
                projected = mapped
                proj_source = f"nba_api_l{avg_info.get('games')}_{avg_info.get('season')}"
            else:
                return _base_result(
                    prop,
                    None,
                    file_meta=file_meta,
                    as_of=as_of,
                    skip_reason="market not mappable to recent avg",
                    recent_avg=avg_info,
                )
        else:
            return _base_result(
                prop,
                None,
                file_meta=file_meta,
                as_of=as_of,
                skip_reason="no projected and nba_api failed",
                recent_avg=avg_info,
            )

    edge = _edge_from_prop(prop, float(projected))
    return _base_result(
        prop,
        edge,
        file_meta=file_meta,
        as_of=as_of,
        projected=float(projected),
        proj_source=proj_source,
        recent_avg=avg_info,
    )


def evaluate_wnba_prop(
    prop: dict[str, Any],
    *,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """WNBA: no auto-proj API yet. Needs manual projected or skip."""
    errors = price_errors(prop)
    if errors:
        return _rejected(prop, errors, file_meta=file_meta, as_of=as_of)
    projected = prop.get("projected")
    if projected is None:
        return _base_result(
            prop,
            None,
            file_meta=file_meta,
            as_of=as_of,
            skip_reason="WNBA: no auto-proj; supply projected manually",
        )
    edge = _edge_from_prop(prop, float(projected))
    return _base_result(prop, edge, file_meta=file_meta, as_of=as_of, proj_source="manual")


def evaluate_nfl_prop(
    prop: dict[str, Any],
    *,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """NFL: projected optional; without it skip (Muse owns final NFL ticket)."""
    errors = price_errors(prop)
    if errors:
        return _rejected(prop, errors, file_meta=file_meta, as_of=as_of)
    projected = prop.get("projected")
    if projected is None:
        return _base_result(
            prop,
            None,
            file_meta=file_meta,
            as_of=as_of,
            skip_reason="NFL: no auto-proj; supply projected manually (Muse owns ticket)",
        )
    edge = _edge_from_prop(prop, float(projected))
    return _base_result(prop, edge, file_meta=file_meta, as_of=as_of, proj_source="manual")


def evaluate_mlb_prop(
    prop: dict[str, Any],
    *,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """MLB skeleton: needs manual projected."""
    errors = price_errors(prop)
    if errors:
        return _rejected(prop, errors, file_meta=file_meta, as_of=as_of)
    projected = prop.get("projected")
    if projected is None:
        return _base_result(
            prop,
            None,
            file_meta=file_meta,
            as_of=as_of,
            skip_reason="MLB: no auto-proj; supply projected manually",
        )
    edge = _edge_from_prop(prop, float(projected))
    return _base_result(prop, edge, file_meta=file_meta, as_of=as_of, proj_source="manual")


def evaluate_prop(
    prop: dict[str, Any],
    *,
    n: int = 10,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Dispatch; if prop has `books` {book: {line, odds, source} | null}, price every book.

    One bad prop returns a skipped row. It does not raise.
    """
    if not isinstance(prop, dict):
        return {
            "player": None,
            "evaluated": False,
            "skipped": True,
            "status": "NOT_EVALUABLE",
            "qualified": False,
            "data_quality": "incomplete",
            "skip_reason": "prop must be a JSON object",
            "explanation": "This entry is not a prop object, so it was not graded.",
            "status_reasons": ["prop must be a JSON object"],
        }
    if threshold is not None and prop.get("threshold") is None:
        prop = {**prop, "threshold": threshold}
    try:
        if prop.get("books"):
            return evaluate_multibook_prop(prop, n=n, file_meta=file_meta, as_of=as_of)
        return _evaluate_single(prop, n=n, file_meta=file_meta, as_of=as_of)
    except Exception as exc:  # noqa: BLE001 — one bad prop must not kill the batch
        return _base_result(
            prop,
            None,
            file_meta=file_meta,
            as_of=as_of,
            skip_reason=f"could not evaluate this prop: {exc}",
        )


def evaluate_multibook_prop(
    prop: dict[str, Any],
    *,
    n: int = 10,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Evaluate each supported book's price; headline = best legacy edge.

    Missing books stay missing. A bad price on one book does not drop the others.
    ``best_book`` is the highest legacy edge, not a claim about Indiana.
    """
    per_book: dict[str, Any] = {}
    raw = {normalize_book(k) or k: v for k, v in (prop.get("books") or {}).items()}
    best = None
    for key in BOOK_KEYS:
        quote = raw.get(key)
        if not quote or quote.get("line") is None or quote.get("odds") is None:
            per_book[key] = {
                "book": BOOK_LABELS[key],
                "missing": True,
                "status": "NOT_EVALUABLE",
                "reason": (quote or {}).get("reason", "no line found"),
                "status_reasons": [
                    (quote or {}).get("reason")
                    or "not available in this input. That does not mean the sportsbook has no price."
                ],
            }
            continue
        sub = {**prop, "line": quote["line"], "odds": quote["odds"], "source": quote.get("source")}
        sub.pop("books", None)
        sub.pop("opposite", None)
        if quote.get("opposite"):
            sub["opposite"] = quote["opposite"]
        if quote.get("retrieved_at"):
            sub["retrieved_at"] = quote["retrieved_at"]
        try:
            row = _evaluate_single(sub, n=n, file_meta=file_meta, as_of=as_of)
        except Exception as exc:  # noqa: BLE001 — one bad book must not drop the others
            row = _base_result(
                sub,
                None,
                file_meta=file_meta,
                as_of=as_of,
                skip_reason=f"could not evaluate this prop: {exc}",
            )
        row["book"] = BOOK_LABELS[key]
        per_book[key] = row
        if row.get("evaluated") and (best is None or row["edge"] > best["edge"]):
            best = row
    unknown = [k for k in raw if k not in BOOK_KEYS]
    if best is None:
        out = _base_result(prop, None, file_meta=file_meta, as_of=as_of, skip_reason="no priced book")
    else:
        out = dict(best)
        out["best_book"] = best["book"]
    out["books"] = per_book
    if unknown:
        out["unsupported_books"] = unknown
    return out


def _evaluate_single(
    prop: dict[str, Any],
    *,
    n: int = 10,
    file_meta: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Dispatch by sport key (nba/wnba/nfl/mlb)."""
    sport = str(prop.get("sport") or "").strip().lower()
    if sport in ("nba", "basketball_nba"):
        return evaluate_nba_prop(prop, n=n, file_meta=file_meta, as_of=as_of)
    if sport in ("wnba", "basketball_wnba"):
        return evaluate_wnba_prop(prop, file_meta=file_meta, as_of=as_of)
    if sport in ("nfl", "americanfootball_nfl"):
        return evaluate_nfl_prop(prop, file_meta=file_meta, as_of=as_of)
    if sport in ("mlb", "baseball_mlb"):
        return evaluate_mlb_prop(prop, file_meta=file_meta, as_of=as_of)
    errors = price_errors(prop)
    if errors:
        return _rejected(prop, errors, file_meta=file_meta, as_of=as_of)
    if prop.get("projected") is not None:
        edge = _edge_from_prop(prop, float(prop["projected"]))
        return _base_result(
            prop,
            edge,
            file_meta=file_meta,
            as_of=as_of,
            proj_source="manual",
            sport_note="unknown sport",
        )
    return _base_result(
        prop,
        None,
        file_meta=file_meta,
        as_of=as_of,
        skip_reason=f"unknown sport: {sport}",
    )
