"""Collect real pregame props into the existing archive.

This module does not invent a projection or a price. It does not call the
historical odds endpoints. Those are paid, and a price captured after the
game would violate the archive's backdating rules. Current Odds API calls
go through ``odds.fetch_event_player_props`` only when a live collect is
asked for.

A FanDuel price from that feed is stored as ``fanduel``. It is stored as
FanDuel Indiana only when the prop itself has a verified Indiana region and
a source for that verification. A stale or unverified provider timestamp is
rejected. The archive still applies its own clock, kickoff, and duplicate
rules. One rejection does not drop the rest of the batch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from . import archive as archive_mod
from .core import canonical_market, parse_side
from .journal import Journal, default_journal_path
from .odds import fetch_event_player_props
from .player_names import match_players
from .result_sources import starts_match
from .version import MODEL_VERSION

logger = logging.getLogger(__name__)

_INTERNAL = {
    "freshness",
    "freshness_detail",
    "duplicate",
    "supported_shape",
    "fair_market_probability",
    "fair_market_reason",
    "overround",
    "source_note",
    "provider_last_update",
    "bookmaker_key",
    "book_display",
    "american_odds",
    "commence_time",
    "records",
    "_collection_raw",
    "raw",
    "region_note",
}


def _claims_indiana(value: object) -> bool:
    text = " ".join(str(value or "").casefold().replace("-", " ").replace("_", " ").split())
    return text in {
        "fanduel in",
        "fanduel indiana",
        "fd in",
        "fan duel in",
        "fan duel indiana",
    }


def _indiana_verified(prop: dict[str, Any]) -> bool:
    if prop.get("region_verified") is not True:
        return False
    if not str(prop.get("region") or "").strip() or not str(prop.get("region_source") or "").strip():
        return False
    region = " ".join(str(prop.get("region")).casefold().replace("-", " ").replace("_", " ").split())
    return region in {"in", "indiana", "us in"}


def prepare_sportsbook(prop: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Copy a prop. An unverified Indiana label is stored as FanDuel."""
    cleaned = {key: value for key, value in prop.items() if key not in _INTERNAL}
    book = str(cleaned.get("book") or cleaned.get("sportsbook") or "")
    if not _claims_indiana(book):
        return cleaned, None
    if _indiana_verified(prop):
        return cleaned, None
    cleaned["book"] = "fanduel"
    cleaned["sportsbook"] = "fanduel"
    cleaned["region_verified"] = False
    return cleaned, "FanDuel was not stored as FanDuel Indiana because the location was not verified"


def _precheck(prop: dict[str, Any]) -> str | None:
    freshness = prop.get("freshness")
    if freshness in (None, "fresh"):
        return None
    if freshness == "stale":
        return str(prop.get("freshness_detail") or "price is stale")
    if freshness == "unverified":
        return str(prop.get("freshness_detail") or "price timestamp could not be verified")
    return f"price freshness {freshness!r} is not a known fresh price"


def _player(prop: dict[str, Any]) -> str | None:
    value = prop.get("player") or prop.get("player_name")
    return None if value in (None, "") else str(value)


def _side(prop: dict[str, Any]) -> str | None:
    try:
        return "Over" if parse_side(prop.get("is_over") if prop.get("is_over") is not None else prop.get("side")) else "Under"
    except ValueError:
        return None


def _same_offer(projection: dict[str, Any], price: dict[str, Any]) -> bool:
    if not price.get("event_id"):
        return False
    if str(price.get("event_id")) != str(projection.get("event_id") or projection.get("game_id") or ""):
        return False
    left = canonical_market(str(projection.get("market") or ""))
    right = canonical_market(str(price.get("market") or ""))
    if not left or left != right:
        return False
    return _side(projection) is not None and _side(projection) == _side(price)


def _names_agree(left: object, right: object) -> bool:
    return match_players(left, None, [{"name": right}]).get("ok") is True


def _prop_from_pair(projection: dict[str, Any], price: dict[str, Any]) -> dict[str, Any]:
    """Build one archive prop, or a rejection record when the pair cannot be stored."""
    start = projection.get("event_start") or projection.get("start_time") or price.get("commence_time")
    supplied_start = projection.get("event_start") or projection.get("start_time")
    if supplied_start and price.get("commence_time") and not starts_match(supplied_start, price.get("commence_time")):
        return {
            "action": "rejected",
            "reason": "event start does not match the sportsbook commence time",
            "player": _player(projection),
            "market": projection.get("market"),
            "sportsbook": price.get("bookmaker_key"),
            "event_id": price.get("event_id"),
            "sport": projection.get("sport"),
            "raw": {"projection": projection, "price": price},
        }
    book = str(price.get("bookmaker_key") or price.get("book") or "")
    prop = {
        "player": _player(projection),
        "player_id": projection.get("player_id"),
        "sport": projection.get("sport"),
        "league": projection.get("league"),
        "event_id": projection.get("event_id") or projection.get("game_id"),
        "event_start": start,
        "team": projection.get("team"),
        "opponent": projection.get("opponent"),
        "market": projection.get("market"),
        "is_over": projection.get("is_over") if projection.get("is_over") is not None else projection.get("side"),
        "line": price.get("line"),
        "odds": price.get("american_odds") if price.get("american_odds") is not None else price.get("odds"),
        "projected": projection.get("projected"),
        "projection_source": projection.get("projection_source") or projection.get("source"),
        "projection_at": projection.get("projection_at") or projection.get("generated_at"),
        "projection_input_at": projection.get("projection_input_at"),
        "retrieved_at": price.get("retrieved_at") or price.get("odds_retrieved_at"),
        "book": book,
        "odds_provider": price.get("provider") or "the_odds_api",
        "region_verified": False,
        "freshness": price.get("freshness"),
        "freshness_detail": price.get("freshness_detail"),
        "bookmaker_market_id": price.get("market"),
        "_collection_raw": {"projection": projection, "price": price},
    }
    if prop["line"] is None or prop["odds"] is None:
        return {
            "action": "rejected",
            "reason": "price is missing a line or American odds",
            "player": prop["player"],
            "market": prop["market"],
            "sportsbook": book,
            "event_id": prop["event_id"],
            "sport": prop["sport"],
            "raw": prop["_collection_raw"],
        }
    return prop


def assemble_prices(
    projections: list[Any],
    prices: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join supplied projections to supplied prices. Missing prices stay missing.

    Each fresh book, line, and price becomes its own prop. A price that fits
    two projections is not given to either one.
    """
    usable: list[dict[str, Any]] = []
    extras: list[dict[str, Any]] = []
    for price in prices:
        if not isinstance(price, dict):
            extras.append({"action": "rejected", "reason": "price record must be an object", "player": None})
            continue
        if price.get("supported_shape") is False:
            extras.append(
                {
                    "action": "rejected",
                    "reason": price.get("reason") or "price was not an Over/Under price",
                    "player": price.get("player"),
                    "market": price.get("market"),
                    "sportsbook": price.get("bookmaker_key"),
                    "event_id": price.get("event_id"),
                    "raw": price,
                }
            )
            continue
        if price.get("duplicate") is True:
            extras.append(
                {
                    "action": "duplicate",
                    "reason": "the odds payload repeated this price and the extra copy was not archived",
                    "player": price.get("player"),
                    "market": price.get("market"),
                    "sportsbook": price.get("bookmaker_key"),
                    "event_id": price.get("event_id"),
                    "raw": price,
                }
            )
            continue
        usable.append(price)

    projection_rows = [row for row in projections if isinstance(row, dict)]
    for row in projections:
        if not isinstance(row, dict):
            extras.append({"action": "rejected", "reason": "projection must be an object", "player": None})

    paired: dict[int, list[dict[str, Any]]] = {id(row): [] for row in projection_rows}
    attempted: set[int] = set()
    for price in usable:
        fits = [
            row
            for row in projection_rows
            if _same_offer(row, price) and _names_agree(price.get("player"), _player(row))
        ]
        if len(fits) > 1:
            extras.append(
                {
                    "action": "conflict",
                    "reason": "this price matched more than one projection, so it was not assigned",
                    "player": price.get("player"),
                    "market": price.get("market"),
                    "sportsbook": price.get("bookmaker_key"),
                    "event_id": price.get("event_id"),
                    "raw": price,
                }
            )
            continue
        if len(fits) == 0:
            extras.append(
                {
                    "action": "rejected",
                    "reason": "no projection matched this price, so the price was not archived",
                    "player": price.get("player"),
                    "market": price.get("market"),
                    "sportsbook": price.get("bookmaker_key"),
                    "event_id": price.get("event_id"),
                    "raw": price,
                }
            )
            continue
        attempted.add(id(fits[0]))
        built = _prop_from_pair(fits[0], price)
        if built.get("action"):
            extras.append(built)
            continue
        paired[id(fits[0])].append(built)

    props: list[dict[str, Any]] = []
    for projection in projection_rows:
        built_rows = paired[id(projection)]
        if built_rows:
            props.extend(built_rows)
            continue
        if id(projection) in attempted:
            continue
        extras.append(
            {
                "action": "rejected",
                "reason": "no sportsbook price for this player, market, side, and event",
                "player": _player(projection),
                "market": projection.get("market"),
                "event_id": projection.get("event_id") or projection.get("game_id"),
                "sport": projection.get("sport"),
                "raw": {"projection": projection},
            }
        )
    return props, extras


def _record(journal: Journal, run_id: str, action: str, prop: dict[str, Any] | None, **fields: Any) -> None:
    source = prop or {}
    journal.add(
        run_id,
        action=action,
        sport=fields.get("sport", source.get("sport")),
        sportsbook=fields.get("sportsbook", source.get("book") or source.get("sportsbook")),
        event_id=fields.get("event_id", source.get("event_id") or source.get("game_id")),
        player=fields.get("player", _player(source)),
        market=fields.get("market", source.get("market")),
        archive_id=fields.get("archive_id"),
        reason=fields.get("reason"),
        raw=fields.get("raw", source.get("_collection_raw") or source),
    )


def collect_pregame(
    database: Path,
    props: list[Any],
    *,
    journal_path: Path | None = None,
    extras: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Archive a batch. The journal stores the original prop, including a relabeled book."""
    journal = Journal(journal_path)
    run_id = journal.start_run("collect")
    try:
        summary = _collect(database, props, journal=journal, run_id=run_id, extras=extras or [])
        summary["run_id"] = run_id
        summary["model_version"] = MODEL_VERSION
        summary["calibration_claimed"] = False
        summary["profit_reported"] = False
        summary["automatic_betting"] = False
        run_status = (
            "completed" if summary.get("ok") is True
            else "partial" if summary.get("stored", 0) or summary.get("duplicates", 0)
            else "failed"
        )
        journal.finish_run(run_id, run_status, {key: value for key, value in summary.items() if key != "items"})
        logger.info(
            "collection stored=%s duplicates=%s rejected=%s conflicts=%s",
            summary["stored"],
            summary["duplicates"],
            summary["rejected"],
            summary["conflicts"],
        )
        return summary
    except Exception:
        journal.finish_run(run_id, "failed", {"ok": False, "provider_failures": 1})
        raise


def _collect(
    database: Path,
    props: list[Any],
    *,
    journal: Journal,
    run_id: str,
    extras: list[dict[str, Any]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    ready_meta: list[dict[str, Any]] = []
    for extra in extras:
        payload = dict(extra)
        action = payload.pop("action", None) or "rejected"
        _record(journal, run_id, action, None, **payload)
        items.append({"action": action, "reason": extra.get("reason"), "player": extra.get("player")})
        logger.warning("collection %s: %s", action, extra.get("reason"))
    for prop in props:
        if not isinstance(prop, dict):
            reason = "prop must be an object"
            journal.add(run_id, action="rejected", reason=reason)
            items.append({"action": "rejected", "reason": reason})
            continue
        reason = _precheck(prop)
        if reason:
            action = "skipped" if "already started" in reason else "rejected"
            _record(journal, run_id, action, prop, reason=reason)
            items.append({"action": action, "reason": reason, "player": _player(prop)})
            logger.warning("collection %s %s: %s", action, _player(prop), reason)
            continue
        cleaned, note = prepare_sportsbook(prop)
        raw = prop.get("_collection_raw") or prop
        if note:
            raw = {"submitted": raw, "storage_note": note}
        ready.append(cleaned)
        ready_meta.append({"raw": raw, "note": note, "player": _player(prop)})
    archived = archive_mod.archive_many(Path(database), ready) if ready else []
    for meta, result in zip(ready_meta, archived):
        reason = result.get("reason")
        if result.get("stored"):
            action = "stored"
        elif result.get("duplicate"):
            action = "duplicate"
        elif reason and "already started" in str(reason):
            action = "skipped"
        else:
            action = "rejected"
        if meta["note"] and action == "stored":
            reason = meta["note"]
        index = result.get("index")
        origin = ready[index] if isinstance(index, int) and index < len(ready) else {}
        journal.add(
            run_id,
            action=action,
            archive_id=result.get("archive_id"),
            player=result.get("player") or meta["player"],
            reason=reason,
            sport=origin.get("sport"),
            sportsbook=origin.get("book") or origin.get("sportsbook"),
            event_id=origin.get("event_id"),
            market=origin.get("market"),
            raw=meta["raw"],
        )
        items.append(
            {
                "action": action,
                "reason": reason,
                "archive_id": result.get("archive_id"),
                "player": result.get("player") or meta["player"],
            }
        )
        if action == "rejected":
            logger.warning("collection rejected %s: %s", meta["player"], reason)
    stored = sum(1 for item in items if item["action"] == "stored")
    duplicates = sum(1 for item in items if item["action"] == "duplicate")
    rejected = sum(1 for item in items if item["action"] == "rejected")
    skipped = sum(1 for item in items if item["action"] == "skipped")
    conflicts = sum(1 for item in items if item["action"] == "conflict")
    return {
        "ok": rejected == 0 and conflicts == 0,
        "stored": stored,
        "duplicates": duplicates,
        "rejected": rejected,
        "skipped": skipped,
        "conflicts": conflicts,
        "provider_failures": 0,
        "items": items,
    }


def collect_joined(
    database: Path,
    projections: list[Any],
    prices: list[Any],
    *,
    journal_path: Path | None = None,
) -> dict[str, Any]:
    """Archive projections only where a real price record matches."""
    props, extras = assemble_prices(projections, prices)
    return collect_pregame(database, props, journal_path=journal_path, extras=extras)


def collect_event_odds(
    database: Path,
    projections: list[Any],
    *,
    sport_key: str,
    event_id: str,
    markets: str,
    journal_path: Path | None = None,
    bookmakers: str | None = None,
    transport: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Current player props for one event. No key and no price means nothing is archived."""
    fetched = fetch_event_player_props(
        sport_key,
        event_id,
        markets,
        bookmakers=bookmakers,
        transport=transport,
        sleep=sleep or (lambda _seconds: None),
        as_of=as_of,
    )
    quota = {
        "requests_remaining": fetched.get("requests_remaining"),
        "requests_used": fetched.get("requests_used"),
        "requests_last": fetched.get("requests_last"),
    }
    if not fetched.get("ok"):
        journal = Journal(journal_path)
        run_id = journal.start_run("collect")
        reason = fetched.get("reason") or "odds provider did not return prices"
        for projection in projections if isinstance(projections, list) else []:
            player = _player(projection) if isinstance(projection, dict) else None
            journal.add(
                run_id,
                action="provider_failure",
                reason=reason,
                player=player,
                event_id=event_id,
                sport=projection.get("sport") if isinstance(projection, dict) else None,
                market=projection.get("market") if isinstance(projection, dict) else None,
            )
            logger.warning("collection provider failure for %s: %s", player, reason)
        summary = {
            "ok": False,
            "stored": 0,
            "duplicates": 0,
            "rejected": 0,
            "conflicts": 0,
            "provider_failures": 1,
            "reason": reason,
            "bookmaker_notes": fetched.get("bookmaker_notes"),
            "quota": quota,
            "called": fetched.get("called"),
            "run_id": run_id,
            "model_version": MODEL_VERSION,
            "calibration_claimed": False,
            "profit_reported": False,
            "automatic_betting": False,
            "items": [],
        }
        journal.finish_run(run_id, "failed", {key: value for key, value in summary.items() if key != "items"})
        return summary
    result = collect_joined(database, projections, fetched.get("records") or [], journal_path=journal_path)
    result["bookmaker_notes"] = fetched.get("bookmaker_notes")
    result["books_not_returned"] = fetched.get("books_not_returned")
    result["quota"] = quota
    result["called"] = True
    return result


def default_paths(database: Path | None, journal: Path | None) -> tuple[Path, Path]:
    return (
        database if database is not None else archive_mod.default_db_path(),
        journal if journal is not None else default_journal_path(),
    )
