"""Sport-specific prop edge helpers. Reuse calculate_edge; NBA-first for auto-proj."""

from __future__ import annotations

from typing import Any

from .core import EDGE_THRESHOLD, calculate_edge, implied_probability
from .books import BOOK_KEYS, BOOK_LABELS, normalize_book
from .nba import get_nba_player_recent_avg, map_market_to_avg


def _edge_from_prop(prop: dict[str, Any], projected: float) -> dict[str, Any]:
    """Honor eval_mode=binary_ml (win_prob - implied) and optional sigma."""
    mode = str(prop.get("eval_mode") or "").strip().lower()
    odds = int(prop["odds"])
    line = float(prop["line"])
    is_over = bool(prop["is_over"])
    market = str(prop.get("market") or "")
    sigma = prop.get("sigma")
    if mode == "binary_ml":
        # projected is already a win probability in [0,1]
        win_p = float(projected)
        if win_p > 1.0:
            win_p = win_p / 100.0
        implied = implied_probability(odds)
        edge = win_p - implied
        return {
            "projected": float(projected) if float(projected) <= 1.0 else win_p,
            "line": line,
            "odds": odds,
            "is_over": is_over,
            "implied_prob": round(implied, 6),
            "model_prob": round(win_p, 6),
            "edge": round(edge, 6),
            "positive_edge": bool(edge > EDGE_THRESHOLD),
            "sigma": None,
            "market": market,
            "edge_threshold": EDGE_THRESHOLD,
            "eval_mode": "binary_ml",
        }
    kw = {"market": market}
    if sigma is not None:
        kw["sigma"] = float(sigma)
    return calculate_edge(float(projected), line, odds, is_over, **kw)


def _base_result(prop: dict[str, Any], edge: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    out = {
        "player": prop.get("player"),
        "sport": prop.get("sport"),
        "market": prop.get("market"),
        "line": prop.get("line"),
        "odds": prop.get("odds"),
        "is_over": prop.get("is_over"),
        "source": prop.get("source"),
        "notes": prop.get("notes"),
        "evaluated": edge is not None,
        "skipped": edge is None,
    }
    if edge:
        out.update(edge)
    out.update(extra)
    return out


def evaluate_nba_prop(prop: dict[str, Any], *, n: int = 10) -> dict[str, Any]:
    """Evaluate one NBA prop; auto-fill projected from recent avg if missing."""
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
                    skip_reason="market not mappable to recent avg",
                    recent_avg=avg_info,
                )
        else:
            return _base_result(
                prop,
                None,
                skip_reason="no projected and nba_api failed",
                recent_avg=avg_info,
            )

    edge = _edge_from_prop(prop, float(projected))
    return _base_result(
        prop,
        edge,
        projected=float(projected),
        proj_source=proj_source,
        recent_avg=avg_info,
    )


def evaluate_wnba_prop(prop: dict[str, Any]) -> dict[str, Any]:
    """WNBA: no auto-proj API yet. Needs manual projected or skip."""
    projected = prop.get("projected")
    if projected is None:
        return _base_result(
            prop,
            None,
            skip_reason="WNBA: no auto-proj; supply projected manually",
        )
    edge = _edge_from_prop(prop, float(projected))
    return _base_result(prop, edge, proj_source="manual")


def evaluate_nfl_prop(prop: dict[str, Any]) -> dict[str, Any]:
    """NFL: projected optional; without it skip (Muse owns final NFL ticket)."""
    projected = prop.get("projected")
    if projected is None:
        return _base_result(
            prop,
            None,
            skip_reason="NFL: no auto-proj; supply projected manually (Muse owns ticket)",
        )
    edge = _edge_from_prop(prop, float(projected))
    return _base_result(prop, edge, proj_source="manual")


def evaluate_mlb_prop(prop: dict[str, Any]) -> dict[str, Any]:
    """MLB skeleton: needs manual projected."""
    projected = prop.get("projected")
    if projected is None:
        return _base_result(
            prop,
            None,
            skip_reason="MLB: no auto-proj; supply projected manually",
        )
    edge = _edge_from_prop(prop, float(projected))
    return _base_result(prop, edge, proj_source="manual")


def evaluate_prop(prop: dict[str, Any], *, n: int = 10) -> dict[str, Any]:
    """Dispatch; if prop has `books` {book: {line, odds, source} | null}, price every book."""
    if prop.get("books"):
        return evaluate_multibook_prop(prop, n=n)
    return _evaluate_single(prop, n=n)


def evaluate_multibook_prop(prop: dict[str, Any], *, n: int = 10) -> dict[str, Any]:
    """Evaluate each supported book's price; headline = best edge. Missing books stay missing."""
    per_book: dict[str, Any] = {}
    raw = {normalize_book(k) or k: v for k, v in (prop.get("books") or {}).items()}
    best = None
    for key in BOOK_KEYS:
        q = raw.get(key)
        if not q or q.get("line") is None or q.get("odds") is None:
            per_book[key] = {"book": BOOK_LABELS[key], "missing": True,
                             "reason": (q or {}).get("reason", "no line found")}
            continue
        sub = {**prop, "line": q["line"], "odds": q["odds"], "source": q.get("source")}
        sub.pop("books", None)
        r = _evaluate_single(sub, n=n)
        r["book"] = BOOK_LABELS[key]
        per_book[key] = r
        if r.get("evaluated") and (best is None or r["edge"] > best["edge"]):
            best = r
    unknown = [k for k in raw if k not in BOOK_KEYS]
    if best is None:
        out = _base_result(prop, None, skip_reason="no priced book")
    else:
        out = dict(best)
        out["best_book"] = best["book"]
    out["books"] = per_book
    if unknown:
        out["unsupported_books"] = unknown
    return out


def _evaluate_single(prop: dict[str, Any], *, n: int = 10) -> dict[str, Any]:
    """Dispatch by sport key (nba/wnba/nfl/mlb)."""
    sport = str(prop.get("sport") or "").strip().lower()
    if sport in ("nba", "basketball_nba"):
        return evaluate_nba_prop(prop, n=n)
    if sport in ("wnba", "basketball_wnba"):
        return evaluate_wnba_prop(prop)
    if sport in ("nfl", "americanfootball_nfl"):
        return evaluate_nfl_prop(prop)
    if sport in ("mlb", "baseball_mlb"):
        return evaluate_mlb_prop(prop)
    # fallback: if projected present, still calc edge
    if prop.get("projected") is not None:
        edge = _edge_from_prop(prop, float(prop["projected"]))
        return _base_result(prop, edge, proj_source="manual", sport_note="unknown sport")
    return _base_result(prop, None, skip_reason=f"unknown sport: {sport}")
