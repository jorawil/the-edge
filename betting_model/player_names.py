"""Player-name matching that refuses a guess.

A player id matches only that id. A name matches one person after accents,
punctuation, and a trailing generational suffix are folded. A first initial
matches only when the rest of the name matches one player. A last name alone
never matches.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "junior", "senior"}


def normalize_label(value: object) -> str:
    """Fold case, accents, and punctuation. Empty input stays empty."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value)


def _core(spaced: str) -> str:
    parts = spaced.split()
    while len(parts) >= 2 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


def _forms(value: object) -> dict[str, str]:
    spaced = normalize_label(value)
    return {"spaced": spaced, "compact": _compact(spaced), "core": _core(spaced)}


def _candidate_name(candidate: dict[str, Any]) -> object:
    return candidate.get("name") or candidate.get("player") or candidate.get("player_name")


def _candidate_id(candidate: dict[str, Any]) -> str | None:
    for key in ("player_id", "athlete_id"):
        value = candidate.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def match_players(
    name: object,
    player_id: object,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return one candidate index, or a reason not to choose.

    ``ok`` is true only for a single match. ``ambiguous`` means more than one
    candidate fit and none was selected.
    """
    if player_id not in (None, ""):
        wanted = str(player_id)
        hits = [index for index, candidate in enumerate(candidates) if _candidate_id(candidate) == wanted]
        if len(hits) == 1:
            return {"ok": True, "index": hits[0], "method": "player_id", "ambiguous": False}
        if len(hits) > 1:
            return {
                "ok": False,
                "index": None,
                "method": None,
                "ambiguous": True,
                "reason": "player id matched more than one row, so nobody was chosen",
            }
        return {
            "ok": False,
            "index": None,
            "method": None,
            "ambiguous": False,
            "reason": "player id was not in this event, so the name was not used instead",
        }

    query = _forms(name)
    if not query["spaced"]:
        return {
            "ok": False,
            "index": None,
            "method": None,
            "ambiguous": False,
            "reason": "player name is missing",
        }
    forms = [_forms(_candidate_name(candidate)) for candidate in candidates]

    def hits(predicate) -> list[int]:
        return [index for index, form in enumerate(forms) if predicate(form)]

    exact = hits(
        lambda form: form["spaced"] == query["spaced"]
        or (query["compact"] != "" and form["compact"] == query["compact"])
    )
    if len(exact) == 1:
        return {"ok": True, "index": exact[0], "method": "normalized_name", "ambiguous": False}
    if len(exact) > 1:
        return {
            "ok": False,
            "index": None,
            "method": None,
            "ambiguous": True,
            "reason": "more than one player has this name, so nobody was chosen",
        }

    if " " in query["core"]:
        loosened = hits(lambda form: form["core"] == query["core"] and " " in form["core"])
        if len(loosened) == 1:
            return {"ok": True, "index": loosened[0], "method": "name_without_suffix", "ambiguous": False}
        if len(loosened) > 1:
            return {
                "ok": False,
                "index": None,
                "method": None,
                "ambiguous": True,
                "reason": "more than one player matches after the suffix is ignored, so nobody was chosen",
            }

    parts = query["core"].split()
    if len(parts) >= 2 and len(parts[0]) == 1:
        rest = parts[1:]

        def initial(form: dict[str, str]) -> bool:
            candidate_parts = form["core"].split()
            if len(candidate_parts) < 2 or candidate_parts[1:] != rest:
                return False
            first = candidate_parts[0]
            return len(first) > 1 and first.startswith(parts[0])

        initials = hits(initial)
        if len(initials) == 1:
            return {
                "ok": True,
                "index": initials[0],
                "method": "first_initial_and_last_name",
                "ambiguous": False,
            }
        if len(initials) > 1:
            return {
                "ok": False,
                "index": None,
                "method": None,
                "ambiguous": True,
                "reason": "more than one player matches that initial and last name, so nobody was chosen",
            }

    def candidate_is_initial(form: dict[str, str]) -> bool:
        candidate_parts = form["core"].split()
        query_parts = query["core"].split()
        if len(candidate_parts) < 2 or len(query_parts) < 2 or len(candidate_parts[0]) != 1:
            return False
        if candidate_parts[1:] != query_parts[1:]:
            return False
        return len(query_parts[0]) > 1 and query_parts[0].startswith(candidate_parts[0])

    reversed_initials = hits(candidate_is_initial)
    if len(reversed_initials) == 1:
        return {
            "ok": True,
            "index": reversed_initials[0],
            "method": "first_initial_and_last_name",
            "ambiguous": False,
        }
    if len(reversed_initials) > 1:
        return {
            "ok": False,
            "index": None,
            "method": None,
            "ambiguous": True,
            "reason": "more than one player matches that initial and last name, so nobody was chosen",
        }

    return {
        "ok": False,
        "index": None,
        "method": None,
        "ambiguous": False,
        "reason": "player name did not match one person in this event",
    }
