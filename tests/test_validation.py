"""Bad props are flagged. They do not get a made-up number, and they do not
stop a good prop that is evaluated next to them.
"""

from betting_model.sports import evaluate_prop


def _nfl(**overrides):
    prop = {
        "player": "Example Player",
        "sport": "nfl",
        "market": "passing_yards",
        "line": 250.5,
        "odds": -110,
        "is_over": True,
        "projected": 260.0,
    }
    prop.update(overrides)
    return prop


def test_string_false_is_not_treated_as_over():
    result = evaluate_prop(_nfl(is_over="false"))
    assert result["evaluated"] is True
    assert result["is_over"] is False
    assert result["model_prob"] < 0.5


def test_missing_required_fields_are_named():
    result = evaluate_prop({"sport": "nfl"})
    assert result["evaluated"] is False
    assert result["skipped"] is True
    text = result["skip_reason"]
    assert "player" in text
    assert "market" in text
    assert "line" in text
    assert "odds" in text


def test_bad_odds_do_not_crash_and_do_not_produce_an_edge():
    result = evaluate_prop(_nfl(odds=0))
    assert result["evaluated"] is False
    assert "odds" in result["skip_reason"].lower() or "american" in result["skip_reason"].lower()
    assert "edge" not in result


def test_one_bad_prop_does_not_change_a_good_one():
    good = evaluate_prop(_nfl())
    bad = evaluate_prop(_nfl(player="Other Player", odds="nope"))
    again = evaluate_prop(_nfl())
    assert bad["evaluated"] is False
    assert good["evaluated"] is True
    assert again["edge"] == good["edge"]
    assert again["model_prob"] == good["model_prob"]


def test_unknown_book_is_listed_and_known_books_still_score():
    prop = _nfl(
        books={
            "draftkings": {"line": 250.5, "odds": -110, "source": "fixture"},
            "not_a_book": {"line": 250.5, "odds": -110},
        }
    )
    result = evaluate_prop(prop)
    assert result["evaluated"] is True
    assert "not_a_book" in result["unsupported_books"]
    assert result["books"]["draftkings"]["evaluated"] is True
    assert result["books"]["fanduel_in"]["missing"] is True
    assert result["books"]["bet365"]["missing"] is True


def test_one_bad_book_does_not_drop_the_other_book():
    prop = _nfl(
        books={
            "draftkings": {"line": 250.5, "odds": -110, "source": "fixture"},
            "fanduel": {"line": 250.5, "odds": 0, "source": "fixture"},
        }
    )
    result = evaluate_prop(prop)
    assert result["books"]["draftkings"]["evaluated"] is True
    assert result["books"]["fanduel_in"]["evaluated"] is False
    assert result["evaluated"] is True
    assert result["best_book"] == "DraftKings"


def test_non_object_prop_is_skipped():
    result = evaluate_prop(["not", "a", "prop"])  # type: ignore[arg-type]
    assert result["skipped"] is True
    assert "JSON object" in result["skip_reason"]
