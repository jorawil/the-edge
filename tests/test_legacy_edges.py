"""Saved sample reports keep the same legacy edge numbers.

The projections and prices in those files are historical inputs. This test
only checks that the original formula did not move. It does not mean those
props were good bets.
"""

import json
from collections import Counter
from pathlib import Path

from betting_model.sports import evaluate_prop

ROOT = Path(__file__).resolve().parents[1]


def _key(row: dict) -> tuple:
    return (
        row.get("player"),
        str(row.get("market")),
        float(row["line"]),
        int(row["odds"]),
        bool(row["is_over"]),
        float(row["projected"]),
    )


def _saved_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["all"]


def _input_props(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return raw["props"]
    return raw


def _signature(row: dict) -> tuple:
    return _key(row) + (
        round(float(row["edge"]), 6),
        round(float(row["model_prob"]), 6),
        round(float(row["implied_prob"]), 6),
        bool(row["positive_edge"]),
        float(row["sigma"]) if row.get("sigma") is not None else None,
    )


def _assert_legacy_edges(props_path: Path, saved_path: Path) -> None:
    saved_rows = _saved_rows(saved_path)
    results = []
    for prop in _input_props(props_path):
        result = evaluate_prop(prop)
        assert result["evaluated"] is True, result.get("skip_reason")
        results.append(result)
    assert Counter(_signature(row) for row in results) == Counter(
        _signature(row) for row in saved_rows
    )


def test_rushing_sample_keeps_legacy_edges():
    _assert_legacy_edges(ROOT / "props_rush_w11_sat.json", ROOT / "w11_rushing_sat.json")


def test_passing_sample_keeps_legacy_edges():
    _assert_legacy_edges(ROOT / "props_w11_passing_sat.json", ROOT / "edges_latest.json")
