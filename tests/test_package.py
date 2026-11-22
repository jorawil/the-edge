"""Package layout: imports and paths work without the old CI rename hack."""

from pathlib import Path

import pytest

import betting_model
from betting_model.cli import main
from betting_model.data_store import DATA_DIR


def test_public_imports():
    assert betting_model.implied_probability(-110) == pytest.approx(110 / 210)
    assert betting_model.__version__ == "0.2.0"


def test_data_dir_is_inside_the_repo():
    repo = Path(__file__).resolve().parents[1]
    assert DATA_DIR == repo / "data"


def test_backfill_script_writes_inside_the_repo():
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "backfill_espn.py"
    spec = importlib.util.spec_from_file_location("backfill_espn", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DATA == Path(__file__).resolve().parents[1] / "data"


def test_cli_help_exits_cleanly():
    with pytest.raises(SystemExit) as exc:
        main(["evaluate", "--help"])
    assert exc.value.code == 0
