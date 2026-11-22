"""The command-line report stays runnable and says what the grades mean."""

import json
from pathlib import Path

import pytest

from betting_model.cli import evaluate_props_file, main, safe_save_name


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
