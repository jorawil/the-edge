"""Calibration report for the SQLite archive.

Every prop, price, and result in this file is a unit-test fixture.
None of it is a real projection, a real price, or a real result.
"""

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from betting_model.archive import add_correction, archive_evaluation, attach_result, get_snapshot
from betting_model.archive_analysis import (
    MIN_BIN_SAMPLE,
    MIN_CALIBRATION_CLAIM_EVENTS,
    MIN_GROUP_SAMPLE,
    MODEL_VERSION_NOT_RECORDED,
    analyze_archive,
    describe_reliability,
    multiclass_brier,
    multiclass_log_loss,
    render_markdown,
    select_current_result,
)
from betting_model.books import BOOKS
from betting_model.calibration import MIN_MARKET_SAMPLE, brier_score, log_loss
from betting_model.cli import main
from betting_model.core import EDGE_THRESHOLD, SIGMA_DEFAULTS
from betting_model.version import MODEL_VERSION


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    state = {"moment": datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)}

    def _current():
        return state["moment"]

    monkeypatch.setattr("betting_model.archive._utc_now", _current)
    return state


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 2, day, hour, minute, tzinfo=timezone.utc)


def _save(clock, database: Path, day: int, **overrides) -> str:
    clock["moment"] = _when(day, 12)
    prop = {
        "player": "Example Player",
        "player_id": "ex-1",
        "sport": "nfl",
        "event_id": f"game-{day}",
        "event_start": _iso(_when(day, 18)),
        "market": "passing_yards",
        "is_over": True,
        "line": 250.5,
        "odds": -110,
        "projected": 270.0,
        "projection_source": "unit-test fixture, not a real projection",
        "projection_at": _iso(_when(day, 11)),
        "retrieved_at": _iso(_when(day, 11, 5)),
        "book": "draftkings",
    }
    prop.update(overrides)
    saved = archive_evaluation(database, prop)
    assert saved["ok"] is True, saved
    return saved["archive_id"]


def _settle(clock, database: Path, archive_id: str, day: int, *, actual=None, status="final", provided=None):
    if provided is None:
        provided = actual is not None
    clock["moment"] = _when(day, 23, 30)
    result = attach_result(
        database,
        archive_id,
        game_status=status,
        result_source="unit-test fixture, not a real box score",
        result_retrieved_at=_iso(_when(day, 23)),
        actual=actual,
        actual_provided=provided,
    )
    assert result["ok"] is True, result
    return result


def _groups(report, kind):
    return report["slices"]["all_rows_not_held_out"][kind]


def _find(report, kind, slice_name="all_rows_not_held_out", **match):
    found = []
    for group in report["slices"][slice_name][kind]:
        if all(group.get(key) == value for key, value in match.items()):
            found.append(group)
    return found


def _ids(report, kind, slice_name="all_rows_not_held_out"):
    found = set()
    for group in report["slices"][slice_name][kind]:
        found.update(group["archive_ids"])
        found.update(group.get("collapsed_archive_ids") or [])
    return found


def _reason_ids(report, track, text):
    found = set()
    for item in report["coverage"][track]["reasons"]:
        if text in item["reason"]:
            found.update(item["archive_ids"])
    return found


def _insert_result(
    database: Path,
    archive_id: str,
    result_id: str,
    *,
    settlement: str,
    actual: float,
    retrieved: str,
    attached: str,
    supersedes: str | None = None,
) -> None:
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            """
            INSERT INTO results (
                result_id, archive_id, attached_at, actual_stat, actual_missing, settlement,
                settlement_reason, game_status, result_source, result_retrieved_at,
                result_retrieved_at_source, supersedes_result_id
            ) VALUES (?, ?, ?, ?, 0, ?, 'unit-test fixture, not a real result', 'final',
                      'unit test', ?, ?, ?)
            """,
            (result_id, archive_id, attached, actual, settlement, retrieved, retrieved, supersedes),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_snapshot(database: Path, source_id: str, **changes) -> None:
    source = get_snapshot(database, source_id)
    row = {key: value for key, value in source.items() if key not in {"results", "corrections"}}
    row.update(changes)
    conn = sqlite3.connect(database)
    try:
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO snapshots ({', '.join(columns)}) VALUES ({placeholders})",
            [row[column] for column in columns],
        )
        conn.commit()
    finally:
        conn.close()


def test_brier_and_log_loss_match_the_definition():
    probabilities = [0.8, 0.4, 1.0]
    outcomes = [1, 0, 1]
    expected_brier = ((0.8 - 1) ** 2 + (0.4 - 0) ** 2 + (1.0 - 1) ** 2) / 3
    assert brier_score(probabilities, outcomes) == pytest.approx(expected_brier)

    def _clip(probability: float) -> float:
        return min(max(probability, 1e-6), 1 - 1e-6)

    losses = []
    for probability, outcome in zip(probabilities, outcomes):
        clipped = _clip(probability)
        losses.append(-(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped)))
    assert log_loss(probabilities, outcomes) == pytest.approx(sum(losses) / len(losses))
    assert brier_score([], []) is None
    assert log_loss([0.5], [1, 0]) is None


def test_multiclass_brier_and_log_loss_keep_pushes():
    # A push scored against (0.2, 0.5, 0.3).
    assert multiclass_brier([(0.2, 0.5, 0.3)], ["push"]) == pytest.approx(0.2**2 + 0.5**2 + 0.7**2)
    assert multiclass_log_loss([(0.2, 0.5, 0.3)], ["push"]) == pytest.approx(-math.log(0.3))
    assert multiclass_brier([(1.0, 0.0, 0.0)], ["win"]) == pytest.approx(0.0)
    assert multiclass_brier([(0.5, 0.5, 0.0)], ["win"]) == pytest.approx(0.5)
    assert multiclass_log_loss([(0.0, 1.0, 0.0)], ["win"]) == pytest.approx(-math.log(1e-6))
    assert multiclass_brier([], []) is None
    assert multiclass_log_loss([(0.5, 0.5, 0.0)], ["win", "loss"]) is None


def test_probability_ranges_count_every_row_and_warn_when_small():
    assert MIN_GROUP_SAMPLE == MIN_MARKET_SAMPLE == 30
    assert MIN_BIN_SAMPLE == 30
    assert MIN_CALIBRATION_CLAIM_EVENTS >= 300
    table = describe_reliability([0.25, 0.25, 1.0], [1, 0, 1], min_bin_sample=30)
    assert len(table) == 10
    assert sum(row["sample_size"] for row in table) == 3
    assert table[2]["lower"] == pytest.approx(0.2)
    assert table[2]["upper"] == pytest.approx(0.3)
    assert table[2]["sample_size"] == 2
    assert table[2]["mean_predicted"] == pytest.approx(0.25)
    assert table[2]["observed_rate"] == pytest.approx(0.5)
    assert table[2]["enough_to_evaluate"] is False
    assert "below 30" in table[2]["warning"]
    assert table[-1]["sample_size"] == 1
    assert table[-1]["observed_rate"] == pytest.approx(1.0)
    assert table[0]["sample_size"] == 0
    assert "no observations" in table[0]["warning"]
    large_enough = describe_reliability([0.25, 0.22], [1, 1], min_bin_sample=2)
    assert large_enough[2]["enough_to_evaluate"] is True
    assert large_enough[2]["warning"] is None


def test_current_result_keeps_a_successor_and_rejects_a_tie():
    older = {
        "result_id": "a",
        "attached_at": "2026-02-02T00:00:00Z",
        "supersedes_result_id": None,
        "settlement": "Win",
    }
    newer = {
        "result_id": "b",
        "attached_at": "2026-02-03T00:00:00Z",
        "supersedes_result_id": "a",
        "settlement": "Loss",
    }
    current, history, live = select_current_result([newer, older])
    assert current["settlement"] == "Loss"
    assert history == 1
    assert live == 1
    first = {"result_id": "a", "attached_at": "2026-02-02T00:00:00Z", "supersedes_result_id": None}
    second = {"result_id": "b", "attached_at": "2026-02-03T00:00:00Z", "supersedes_result_id": None}
    current, history, live = select_current_result([first, second])
    assert current is None
    assert history == 0
    assert live == 2
    cycle_a = {"result_id": "a", "attached_at": "2026-02-02T00:00:00Z", "supersedes_result_id": "b"}
    cycle_b = {"result_id": "b", "attached_at": "2026-02-03T00:00:00Z", "supersedes_result_id": "a"}
    assert select_current_result([cycle_a, cycle_b]) == (None, 2, 0)
    assert select_current_result([]) == (None, 0, 0)


def test_empty_archive_does_not_invent_scores_or_create_a_file(tmp_path: Path):
    database = tmp_path / "missing.sqlite"
    report = analyze_archive(database)
    assert database.exists() is False
    assert report["ok"] is True
    assert report["archive_exists"] is False
    assert report["calibration_claimed"] is False
    assert report["qualified_gate_changed"] is False
    assert report["profit_reported"] is False
    assert report["roi_reported"] is False
    assert report["questions"]["predictions_archived"] == 0
    assert report["questions"]["settled"] == 0
    assert report["questions"]["usable_for_conditional_calibration"] == 0
    assert report["slices"]["test"]["conditional_groups"] == []
    assert report["slices"]["test"]["projection_groups"] == []
    text = render_markdown(report)
    assert "Predictions archived: **0**" in text
    assert "does not claim that the model is calibrated" in text
    assert "Profit reported: **no**" in text
    assert analyze_archive(tmp_path)["ok"] is False
    bad = analyze_archive(database, train_fraction=0.9, validation_fraction=0.2)
    assert bad["ok"] is False
    assert bad["calibration_claimed"] is False


def test_small_sample_is_shown_and_does_not_claim_calibration(tmp_path: Path, clock):
    database = tmp_path / "one.sqlite"
    archive_id = _save(clock, database, 2, projected=270)
    _settle(clock, database, archive_id, 2, actual=280)
    before = dict(SIGMA_DEFAULTS)
    report = analyze_archive(database)
    assert SIGMA_DEFAULTS == before
    assert EDGE_THRESHOLD == pytest.approx(0.03)
    assert report["calibration_claimed"] is False
    assert report["edge_threshold"] == pytest.approx(0.03)
    assert report["edge_threshold_changed"] is False
    assert report["automatic_betting"] is False
    assert report["qualified_snapshots"] == 0
    group = report["slices"]["test"]["conditional_groups"][0]
    stored = get_snapshot(database, archive_id)["conditional_win_probability"]
    assert stored is not None
    assert group["sample_size"] == 1
    assert group["event_count"] == 1
    assert group["enough_to_evaluate"] is False
    assert group["calibration_claimed"] is False
    assert group["probabilities_recomputed"] is False
    assert group["mean_predicted"] == pytest.approx(stored)
    assert group["brier_score"] is not None
    assert any("below 30" in warning for warning in group["warnings"])
    ranges = group["probability_ranges"]
    assert len(ranges) == 10
    assert sum(item["sample_size"] for item in ranges) == 1
    assert all(item["warning"] for item in ranges)
    text = render_markdown(report)
    assert "sample size 1" in text
    assert "too small to evaluate" in text
    lowered = analyze_archive(database, min_group_sample=1, min_bin_sample=1)
    assert lowered["calibration_claimed"] is False
    assert lowered["qualified_gate_changed"] is False
    lowered_group = lowered["slices"]["test"]["conditional_groups"][0]
    assert lowered_group["enough_to_evaluate"] is True
    assert lowered_group["calibration_claimed"] is False
    occupied = [item for item in lowered_group["probability_ranges"] if item["sample_size"]]
    assert occupied[0]["warning"] is None


def test_events_are_split_by_kickoff_not_by_insert_order(tmp_path: Path, clock):
    database = tmp_path / "order.sqlite"
    for day in (5, 1, 3, 2, 4):
        _save(clock, database, day)
    report = analyze_archive(database)
    split = report["chronological_split"]
    assert split["train_event_ids"] == ["game-1", "game-2", "game-3"]
    assert split["validation_event_ids"] == ["game-4"]
    assert split["test_event_ids"] == ["game-5"]
    assert report["slices"]["test"]["held_out"] is True
    assert report["slices"]["train"]["held_out"] is False
    assert report["slices"]["all_rows_not_held_out"]["held_out"] is False
    assert "not a held-out test" in report["slices"]["all_rows_not_held_out"]["label"]
    assert set(split["train_event_ids"]).isdisjoint(split["test_event_ids"])
    assert set(split["train_event_ids"]).isdisjoint(split["validation_event_ids"])


def test_later_result_does_not_change_earlier_scores(tmp_path: Path, clock):
    database = tmp_path / "leak.sqlite"
    early = _save(clock, database, 2, projected=10, line=5.5)
    late = _save(clock, database, 4, projected=10, line=5.5)
    _settle(clock, database, early, 2, actual=10)
    _settle(clock, database, late, 4, actual=50)
    before = analyze_archive(database, train_fraction=0.5, validation_fraction=0.0)
    assert before["chronological_split"]["train_event_ids"] == ["game-2"]
    assert before["chronological_split"]["test_event_ids"] == ["game-4"]
    train_projection = _find(before, "projection_groups", "train", market="PASS_YDS")[0]
    test_projection = _find(before, "projection_groups", "test", market="PASS_YDS")[0]
    train_probability = _find(before, "conditional_groups", "train", market="PASS_YDS")[0]
    assert train_projection["mae"] == pytest.approx(0.0)
    assert train_projection["mean_signed_error"] == pytest.approx(0.0)
    assert test_projection["mae"] == pytest.approx(40.0)
    assert early in train_probability["archive_ids"]
    assert late not in train_probability["archive_ids"]
    clock["moment"] = _when(5, 12)
    second = attach_result(
        database,
        late,
        game_status="final",
        result_source="unit-test fixture, a later result",
        result_retrieved_at=_iso(_when(5, 11)),
        actual=0,
        actual_provided=True,
    )
    assert second["ok"] is True, second
    after = analyze_archive(database, train_fraction=0.5, validation_fraction=0.0)
    assert after["chronological_split"]["train_event_ids"] == ["game-2"]
    assert _find(after, "projection_groups", "train", market="PASS_YDS")[0]["mae"] == pytest.approx(0.0)
    assert _find(after, "conditional_groups", "train", market="PASS_YDS")[0]["brier_score"] == pytest.approx(
        train_probability["brier_score"]
    )
    assert _find(after, "conditional_groups", "train", market="PASS_YDS")[0]["archive_ids"] == [early]
    assert _find(after, "projection_groups", "test", market="PASS_YDS")[0]["mae"] == pytest.approx(10.0)
    assert after["superseded_results_not_scored"] == 1
    assert late not in _ids(after, "conditional_groups", "train")


def test_bad_timestamps_are_excluded_even_if_they_were_inserted(tmp_path: Path, clock):
    database = tmp_path / "timestamps.sqlite"
    early = _save(clock, database, 2, projected=10, line=5.5)
    late = _save(clock, database, 4, projected=10, line=5.5)
    bad_result_id = _save(clock, database, 6, projected=10, line=5.5)
    _settle(clock, database, early, 2, actual=10)
    _settle(clock, database, late, 4, actual=12)
    before = analyze_archive(database, train_fraction=0.5, validation_fraction=0.0)
    leaked_start = _iso(_when(1, 18))
    _insert_snapshot(
        database,
        early,
        archive_id="leaked-row",
        observation_key="leaked-observation",
        event_id="game-leaked",
        event_start=leaked_start,
        event_start_source=leaked_start,
        projection_created_at=leaked_start,
        projection_created_at_source=leaked_start,
    )
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            """
            INSERT INTO results (
                result_id, archive_id, attached_at, actual_stat, actual_missing, settlement,
                settlement_reason, game_status, result_source, result_retrieved_at,
                result_retrieved_at_source, supersedes_result_id
            ) VALUES (?, ?, ?, ?, 0, 'Win', 'unit-test leak, not a real result', 'final',
                      'unit test', ?, ?, NULL)
            """,
            (
                "early-result",
                bad_result_id,
                "2026-02-10T12:00:00Z",
                400,
                "2026-02-01T00:00:00Z",
                "2026-02-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    after = analyze_archive(database, train_fraction=0.5, validation_fraction=0.0)
    assert after["chronological_split"]["train_event_ids"] == before["chronological_split"]["train_event_ids"]
    assert _find(after, "projection_groups", "train", market="PASS_YDS")[0]["mae"] == pytest.approx(
        _find(before, "projection_groups", "train", market="PASS_YDS")[0]["mae"]
    )
    assert "leaked-row" in _reason_ids(after, "conditional_exclusions", "projection timestamp is not before the event")
    assert "leaked-row" not in _ids(after, "conditional_groups")
    assert "leaked-row" not in _ids(after, "projection_groups")
    assert bad_result_id in _reason_ids(after, "conditional_exclusions", "result timestamp is before the event")
    assert bad_result_id not in _ids(after, "conditional_groups")
    assert "game-leaked" not in after["chronological_split"]["train_event_ids"]
    assert "game-6" not in after["chronological_split"]["train_event_ids"]


def test_push_void_unknown_missing_zero_and_under(tmp_path: Path, clock):
    database = tmp_path / "settlement.sqlite"
    half = _save(clock, database, 2, line=250.5, projected=270)
    whole_win = _save(clock, database, 3, line=250, projected=270)
    push = _save(clock, database, 4, line=250, projected=240)
    void = _save(clock, database, 5, line=250.5, projected=270)
    incomplete = _save(clock, database, 6, line=250.5, projected=270)
    missing = _save(clock, database, 7, line=250.5, projected=270)
    zero = _save(
        clock,
        database,
        8,
        market="receptions",
        line=1.5,
        projected=3,
        player_id="ex-zero",
    )
    unknown_stat = _save(
        clock,
        database,
        9,
        market="fantasy_points",
        line=10.2,
        projected=12,
        player_id="ex-fantasy",
    )
    under = _save(clock, database, 10, is_over="false", line=250.5, projected=230, player_id="ex-under")
    _settle(clock, database, half, 2, actual=280)
    _settle(clock, database, whole_win, 3, actual=280)
    _settle(clock, database, push, 4, actual=250)
    _settle(clock, database, void, 5, status="dnp", provided=False)
    _settle(clock, database, incomplete, 6, status="incomplete", provided=False)
    _settle(clock, database, missing, 7, status="final", provided=False)
    _settle(clock, database, zero, 8, actual=0)
    _settle(clock, database, unknown_stat, 9, actual=9)
    _settle(clock, database, under, 10, actual=100)
    report = analyze_archive(database)
    coverage = report["coverage"]
    assert coverage["usable_conditional_calibration"] + coverage["conditional_exclusions"]["rows"] == coverage[
        "predictions_archived"
    ]
    assert push not in _ids(report, "conditional_groups")
    assert push in _reason_ids(report, "conditional_exclusions", "push; left out of conditional win probability")
    whole = _find(report, "conditional_groups", market="PASS_YDS", line_family="whole_number")
    assert len(whole) == 1
    assert whole_win in whole[0]["archive_ids"]
    assert push not in whole[0]["archive_ids"]
    assert whole[0]["outcome_counts"]["push"] == 0
    unconditional = _find(report, "unconditional_groups", market="PASS_YDS", line_family="whole_number")[0]
    assert unconditional["score_type"] == "multiclass_unconditional"
    assert push in unconditional["archive_ids"]
    assert unconditional["outcome_counts"]["push"] == 1
    half_group = _find(report, "conditional_groups", market="PASS_YDS", line_family="half_point")[0]
    assert half in half_group["archive_ids"]
    assert whole_win not in half_group["archive_ids"]
    assert {group["line_family"] for group in _find(report, "conditional_groups", market="PASS_YDS")} == {
        "half_point",
        "whole_number",
    }
    for kind in ("conditional_groups", "unconditional_groups", "projection_groups"):
        assert void not in _ids(report, kind)
    assert void in _reason_ids(report, "conditional_exclusions", "void")
    assert incomplete in _reason_ids(report, "projection_exclusions", "unknown settlement; no played stat")
    assert missing in _reason_ids(report, "projection_exclusions", "actual stat is missing and was not treated as zero")
    assert incomplete not in _ids(report, "projection_groups")
    assert missing not in _ids(report, "projection_groups")
    assert missing not in _ids(report, "conditional_groups")
    zero_projection = _find(report, "projection_groups", market="RECEPTIONS")[0]
    assert zero in zero_projection["archive_ids"]
    assert zero_projection["mean_signed_error"] == pytest.approx(-3.0)
    assert zero_projection["mae"] == pytest.approx(3.0)
    zero_probability = _find(report, "conditional_groups", market="RECEPTIONS")[0]
    assert zero_probability["outcome_counts"] == {"win": 0, "loss": 1, "push": 0}
    assert unknown_stat in _ids(report, "projection_groups")
    assert unknown_stat not in _ids(report, "conditional_groups")
    fantasy = _find(report, "projection_groups", market="FANTASY_POINTS")[0]
    assert fantasy["mean_signed_error"] == pytest.approx(-3.0)
    under_snapshot = get_snapshot(database, under)
    assert under_snapshot["side"] == "Under"
    under_group = _find(report, "conditional_groups", market="PASS_YDS", line_family="half_point")[0]
    assert under in under_group["archive_ids"]
    # The Under row is not the only half-point row. Check its outcome through the stored probability
    # by scoring that one row's contribution: a loss would not match a group that also has a win.
    assert under_group["outcome_counts"]["win"] >= 1
    stored_under = under_snapshot["conditional_win_probability"]
    assert stored_under is not None
    # Rebuild the one-row expectation and compare with a report of that snapshot alone.
    alone = tmp_path / "under-only.sqlite"
    under_only = _save(clock, alone, 10, is_over="false", line=250.5, projected=230, player_id="ex-under")
    _settle(clock, alone, under_only, 10, actual=100)
    alone_report = analyze_archive(alone)
    alone_group = alone_report["slices"]["test"]["conditional_groups"][0]
    alone_probability = get_snapshot(alone, under_only)["conditional_win_probability"]
    assert alone_group["outcome_counts"]["win"] == 1
    assert alone_group["outcome_counts"]["loss"] == 0
    assert alone_group["brier_score"] == pytest.approx((alone_probability - 1) ** 2)
    assert alone_group["brier_score"] != pytest.approx((alone_probability - 0) ** 2)


def test_markets_books_and_settlement_families_stay_apart(tmp_path: Path, clock):
    database = tmp_path / "groups.sqlite"
    first = _save(clock, database, 2, book="draftkings", projected=270, line=250.5)
    second = _save(clock, database, 2, book="fanduel", projected=270, line=250.5)
    nba = _save(
        clock,
        database,
        4,
        sport="nba",
        market="points",
        line=20.5,
        projected=25,
        player="Other Player",
        player_id="ex-2",
        book="draftkings",
    )
    _settle(clock, database, first, 2, actual=280)
    _settle(clock, database, second, 2, actual=280)
    _settle(clock, database, nba, 4, actual=30)
    report = analyze_archive(database)
    passing = _find(report, "conditional_groups", market="PASS_YDS", line_family="half_point")[0]
    points = _find(report, "conditional_groups", market="PTS", line_family="half_point")[0]
    assert {first, second} == set(passing["archive_ids"])
    assert points["archive_ids"] == [nba]
    assert {book["sportsbook"] for book in passing["sportsbooks"]} == {"draftkings", "fanduel"}
    assert sum(book["sample_size"] for book in passing["sportsbooks"]) == passing["sample_size"]
    assert "pooled_brier_score" not in report
    for slice_report in report["slices"].values():
        for kind in ("conditional_groups", "unconditional_groups", "projection_groups"):
            for group in slice_report[kind]:
                assert group["market"] in {"PASS_YDS", "PTS"}
                for book in group.get("sportsbooks") or []:
                    assert book["market"] == group["market"]
                    assert book["sport"] == group["sport"]
                    assert book.get("line_family") == group.get("line_family")
                    assert "sportsbooks" not in book


def test_projection_error_dedupes_books_and_keeps_bias(tmp_path: Path, clock):
    database = tmp_path / "projection.sqlite"
    first = _save(clock, database, 2, book="draftkings", projected=10, line=5.5)
    second = _save(clock, database, 2, book="fanduel", projected=10, line=5.5)
    other = _save(clock, database, 4, book="draftkings", projected=10, line=5.5)
    points = _save(
        clock,
        database,
        6,
        sport="nba",
        market="points",
        line=19.5,
        projected=20,
        player="Other Player",
        player_id="ex-2",
    )
    _settle(clock, database, first, 2, actual=13)
    _settle(clock, database, second, 2, actual=13)
    _settle(clock, database, other, 4, actual=7)
    _settle(clock, database, points, 6, actual=25)
    report = analyze_archive(database)
    passing = _find(report, "projection_groups", market="PASS_YDS")[0]
    scoring = _find(report, "projection_groups", market="PTS")[0]
    assert passing["sample_size"] == 2
    assert passing["collapsed_duplicate_rows"] == 1
    assert passing["mae"] == pytest.approx(3.0)
    assert passing["rmse"] == pytest.approx(3.0)
    assert passing["mean_signed_error"] == pytest.approx(0.0)
    assert passing["separate_from_probability_calibration"] is True
    assert {first, second, other} == set(passing["archive_ids"]) | set(passing["collapsed_archive_ids"])
    assert scoring["sample_size"] == 1
    assert scoring["mean_signed_error"] == pytest.approx(5.0)
    assert scoring["mae"] == pytest.approx(5.0)
    assert report["coverage"]["collapsed_duplicate_projection_rows"] == 1
    # A second actual for the same projection is a conflict, not an average.
    conflict = tmp_path / "conflict.sqlite"
    left = _save(clock, conflict, 2, book="draftkings", projected=10, line=5.5)
    right = _save(clock, conflict, 2, book="fanduel", projected=10, line=5.5)
    _settle(clock, conflict, left, 2, actual=13)
    _settle(clock, conflict, right, 2, actual=99)
    conflicted = analyze_archive(conflict)
    assert left in _reason_ids(conflicted, "projection_exclusions", "conflicting actuals")
    assert right in _reason_ids(conflicted, "projection_exclusions", "conflicting actuals")
    assert conflicted["questions"]["usable_for_projection_accuracy"] == 0


def test_over_and_under_are_two_probabilities_and_one_projection(tmp_path: Path, clock):
    database = tmp_path / "sides.sqlite"
    over = _save(clock, database, 2, is_over=True, projected=270, line=250.5)
    under = _save(clock, database, 2, is_over=False, projected=270, line=250.5)
    _settle(clock, database, over, 2, actual=280)
    _settle(clock, database, under, 2, actual=280)
    report = analyze_archive(database)
    probability = _find(report, "conditional_groups", market="PASS_YDS", line_family="half_point")[0]
    projection = _find(report, "projection_groups", market="PASS_YDS")[0]
    assert probability["sample_size"] == 2
    assert probability["event_count"] == 1
    assert probability["outcome_counts"] == {"win": 1, "loss": 1, "push": 0}
    assert any("not independent" in warning for warning in probability["warnings"])
    assert projection["sample_size"] == 1
    assert projection["collapsed_duplicate_rows"] == 1
    assert projection["mean_signed_error"] == pytest.approx(10.0)


def test_supplied_win_probability_is_not_a_stat_projection(tmp_path: Path, clock):
    database = tmp_path / "binary.sqlite"
    normal = _save(clock, database, 2, projected=270, line=250.5)
    supplied = _save(
        clock,
        database,
        4,
        eval_mode="binary_ml",
        projected=0.62,
        line=250.5,
        player_id="ex-binary",
    )
    _settle(clock, database, normal, 2, actual=280)
    _settle(clock, database, supplied, 4, actual=300)
    report = analyze_archive(database)
    half = _find(report, "conditional_groups", market="PASS_YDS", line_family="half_point")[0]
    assert supplied not in half["archive_ids"]
    assert normal in half["archive_ids"]
    binary_groups = _find(report, "unconditional_groups", line_family="binary_no_push")
    assert len(binary_groups) == 1
    assert binary_groups[0]["archive_ids"] == [supplied]
    assert binary_groups[0]["market"] == "PASS_YDS"
    assert supplied not in _ids(report, "projection_groups")
    assert supplied in _reason_ids(report, "projection_exclusions", "not a stat projection")
    assert supplied in _reason_ids(report, "conditional_exclusions", "conditional win probability was not stored")


def test_corrections_are_not_applied_and_the_snapshot_stays(tmp_path: Path, clock):
    database = tmp_path / "history.sqlite"
    archive_id = _save(clock, database, 2, projected=10, line=5.5)
    _settle(clock, database, archive_id, 2, actual=14)
    clock["moment"] = _when(3, 12)
    correction = add_correction(
        database,
        archive_id,
        field_name="projected_value",
        corrected_value="14",
        reason="unit-test note, not a real correction",
        source="unit test",
    )
    assert correction["ok"] is True
    before_sigma = dict(SIGMA_DEFAULTS)
    before_books = list(BOOKS)
    before = get_snapshot(database, archive_id)
    report = analyze_archive(database)
    after = get_snapshot(database, archive_id)
    assert after == before
    assert after["projected_value"] == 10.0
    assert after["qualified"] == 0
    assert SIGMA_DEFAULTS == before_sigma
    assert list(BOOKS) == before_books
    assert [key for key, _label in BOOKS] == ["fanduel_in", "draftkings", "bet365", "fanatics"]
    assert EDGE_THRESHOLD == pytest.approx(0.03)
    projection = report["slices"]["test"]["projection_groups"][0]
    assert projection["mean_signed_error"] == pytest.approx(4.0)
    assert report["corrections_not_applied"] == 1
    assert report["calibration_claimed"] is False
    assert report["qualified_gate_changed"] is False
    assert report["sigma_defaults_changed"] is False


def test_binary_probability_on_a_whole_line_is_not_mixed_in(tmp_path: Path, clock):
    database = tmp_path / "binary-whole.sqlite"
    archive_id = _save(clock, database, 2, eval_mode="binary_ml", projected=0.62, line=250)
    _settle(clock, database, archive_id, 2, actual=260)
    report = analyze_archive(database)
    assert archive_id not in _ids(report, "conditional_groups")
    assert archive_id not in _ids(report, "unconditional_groups")
    assert archive_id not in _ids(report, "projection_groups")
    assert archive_id in _reason_ids(
        report,
        "conditional_exclusions",
        "stored push handling does not match the market and line",
    )


def test_conflicting_unsuperseded_results_are_excluded(tmp_path: Path, clock):
    database = tmp_path / "ambiguous.sqlite"
    win_loss = _save(clock, database, 2, projected=10, line=5.5)
    stats = _save(clock, database, 4, projected=10, line=5.5)
    chained = _save(clock, database, 6, projected=10, line=5.5)
    retrieved = _iso(_when(2, 23))
    _insert_result(
        database,
        win_loss,
        "win-row",
        settlement="Win",
        actual=20,
        retrieved=retrieved,
        attached=_iso(_when(2, 23, 10)),
    )
    _insert_result(
        database,
        win_loss,
        "loss-row",
        settlement="Loss",
        actual=5,
        retrieved=retrieved,
        attached=_iso(_when(2, 23, 40)),
    )
    _insert_result(
        database,
        stats,
        "actual-low",
        settlement="Win",
        actual=11,
        retrieved=_iso(_when(4, 23)),
        attached=_iso(_when(4, 23, 10)),
    )
    _insert_result(
        database,
        stats,
        "actual-high",
        settlement="Win",
        actual=40,
        retrieved=_iso(_when(4, 23)),
        attached=_iso(_when(4, 23, 40)),
    )
    _settle(clock, database, chained, 6, actual=18)
    clock["moment"] = _when(7, 12)
    successor = attach_result(
        database,
        chained,
        game_status="final",
        result_source="unit-test fixture, a later result",
        result_retrieved_at=_iso(_when(7, 11)),
        actual=4,
        actual_provided=True,
    )
    assert successor["ok"] is True, successor
    assert successor["settlement"] == "Loss"
    before_conflict = get_snapshot(database, win_loss)
    before_stats = get_snapshot(database, stats)
    report = analyze_archive(database)
    assert get_snapshot(database, win_loss) == before_conflict
    assert get_snapshot(database, stats) == before_stats
    assert len(before_conflict["results"]) == 2
    assert len(before_stats["results"]) == 2
    for archive_id in (win_loss, stats):
        assert archive_id not in _ids(report, "conditional_groups")
        assert archive_id not in _ids(report, "unconditional_groups")
        assert archive_id not in _ids(report, "projection_groups")
        for track in (
            "conditional_exclusions",
            "unconditional_exclusions",
            "projection_exclusions",
        ):
            assert archive_id in _reason_ids(report, track, "more than one current result")
    assert report["ambiguous_snapshots"] == 2
    assert report["additional_unsuperseded_results_not_scored"] == 4
    assert report["superseded_results_not_scored"] == 1
    coverage = report["coverage"]
    assert coverage["usable_conditional_calibration"] + coverage["conditional_exclusions"]["rows"] == 3
    assert coverage["usable_projection_rows"] + coverage["projection_exclusions"]["rows"] == 3
    reason_rows = [
        item
        for item in coverage["conditional_exclusions"]["reasons"]
        if "more than one current result" in item["reason"]
    ]
    assert len(reason_rows) == 1
    assert reason_rows[0]["count"] == 2
    assert set(reason_rows[0]["archive_ids"]) == {win_loss, stats}
    scored = _find(report, "conditional_groups", market="PASS_YDS", line_family="half_point")
    assert len(scored) == 1
    assert scored[0]["archive_ids"] == [chained]
    assert scored[0]["outcome_counts"] == {"win": 0, "loss": 1, "push": 0}
    assert scored[0]["sample_size"] == 1
    projection = _find(report, "projection_groups", market="PASS_YDS")
    assert len(projection) == 1
    assert projection[0]["archive_ids"] == [chained]
    assert projection[0]["mean_signed_error"] == pytest.approx(-6.0)
    assert projection[0]["mae"] == pytest.approx(6.0)
    text = render_markdown(report)
    assert "more than one current result" in text
    assert "left unscored: 2" in text


def test_model_versions_stay_in_separate_groups(tmp_path: Path, clock):
    database = tmp_path / "versions.sqlite"
    current = _save(clock, database, 2, projected=10, line=5.5)
    _settle(clock, database, current, 2, actual=16)
    stored_version = get_snapshot(database, current)["model_version"]
    assert stored_version == MODEL_VERSION
    _insert_snapshot(
        database,
        current,
        archive_id="version-0-9",
        observation_key="version-0-9-observation",
        event_id="game-version-0-9",
        model_version="0.9.0",
    )
    _insert_snapshot(
        database,
        current,
        archive_id="version-blank",
        observation_key="version-blank-observation",
        event_id="game-version-blank",
        model_version="",
    )
    for archive_id, result_id, actual in (
        ("version-0-9", "result-0-9", 12),
        ("version-blank", "result-blank", 8),
    ):
        _insert_result(
            database,
            archive_id,
            result_id,
            settlement="Win",
            actual=actual,
            retrieved=_iso(_when(2, 23)),
            attached=_iso(_when(2, 23, 15)),
        )
    report = analyze_archive(database)
    text = render_markdown(report)
    for kind in ("conditional_groups", "projection_groups"):
        groups = _find(report, kind, market="PASS_YDS")
        by_version = {group["archived_model_version"]: group for group in groups}
        assert set(by_version) == {stored_version, "0.9.0", MODEL_VERSION_NOT_RECORDED}
        assert MODEL_VERSION_NOT_RECORDED == "not recorded"
        assert stored_version != MODEL_VERSION_NOT_RECORDED
        assert all(group["sample_size"] == 1 for group in groups)
        assert all(group["event_count"] == 1 for group in groups)
        assert sum(group["sample_size"] for group in groups) == 3
        assert by_version[stored_version]["archive_ids"] == [current]
        assert by_version["0.9.0"]["archive_ids"] == ["version-0-9"]
        assert by_version[MODEL_VERSION_NOT_RECORDED]["archive_ids"] == ["version-blank"]
    conditional = {
        group["archived_model_version"]: group
        for group in _find(report, "conditional_groups", market="PASS_YDS", line_family="half_point")
    }
    for version, group in conditional.items():
        assert group["line_family"] == "half_point"
        assert group["sport"] == "nfl"
        assert {book["sportsbook"] for book in group["sportsbooks"]} == {"draftkings"}
        assert group["sportsbooks"][0]["archived_model_version"] == version
        assert group["sportsbooks"][0]["line_family"] == "half_point"
        assert group["sportsbooks"][0]["sample_size"] == 1
    blank_projection = _find(report, "projection_groups", market="PASS_YDS")
    blank = next(group for group in blank_projection if group["archived_model_version"] == "not recorded")
    known = next(group for group in blank_projection if group["archived_model_version"] == "0.9.0")
    assert blank["mean_signed_error"] == pytest.approx(-2.0)
    assert known["mean_signed_error"] == pytest.approx(2.0)
    assert f"model {stored_version}" in text
    assert "model 0.9.0" in text
    assert "model not recorded" in text
    assert report["calibration_claimed"] is False


def test_cli_analyze_writes_the_report_and_leaves_existing_commands(tmp_path: Path, clock, capsys):
    database = tmp_path / "cli.sqlite"
    archive_id = _save(clock, database, 2, projected=270, line=250.5)
    _settle(clock, database, archive_id, 2, actual=280)
    out_dir = tmp_path / "out"
    assert main(["archive", "analyze", "--db", str(database), "--out", str(out_dir), "--format", "markdown"]) == 0
    written = (out_dir / "calibration_report.md").read_text(encoding="utf-8")
    payload = json.loads((out_dir / "calibration_report.json").read_text(encoding="utf-8"))
    assert "Predictions archived: **1**" in written
    assert "does not claim that the model is calibrated" in written
    assert "Profit reported: **no**" in written
    assert "sample size" in written
    assert payload["calibration_claimed"] is False
    assert payload["questions"]["settled"] == 1
    capsys.readouterr()
    assert main(["archive", "analyze", "--db", str(database), "--format", "json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["questions"]["usable_for_conditional_calibration"] == 1
    missing = tmp_path / "absent.sqlite"
    assert main(["archive", "analyze", "--db", str(missing), "--format", "json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["questions"]["predictions_archived"] == 0
    assert missing.exists() is False
    assert main(["archive", "stats", "--db", str(database)]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["calibration_claimed"] is False
    assert stats["snapshots"] == 1
    with pytest.raises(SystemExit) as exc:
        main(["archive", "analyze", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--threshold" not in help_text
