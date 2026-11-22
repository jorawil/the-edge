"""Pregame archive integrity. Fixtures are not historical projections or prices."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from betting_model.archive import (
    SCHEMA_VERSION,
    add_correction,
    archive_evaluation,
    archive_many,
    archive_stats,
    attach_result,
    export_archive,
    get_snapshot,
    import_archive,
    list_snapshots,
    list_ungraded,
    migrate,
    pregame_view,
    schema_version,
)
from betting_model.cli import main

NOW = datetime(2026, 11, 23, 17, 5, tzinfo=timezone.utc)
START = "2026-11-23T18:00:00Z"
PROJECTED_AT = "2026-11-23T17:00:00Z"
PRICE_AT = "2026-11-23T17:00:30Z"


def _prop(**overrides):
    prop = {
        "player": "Example Player",
        "player_id": "ex-1",
        "sport": "nfl",
        "league": "NFL",
        "event_id": "game-1",
        "event_start": START,
        "team": "HOM",
        "opponent": "AWY",
        "market": "passing_yards",
        "is_over": True,
        "line": 250.5,
        "odds": -110,
        "projected": 270.0,
        "projection_source": "example desk note, not a real forecast",
        "projection_at": PROJECTED_AT,
        "retrieved_at": PRICE_AT,
        "book": "draftkings",
        "bookmaker_market_id": "mkt-1",
    }
    prop.update(overrides)
    return prop


def test_snapshot_stores_the_supplied_pregame_record(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    saved = archive_evaluation(database, _prop(), now=NOW)
    assert saved["ok"] is True
    row = get_snapshot(database, saved["archive_id"])
    assert row["projected_value"] == 270.0
    assert row["american_odds"] == -110
    assert row["line"] == 250.5
    assert row["projection_source"].startswith("example desk")
    assert row["projection_created_at"] == "2026-11-23T17:00:00Z"
    assert row["projection_created_at_source"] == PROJECTED_AT
    assert row["odds_retrieved_at_source"] == PRICE_AT
    assert row["archived_at"] == "2026-11-23T17:05:00Z"
    assert row["legacy_edge"] is not None
    assert row["qualified"] == 0
    assert row["screening_grade"] != "QUALIFIED"
    assert row["region_verified"] == 0
    assert "actual" not in json.loads(row["payload_json"])


def test_later_price_is_a_new_snapshot_and_the_first_is_unchanged(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    first = archive_evaluation(database, _prop(), now=NOW)
    second = archive_evaluation(
        database,
        _prop(line=255.5, odds=-115, retrieved_at="2026-11-23T17:30:00Z"),
        now=NOW + timedelta(minutes=30),
    )
    third = archive_evaluation(
        database,
        _prop(odds=100, retrieved_at="2026-11-23T17:40:00Z"),
        now=NOW + timedelta(minutes=40),
    )
    assert first["ok"] and second["ok"] and third["ok"]
    ids = {first["archive_id"], second["archive_id"], third["archive_id"]}
    assert len(ids) == 3
    original = get_snapshot(database, first["archive_id"])
    assert original["line"] == 250.5
    assert original["american_odds"] == -110
    listed = list_snapshots(database, player="Example Player", event_id="game-1")
    assert len(listed) == 3


def test_exact_resubmit_does_not_change_the_original(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    first = archive_evaluation(database, _prop(), now=NOW)
    before = get_snapshot(database, first["archive_id"])
    duplicate = archive_evaluation(database, _prop(), now=NOW + timedelta(minutes=1))
    assert duplicate["ok"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["archive_id"] == first["archive_id"]
    after = get_snapshot(database, first["archive_id"])
    assert after["payload_json"] == before["payload_json"]
    assert after["american_odds"] == -110
    assert after["projected_value"] == 270.0


def test_missing_and_invalid_fields_are_not_invented(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    refused = archive_evaluation(database, _prop(projected=None, odds=None), now=NOW)
    assert refused["stored"] is False
    assert "projected" in refused["reason"]
    assert list_snapshots(database) == []
    bad_odds = archive_evaluation(database, _prop(odds=0), now=NOW)
    assert bad_odds["stored"] is False
    leaked = archive_evaluation(database, _prop(actual=30), now=NOW)
    assert leaked["stored"] is False
    assert "postgame" in leaked["reason"]
    assert list_snapshots(database) == []


def test_utc_storage_and_no_backdating(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    saved = archive_evaluation(
        database,
        _prop(event_start="2026-11-23T13:00:00-05:00"),
        now=NOW,
    )
    row = get_snapshot(database, saved["archive_id"])
    assert row["event_start"] == "2026-11-23T18:00:00Z"
    assert row["event_start_source"] == "2026-11-23T13:00:00-05:00"
    late = archive_evaluation(
        database,
        _prop(event_id="already-started", event_start="2026-11-23T17:00:00Z"),
        now=NOW,
    )
    assert late["stored"] is False
    assert "already started" in late["reason"]
    backdated = archive_evaluation(
        database,
        _prop(
            event_id="old-stamp",
            projection_at="2026-11-20T12:00:00Z",
            retrieved_at="2026-11-23T17:00:00Z",
        ),
        now=NOW,
    )
    assert backdated["stored"] is False
    assert "backdated" in backdated["reason"]
    naive = archive_evaluation(database, _prop(event_id="naive", projection_at="2026-11-23T17:00:00"), now=NOW)
    assert naive["stored"] is False
    assert "timezone" in naive["reason"]


def test_string_false_is_under_and_fanduel_is_not_indiana(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    saved = archive_evaluation(
        database,
        _prop(is_over="false", book="fanduel_in", projected=200),
        now=NOW,
    )
    row = get_snapshot(database, saved["archive_id"])
    assert row["side"] == "Under"
    assert row["region_verified"] == 0
    assert "Indiana" in row["region_note"]


def test_result_attachment_does_not_edit_the_snapshot(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    saved = archive_evaluation(database, _prop(), now=NOW)
    before = get_snapshot(database, saved["archive_id"])["payload_json"]
    graded = attach_result(
        database,
        saved["archive_id"],
        game_status="final",
        actual=280,
        actual_provided=True,
        result_source="fixture box score",
        result_retrieved_at="2026-11-24T02:00:00Z",
        now=datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc),
        event_id="game-1",
        player_id="ex-1",
    )
    assert graded["settlement"] == "Win"
    after = get_snapshot(database, saved["archive_id"])
    assert after["payload_json"] == before
    assert after["projected_value"] == 270.0
    assert after["american_odds"] == -110
    assert pregame_view(after)["results"] if False else "results" not in pregame_view(after)
    mismatch = attach_result(
        database,
        saved["archive_id"],
        game_status="final",
        actual=10,
        actual_provided=True,
        result_source="wrong game",
        result_retrieved_at="2026-11-24T02:00:00Z",
        now=datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc),
        event_id="other-game",
    )
    assert mismatch["ok"] is False


def test_win_loss_push_void_unknown_and_a_real_zero(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    over = archive_evaluation(database, _prop(event_id="over"), now=NOW)
    under = archive_evaluation(
        database, _prop(event_id="under", is_over="false", projected=200), now=NOW
    )
    push = archive_evaluation(
        database, _prop(event_id="push", market="PTS", line=20, projected=21, sport="nba"), now=NOW
    )
    dnp = archive_evaluation(database, _prop(event_id="dnp"), now=NOW)
    missing = archive_evaluation(database, _prop(event_id="missing"), now=NOW)
    zero = archive_evaluation(
        database, _prop(event_id="zero", market="PTS", line=0.5, projected=1, sport="nba"), now=NOW
    )
    when = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    common = {
        "result_source": "fixture",
        "result_retrieved_at": "2026-11-24T02:00:00Z",
        "now": when,
    }
    assert attach_result(database, over["archive_id"], game_status="final", actual=240, actual_provided=True, **common)["settlement"] == "Loss"
    assert attach_result(database, under["archive_id"], game_status="final", actual=240, actual_provided=True, **common)["settlement"] == "Win"
    assert attach_result(database, push["archive_id"], game_status="final", actual=20, actual_provided=True, **common)["settlement"] == "Push"
    dnp_result = attach_result(database, dnp["archive_id"], game_status="dnp", actual=0, actual_provided=True, **common)
    assert dnp_result["settlement"] == "Void"
    stored = get_snapshot(database, dnp["archive_id"])["results"][0]
    assert stored["actual_stat"] is None
    unknown = attach_result(database, missing["archive_id"], game_status="final", **common)
    assert unknown["settlement"] == "Unknown"
    assert get_snapshot(database, missing["archive_id"])["results"][0]["actual_stat"] is None
    zero_result = attach_result(database, zero["archive_id"], game_status="final", actual=0, actual_provided=True, **common)
    assert zero_result["settlement"] == "Loss"
    assert get_snapshot(database, zero["archive_id"])["results"][0]["actual_stat"] == 0
    postponed = archive_evaluation(database, _prop(event_id="postponed"), now=NOW)
    canceled = archive_evaluation(database, _prop(event_id="canceled"), now=NOW)
    assert attach_result(database, postponed["archive_id"], game_status="postponed", **common)["settlement"] == "Void"
    assert attach_result(database, canceled["archive_id"], game_status="canceled", **common)["settlement"] == "Void"


def test_second_result_and_correction_keep_the_original(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    saved = archive_evaluation(database, _prop(), now=NOW)
    when = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    attach_result(
        database,
        saved["archive_id"],
        game_status="final",
        result_source="incomplete box",
        result_retrieved_at="2026-11-24T01:00:00Z",
        now=when,
    )
    attach_result(
        database,
        saved["archive_id"],
        game_status="final",
        actual=300,
        actual_provided=True,
        result_source="repaired box",
        result_retrieved_at="2026-11-24T04:00:00Z",
        now=when + timedelta(hours=2),
    )
    snapshot = get_snapshot(database, saved["archive_id"])
    assert len(snapshot["results"]) == 2
    assert snapshot["results"][0]["settlement"] == "Unknown"
    assert snapshot["results"][1]["supersedes_result_id"] == snapshot["results"][0]["result_id"]
    assert snapshot["projected_value"] == 270.0
    add_correction(
        database,
        saved["archive_id"],
        field_name="team",
        corrected_value="AWY",
        reason="example correction, original team was HOM",
        now=when,
    )
    again = get_snapshot(database, saved["archive_id"])
    assert again["team"] == "HOM"
    assert again["corrections"][0]["corrected_value"] == "AWY"


def test_export_import_and_schema_migration(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    archive_evaluation(database, _prop(), now=NOW)
    destination = tmp_path / "export.json"
    exported = export_archive(database, destination)
    assert exported["snapshots"] == 1
    other = tmp_path / "restored.sqlite"
    imported = import_archive(other, destination)
    assert imported["inserted"] == 1
    again = import_archive(other, destination)
    assert again["inserted"] == 0
    assert again["skipped_existing"] == 1
    original = list_snapshots(database)[0]
    restored = list_snapshots(other)[0]
    assert restored["payload_json"] == original["payload_json"]
    assert restored["american_odds"] == original["american_odds"]
    csv_path = tmp_path / "export.csv"
    assert export_archive(database, csv_path, file_format="csv")["ok"] is True
    assert "american_odds" in csv_path.read_text(encoding="utf-8").splitlines()[0]

    fresh = sqlite3.connect(tmp_path / "migrate.sqlite")
    assert schema_version(fresh) == 0
    assert migrate(fresh, target=1) == 1
    tables = {row[0] for row in fresh.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "snapshots" in tables
    assert "corrections" not in tables
    assert migrate(fresh) == SCHEMA_VERSION
    tables = {row[0] for row in fresh.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "corrections" in tables
    fresh.close()


def test_stats_do_not_claim_accuracy_and_one_bad_prop_does_not_drop_a_good_one(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    results = archive_many(database, [_prop(), {"sport": "nfl"}], now=NOW)
    assert results[0]["stored"] is True
    assert results[1]["stored"] is False
    stats = archive_stats(database)
    assert stats["snapshots"] == 1
    assert stats["qualified"] == 0
    assert stats["calibration_claimed"] is False
    assert stats["predictive_accuracy_claimed"] is False
    assert len(list_ungraded(database)) == 1


def test_cli_archive_add_and_attach(tmp_path: Path, monkeypatch):
    props = tmp_path / "pregame.json"
    props.write_text(json.dumps(_prop()), encoding="utf-8")
    database = tmp_path / "archive.sqlite"
    monkeypatch.setattr("betting_model.cli.archive_many", lambda path, props, now=None: archive_many(path, props, now=NOW))
    code = main(["archive", "add", "--props", str(props), "--db", str(database)])
    assert code == 0
    row = list_snapshots(database)[0]
    attach_code = main(
        [
            "archive",
            "attach-result",
            "--db",
            str(database),
            "--id",
            row["archive_id"],
            "--game-status",
            "final",
            "--actual",
            "0",
            "--source",
            "fixture",
            "--retrieved-at",
            "2026-11-22T12:00:00Z",
        ]
    )
    assert attach_code == 0
    assert get_snapshot(database, row["archive_id"])["results"][0]["actual_stat"] == 0.0


def test_snapshot_update_is_rejected_by_the_database(tmp_path: Path):
    database = tmp_path / "archive.sqlite"
    saved = archive_evaluation(database, _prop(), now=NOW)
    conn = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE snapshots SET projected_value = 1 WHERE archive_id = ?",
            (saved["archive_id"],),
        )
    conn.close()
