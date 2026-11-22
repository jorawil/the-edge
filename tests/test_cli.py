"""The command-line report stays runnable and says what the grades mean."""

import json
from pathlib import Path

import pytest

from betting_model.cli import evaluate_props_file, main, safe_save_name


def test_save_as_without_out_uses_the_default_directory(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    props = tmp_path / "props.json"
    props.write_text(
        json.dumps(
            [
                {
                    "player": "Example",
                    "sport": "nba",
                    "market": "PTS",
                    "line": 10.5,
                    "odds": -110,
                    "is_over": True,
                    "projected": 12,
                }
            ]
        ),
        encoding="utf-8",
    )
    code = main(["evaluate", "--props", str(props), "--save-as", "slate"])
    assert code == 0
    assert (tmp_path / "out" / "edges_latest.json").exists()
    assert (tmp_path / "out" / "slate.json").exists()
    assert (tmp_path / "out" / "slate.md").exists()


def test_string_false_is_labeled_under_in_the_report(tmp_path: Path):
    props = tmp_path / "props.json"
    props.write_text(
        json.dumps(
            {
                "props": [
                    {
                        "player": "Under Example",
                        "sport": "nfl",
                        "market": "rushing_yards",
                        "line": 50.5,
                        "odds": -110,
                        "is_over": "false",
                        "projected": 40,
                        "projection_source": "example only",
                        "projection_at": "2026-01-01T18:00:00+00:00",
                        "retrieved_at": "2026-01-01T18:00:00+00:00",
                    },
                    {
                        "player": "Bad Under",
                        "sport": "nfl",
                        "market": "rushing_yards",
                        "line": 40.5,
                        "odds": "nope",
                        "is_over": "false",
                        "projected": 30,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = evaluate_props_file(props, tmp_path / "out")
    text = Path(payload["out_md"]).read_text(encoding="utf-8")
    assert "| Under Example | rushing_yards | Under |" in text
    assert "| Under Example | rushing_yards | Over |" not in text
    assert "Bad Under" in text
    assert "U40.5" in text
    assert "O40.5" not in text


def test_string_false_is_labeled_under_in_the_grade_table():
    from betting_model.reports import _grade_table

    lines = _grade_table(
        [
            {
                "player": "String Under",
                "market": "PTS",
                "is_over": "false",
                "line": 10.5,
                "odds": -110,
                "projected": 8,
                "status": "WATCH",
                "status_reasons": ["example"],
            }
        ]
    )
    assert any("| String Under | PTS | Under |" in line for line in lines)
    assert not any("| String Under | PTS | Over |" in line for line in lines)


def test_save_as_rejects_paths():
    with pytest.raises(ValueError):
        safe_save_name("../escape")
    with pytest.raises(ValueError):
        safe_save_name("nested/name")
    assert safe_save_name("saturday") == "saturday"


def test_report_names_grades_and_keeps_a_bad_prop_from_erasing_a_good_one(tmp_path: Path):
    props = {
        "research_at": "2026-01-01T17:00:00+00:00",
        "props": [
            {
                "player": "Good Example",
                "sport": "nfl",
                "market": "rushing_yards",
                "line": 50.5,
                "odds": -110,
                "is_over": True,
                "projected": 80,
                "source": "example only",
            },
            {
                "player": "Bad Example",
                "sport": "nfl",
                "market": "rushing_yards",
                "line": 50.5,
                "odds": "nope",
                "is_over": "false",
                "projected": 80,
            },
        ],
    }
    path = tmp_path / "props.json"
    path.write_text(json.dumps(props), encoding="utf-8")
    payload = evaluate_props_file(path, tmp_path / "out")
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["qualified"] == 0
    text = Path(payload["out_md"]).read_text(encoding="utf-8")
    assert "not a lock" in text
    assert "recent average" in text
    assert "vig" in text
    assert "Good Example" in text
    assert "Bad Example" in text
    saved = json.loads(Path(payload["out_json"]).read_text(encoding="utf-8"))
    statuses = {row["player"]: row["status"] for row in saved["all"]}
    assert statuses["Bad Example"] == "NOT_EVALUABLE"
    assert statuses["Good Example"] in {"WATCH", "REJECTED"}


def test_cli_writes_named_copy(tmp_path: Path, monkeypatch):
    props = tmp_path / "props.json"
    props.write_text(
        json.dumps(
            [
                {
                    "player": "Example",
                    "sport": "nba",
                    "market": "PTS",
                    "line": 10.5,
                    "odds": -110,
                    "is_over": True,
                    "projected": 12,
                }
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    code = main(["evaluate", "--props", str(props), "--out", str(out), "--save-as", "slate"])
    assert code == 0
    assert (out / "slate.md").exists()
    monkeypatch.chdir(tmp_path)
    assert main(["evaluate", "--props", str(props), "--save-as", "../nope"]) == 2
