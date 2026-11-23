"""CLI: python -m betting_model.cli evaluate --props PATH

Writes ./out/edges_latest.json and ./out/edges_latest.md
(the folder you run the command from).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .archive import (
    add_correction,
    archive_many,
    archive_stats,
    attach_result,
    default_db_path,
    export_archive,
    get_snapshot,
    import_archive,
    list_snapshots,
    list_ungraded,
    load_props_file,
)
from .archive_analysis import analyze_archive, render_markdown
from .collection import collect_event_odds, collect_joined, collect_pregame, default_paths
from .grading import grade_archive, grade_with_fixture
from .journal import default_journal_path
from .odds import normalize_player_props
from .quality import quality_report, render_markdown as render_quality
from .result_sources import EspnResultSource
from .schedule import load_schedule, run_once
from .books import BOOKS, normalize_book
from .pairing import attach_opposites
from .parlays import evaluate_parlay
from .reports import group_rows, plain_header
from .core import parse_finite_number, parse_side
from .sports import evaluate_prop
from .version import MODEL_VERSION

ET = ZoneInfo("America/New_York")


def default_out_dir() -> Path:
    """./out in the directory the command is run from."""
    return Path.cwd() / "out"


def _side_label(is_over: object) -> str:
    """O or U. The string ``false`` is Under; ``bool("false")`` is not used."""
    try:
        return "O" if parse_side(is_over) else "U"
    except ValueError:
        return "?"


def _rank_key(row: dict[str, Any]) -> float:
    if row.get("skipped") or not row.get("evaluated"):
        return -999.0
    return float(row.get("edge") or -999.0)


def safe_save_name(name: str) -> str:
    """A plain file name. Rejects paths so --save-as cannot write outside the output folder."""
    if Path(name).name != name or name in {".", ".."}:
        raise ValueError("--save-as must be a file name, not a path")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", name):
        raise ValueError("--save-as may use letters, numbers, dot, underscore, and hyphen only")
    return name



def _prop_observation_key(prop: Any) -> str | None:
    """Stable identity for an exact repeated pregame prop observation."""
    if not isinstance(prop, dict):
        return None
    try:
        side: Any = parse_side(prop.get("is_over"))
    except ValueError:
        side = str(prop.get("is_over") or "").strip().casefold()
    books = prop.get("books")
    if isinstance(books, dict):
        book_quotes = []
        for name, quote in books.items():
            if not isinstance(quote, dict):
                continue
            book_quotes.append((
                normalize_book(str(name)) or str(name).strip().casefold(),
                quote.get("line"),
                quote.get("odds"),
                quote.get("retrieved_at"),
            ))
        book_identity: Any = sorted(book_quotes, key=lambda item: item[0])
    else:
        book_identity = normalize_book(str(prop.get("book") or prop.get("sportsbook") or "")) or str(
            prop.get("book") or prop.get("sportsbook") or ""
        ).strip().casefold()
    identity = {
        "event": prop.get("event_id") or prop.get("game_id") or prop.get("event_start"),
        "sport": str(prop.get("sport") or "").strip().casefold(),
        "player_id": str(prop.get("player_id") or "").strip(),
        "player": str(prop.get("player") or prop.get("player_name") or "").strip().casefold(),
        "market": str(prop.get("market") or "").strip().casefold().replace(" ", "_"),
        "side": side,
        "line": prop.get("line"),
        "odds": prop.get("odds"),
        "book": book_identity,
        "retrieved_at": prop.get("retrieved_at") or prop.get("odds_retrieved_at"),
        "projection": prop.get("projected"),
        "projection_source": prop.get("projection_source") or prop.get("source"),
        "projection_at": prop.get("projection_at") or prop.get("generated_at"),
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)


def _deduplicate_props(props: list[Any]) -> tuple[list[Any], int]:
    """Drop only exact repeated observations; distinct books, prices, or projections remain."""
    unique: list[Any] = []
    seen: set[str] = set()
    duplicates = 0
    for prop in props:
        key = _prop_observation_key(prop)
        if key is not None and key in seen:
            duplicates += 1
            continue
        if key is not None:
            seen.add(key)
        unique.append(prop)
    return unique, duplicates



def _flag_projection_conflicts(props: list[Any]) -> tuple[list[Any], int]:
    """Mark different projection values for the same event/player/market."""
    groups: dict[tuple[str, str, str, str], list[int]] = {}
    values_by_group: dict[tuple[str, str, str, str], set[float]] = {}
    for index, prop in enumerate(props):
        if not isinstance(prop, dict) or prop.get("projected") is None:
            continue
        event = prop.get("event_id") or prop.get("game_id") or prop.get("event_start")
        player = prop.get("player_id") or prop.get("player") or prop.get("player_name")
        if not event or not player:
            continue
        try:
            value = parse_finite_number(prop.get("projected"), "projected")
        except ValueError:
            continue
        key = (
            str(event).strip(),
            str(prop.get("sport") or "").strip().casefold(),
            str(player).strip().casefold(),
            str(prop.get("market") or "").strip().casefold().replace(" ", "_"),
        )
        groups.setdefault(key, []).append(index)
        values_by_group.setdefault(key, set()).add(round(value, 4))

    conflicts = 0
    output = [dict(prop) if isinstance(prop, dict) else prop for prop in props]
    for key, indexes in groups.items():
        values = sorted(values_by_group[key])
        if len(values) <= 1:
            continue
        conflicts += 1
        sources = sorted({
            str(output[index].get("projection_source") or output[index].get("source") or "source not recorded")
            for index in indexes
        })
        for index in indexes:
            output[index]["projection_conflict"] = True
            output[index]["projection_conflict_values"] = values
            output[index]["projection_conflict_sources"] = sources
    return output, conflicts


def evaluate_props_file(
    props_path: Path,
    out_dir: Path,
    n: int = 10,
    *,
    threshold: float | None = None,
) -> dict[str, Any]:
    raw = json.loads(props_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "props" in raw:
        props = raw["props"]
        meta = {k: v for k, v in raw.items() if k != "props"}
    elif isinstance(raw, list):
        props = raw
        meta = {}
    else:
        raise ValueError("props JSON must be a list or {props: [...]}")

    submitted_total = len(props)
    props, duplicates_excluded = _deduplicate_props(props)
    props, projection_conflicts = _flag_projection_conflicts(props)
    props = attach_opposites(props)
    results = [
        evaluate_prop(p, n=n, file_meta=meta, threshold=threshold) for p in props
    ]
    # Legacy probability flags are not a validated ranking signal.
    ranked = results
    evaluated = [r for r in ranked if r.get("evaluated")]
    skipped = [r for r in ranked if r.get("skipped")]
    groups = group_rows(ranked)

    now = datetime.now(ET)
    payload = {
        "generated_at": now.isoformat(),
        "timezone": "America/New_York",
        "model_version": MODEL_VERSION,
        "props_path": str(props_path),
        "meta": meta,
        "summary": {
            "total": len(ranked),
            "submitted_total": submitted_total,
            "duplicates_excluded": duplicates_excluded,
            "projection_conflicts": projection_conflicts,
            "evaluated": len(evaluated),
            "skipped": len(skipped),
            "qualified": len(groups["QUALIFIED"]),
            "screen_pass_uncalibrated": len(groups["SCREEN_PASS_UNCALIBRATED"]),
            "watch": len(groups["WATCH"]),
            "rejected": len(groups["REJECTED"]),
            "not_evaluable": len(groups["NOT_EVALUABLE"]),
            "edge_threshold": threshold if threshold is not None else 0.03,
            "note": "No betting edges or recommendations are reported; probability outputs are uncalibrated diagnostics.",
        },
        "qualified": groups["QUALIFIED"],
        "screen_pass_uncalibrated": groups["SCREEN_PASS_UNCALIBRATED"],
        "watch": groups["WATCH"],
        "rejected": groups["REJECTED"],
        "not_evaluable": groups["NOT_EVALUABLE"],
        "positive_edges": [],  # compatibility field; legacy flags are not opportunities
        "all": ranked,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "edges_latest.json"
    md_path = out_dir / "edges_latest.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_to_markdown(payload), encoding="utf-8")
    payload["out_json"] = str(json_path)
    payload["out_md"] = str(md_path)
    return payload


def _to_markdown(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    lines = [
        plain_header(payload),
        "# THE EDGE — diagnostic report",
        "",
        f"- Generated: **{payload['generated_at']}** ({payload['timezone']})",
        f"- Props: {payload['props_path']}",
        f"- Evaluated: {s['evaluated']} / {s['total']} | Skipped: {s['skipped']} | Exact duplicates excluded: {s.get('duplicates_excluded', 0)} | Projection conflicts: {s.get('projection_conflicts', 0)}",
        "",
        "> No betting edges or recommendations are produced. Probability calculations remain uncalibrated diagnostics.",
        "",
        "## Evaluation rows (descriptive only)",
        "",
    ]

    multi = [r for r in payload["all"] if r.get("books")]
    if multi:
        lines += [
            "### Available book prices",
            "",
            "| Player | Market | Side | Projection | " + " | ".join(lbl for _, lbl in BOOKS) + " |",
            "|---|---|:---:|---:|" + "---|" * len(BOOKS),
        ]
        for r in multi:
            cells = []
            for key, _ in BOOKS:
                b = r["books"].get(key) or {}
                if b.get("missing") or not b.get("evaluated"):
                    cells.append("missing")
                else:
                    cells.append(f"{b['line']} ({int(b['odds']):+d})")
            lines.append(
                f"| {r.get('player')} | {r.get('market')} | {_side_label(r.get('is_over'))} | "
                f"{r.get('projected')} | " + " | ".join(cells) + " |"
            )

    evaluated = [r for r in payload["all"] if r.get("evaluated")]
    lines += ["", "### Evaluated props (not ranked)", ""]
    if not evaluated:
        lines.append("_None evaluated._")
    else:
        lines += [
            "| Player | Sport | Market | Side | Book | Line | Odds | Projection | Projection − line (stat units) | Grade |",
            "|---|---|---|:---:|---|---:|---:|---:|---:|---|",
        ]
        for r in evaluated:
            projection = r.get("projected")
            line = r.get("line")
            gap = None if projection is None or line is None else float(projection) - float(line)
            gap_text = "n/a" if gap is None else f"{gap:+.2f}"
            lines.append(
                f"| {r.get('player')} | {r.get('sport')} | {r.get('market')} | "
                f"{_side_label(r.get('is_over'))} | {r.get('best_book') or ''} | {line} | "
                f"{r.get('odds')} | {projection} | {gap_text} | {r.get('status', 'NOT_EVALUABLE')} |"
            )

    skipped = [r for r in payload["all"] if r.get("skipped")]
    lines += ["", "### Skipped (no usable projection or price)", ""]
    if not skipped:
        lines.append("_None._")
    else:
        for r in skipped:
            lines.append(
                f"- **{r.get('player')}** ({r.get('sport')} {r.get('market')} "
                f"{_side_label(r.get('is_over'))}{r.get('line')} @ {r.get('odds')}): "
                f"{r.get('skip_reason')}"
            )

    lines += ["", "---", "_This report is not a betting recommendation._", ""]
    return "\n".join(lines)


def _parlay_command(args: argparse.Namespace) -> int:
    raw = json.loads(args.ticket.read_text(encoding="utf-8"))
    legs = raw.get("legs") if isinstance(raw, dict) else raw
    quoted = raw.get("quoted_american_odds") if isinstance(raw, dict) else None
    book = raw.get("quoted_book") if isinstance(raw, dict) else None
    result = evaluate_parlay(legs or [], quoted_american_odds=quoted, quoted_book=book)
    out_dir = args.out if args.out is not None else default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "parlay_latest.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {path}")
    print(
        "Parlay status: NOT EVALUABLE. "
        "No joint probability was calculated. "
        f"Leg grades: {', '.join(str(item) for item in result.get('leg_statuses') or []) or 'none'}"
    )
    return 0


def _add_archive_parser(sub: argparse._SubParsersAction) -> None:
    archive = sub.add_parser("archive", help="Save pregame evaluations and attach results later")
    commands = archive.add_subparsers(dest="archive_cmd", required=True)
    add = commands.add_parser("add", help="Archive props from a JSON file before the game starts")
    add.add_argument("--props", required=True, type=Path)
    add.add_argument("--db", type=Path, default=None)
    show = commands.add_parser("show", help="Show one archived snapshot, its results, and corrections")
    show.add_argument("--id", required=True)
    show.add_argument("--db", type=Path, default=None)
    listed = commands.add_parser("list", help="List snapshots for a player, game, or date")
    listed.add_argument("--db", type=Path, default=None)
    listed.add_argument("--player", default=None)
    listed.add_argument("--event", default=None)
    listed.add_argument("--sport", default=None)
    listed.add_argument("--market", default=None)
    listed.add_argument("--book", default=None)
    listed.add_argument("--date", default=None, help="Event date in UTC, YYYY-MM-DD")
    attach = commands.add_parser("attach-result", help="Append a final result without editing the snapshot")
    attach.add_argument("--id", required=True)
    attach.add_argument("--db", type=Path, default=None)
    attach.add_argument("--game-status", required=True)
    attach.add_argument("--source", required=True)
    attach.add_argument("--retrieved-at", required=True)
    attach.add_argument("--actual", default=None, help="Omit when the stat is missing. 0 is a real zero.")
    incomplete = commands.add_parser("incomplete", help="List snapshots that are not graded Win, Loss, Push, or Void")
    incomplete.add_argument("--db", type=Path, default=None)
    exported = commands.add_parser("export", help="Write a JSON or CSV copy of the archive")
    exported.add_argument("--db", type=Path, default=None)
    exported.add_argument("--out", required=True, type=Path)
    exported.add_argument("--format", default="json", choices=("json", "csv"))
    imported = commands.add_parser("import", help="Restore an exported JSON archive without overwriting rows")
    imported.add_argument("--db", type=Path, default=None)
    imported.add_argument("--from", dest="source", required=True, type=Path)
    correct = commands.add_parser("correct", help="Record a correction beside a snapshot; the original stays")
    correct.add_argument("--id", required=True)
    correct.add_argument("--db", type=Path, default=None)
    correct.add_argument("--field", required=True)
    correct.add_argument("--value", required=True)
    correct.add_argument("--reason", required=True)
    correct.add_argument("--source", default=None)
    stats = commands.add_parser("stats", help="Counts only. This does not claim the model is accurate.")
    stats.add_argument("--db", type=Path, default=None)
    analyze = commands.add_parser(
        "analyze",
        help="Score archived probabilities and projections. Does not claim the model is calibrated.",
    )
    analyze.add_argument("--db", type=Path, default=None)
    analyze.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory for calibration_report.md and calibration_report.json.",
    )
    analyze.add_argument(
        "--format",
        default="markdown",
        choices=("markdown", "json"),
        help="What to print. markdown is the default. json prints the full report.",
    )
    collect = commands.add_parser(
        "collect",
        help="Archive real pregame props. Does not invent a price or a projection.",
    )
    collect.add_argument("--db", type=Path, default=None)
    collect.add_argument("--journal", type=Path, default=None)
    collect.add_argument("--props", type=Path, default=None, help="Props that already include prices")
    collect.add_argument("--projections", type=Path, default=None, help="Projections to join to prices")
    collect.add_argument("--odds-fixture", type=Path, default=None, help="Local odds payload. Not a live call.")
    collect.add_argument("--odds-live", action="store_true", help="Call The Odds API for one current event")
    collect.add_argument("--sport-key", default=None, help="Odds API sport key, such as basketball_nba")
    collect.add_argument("--event", default=None, help="Odds API event id")
    collect.add_argument("--markets", default=None, help="Comma-separated player prop markets")
    collect.add_argument("--bookmakers", default=None, help="Documented bookmaker keys. bet365 is not sent.")
    grade = commands.add_parser(
        "grade",
        help="Attach verified finals. Does not grade a missing stat as zero or a loss.",
    )
    grade.add_argument("--db", type=Path, default=None)
    grade.add_argument("--journal", type=Path, default=None)
    grade.add_argument("--results-fixture", type=Path, default=None, help="Local result file with kind result_fixture")
    grade.add_argument("--delay", type=float, default=0.25, help="Seconds between ESPN calls")
    quality = commands.add_parser(
        "quality",
        help="Collection and grading counts. Not a calibration or profit report.",
    )
    quality.add_argument("--db", type=Path, default=None)
    quality.add_argument("--journal", type=Path, default=None)
    quality.add_argument("--format", default="markdown", choices=("markdown", "json"))
    scheduled = commands.add_parser(
        "run-once",
        help="Run the optional schedule once. Does not start a daemon and does not bet.",
    )
    scheduled.add_argument("--config", required=True, type=Path)
    scheduled.add_argument("--db", type=Path, default=None)
    scheduled.add_argument("--journal", type=Path, default=None)


def _db_path(args: argparse.Namespace) -> Path:
    return args.db if args.db is not None else default_db_path()


def _journal_path(args: argparse.Namespace) -> Path:
    return args.journal if args.journal is not None else default_journal_path()


def _load_odds_fixture(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("bookmakers"), list):
        as_of = payload.get("as_of")
        if not isinstance(as_of, str) or not as_of.strip():
            as_of = datetime.now(ET).isoformat()
        normalized = normalize_player_props(payload, retrieved_at=as_of)
        return list(normalized.get("records") or [])
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        if payload.get("fixture") is not True:
            raise ValueError("a records file must set fixture to true so it is not treated as a live price feed")
        return list(payload["records"])
    raise ValueError("odds fixture must be an event payload or a records list marked as a fixture")


def _collect_command(args: argparse.Namespace) -> int:
    database, journal = default_paths(_db_path(args), _journal_path(args))
    if sum(1 for enabled in (args.props is not None, args.projections is not None) if enabled) != 1:
        print("Use either --props or --projections.", file=sys.stderr)
        return 2
    try:
        if args.props is not None:
            if args.odds_live or args.odds_fixture is not None:
                print("--props already includes prices. Do not also pass an odds source.", file=sys.stderr)
                return 2
            result = collect_pregame(database, load_props_file(args.props), journal_path=journal)
        elif args.odds_live:
            if not args.sport_key or not args.event or not args.markets:
                print("--odds-live requires --sport-key, --event, and --markets.", file=sys.stderr)
                return 2
            result = collect_event_odds(
                database,
                load_props_file(args.projections),
                sport_key=args.sport_key,
                event_id=args.event,
                markets=args.markets,
                journal_path=journal,
                bookmakers=args.bookmakers,
            )
        elif args.odds_fixture is not None:
            result = collect_joined(
                database,
                load_props_file(args.projections),
                _load_odds_fixture(args.odds_fixture),
                journal_path=journal,
            )
        else:
            print("--projections needs --odds-fixture or --odds-live.", file=sys.stderr)
            return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not collect: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") is True else 2


def _grade_command(args: argparse.Namespace) -> int:
    database = _db_path(args)
    journal = _journal_path(args)
    try:
        if args.results_fixture is not None:
            document = json.loads(args.results_fixture.read_text(encoding="utf-8"))
            result = grade_with_fixture(database, document, journal_path=journal)
        else:
            result = grade_archive(
                database,
                EspnResultSource(delay=args.delay),
                journal_path=journal,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not grade: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


def _archive_command(args: argparse.Namespace) -> int:
    database = _db_path(args)
    if args.archive_cmd == "add":
        try:
            props = load_props_file(args.props)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Could not read props: {exc}", file=sys.stderr)
            return 2
        results = archive_many(database, props)
        stored = sum(1 for item in results if item.get("stored"))
        print(json.dumps({"stored": stored, "results": results}, indent=2))
        return 0 if stored or not results else 2
    if args.archive_cmd == "show":
        snapshot = get_snapshot(database, args.id)
        if snapshot is None:
            print("No snapshot with that id.", file=sys.stderr)
            return 2
        print(json.dumps(snapshot, indent=2))
        return 0
    if args.archive_cmd == "list":
        rows = list_snapshots(
            database,
            sport=args.sport,
            player=args.player,
            event_id=args.event,
            market=args.market,
            sportsbook=args.book,
            event_date=args.date,
        )
        print(json.dumps(rows, indent=2))
        return 0
    if args.archive_cmd == "attach-result":
        provided = args.actual is not None
        actual = None
        if provided:
            try:
                actual = parse_finite_number(args.actual, "actual")
            except ValueError as exc:
                print(json.dumps({"ok": False, "reason": str(exc)}))
                return 2
        result = attach_result(
            database,
            args.id,
            game_status=args.game_status,
            result_source=args.source,
            result_retrieved_at=args.retrieved_at,
            actual=actual,
            actual_provided=provided,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    if args.archive_cmd == "incomplete":
        print(json.dumps(list_ungraded(database), indent=2))
        return 0
    if args.archive_cmd == "export":
        result = export_archive(database, args.out, file_format=args.format)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    if args.archive_cmd == "import":
        result = import_archive(database, args.source)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    if args.archive_cmd == "correct":
        result = add_correction(
            database,
            args.id,
            field_name=args.field,
            corrected_value=args.value,
            reason=args.reason,
            source=args.source,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    if args.archive_cmd == "stats":
        print(json.dumps(archive_stats(database), indent=2))
        return 0
    if args.archive_cmd == "collect":
        return _collect_command(args)
    if args.archive_cmd == "grade":
        return _grade_command(args)
    if args.archive_cmd == "quality":
        report = quality_report(_db_path(args), _journal_path(args))
        if args.format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(render_quality(report))
        return 0
    if args.archive_cmd == "run-once":
        try:
            config = load_schedule(args.config)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Could not read the schedule: {exc}", file=sys.stderr)
            return 2
        result = run_once(config, database=_db_path(args), journal_path=_journal_path(args))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok", True) else 2
    if args.archive_cmd == "analyze":
        report = analyze_archive(database)
        if not report.get("ok"):
            print(json.dumps(report, indent=2), file=sys.stderr)
            return 2
        rendered = render_markdown(report)
        if args.out is not None:
            args.out.mkdir(parents=True, exist_ok=True)
            json_path = args.out / "calibration_report.json"
            md_path = args.out / "calibration_report.md"
            json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            md_path.write_text(rendered, encoding="utf-8")
            print(f"Wrote {json_path}")
            print(f"Wrote {md_path}")
        if args.format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(rendered)
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="betting_model", description="Probability-first edge screening")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ev = sub.add_parser("evaluate", help="Evaluate a props JSON shortlist")
    ev.add_argument("--props", required=True, type=Path, help="Path to props JSON")
    ev.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory. Default is ./out in the current directory.",
    )
    ev.add_argument("--save-as", type=str, default=None, help="Also save md/json copies as out/<name>.md/.json")
    ev.add_argument("--n", type=int, default=10, help="NBA recent games window")
    ev.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Edge threshold in probability points. Default is 0.03 (3 percentage points).",
    )

    parlay = sub.add_parser("parlay", help="Grade parlay legs without inventing a joint probability")
    parlay.add_argument("--ticket", required=True, type=Path, help="JSON with legs and optional quoted_american_odds")
    parlay.add_argument("--out", type=Path, default=None, help="Output directory. Default is ./out")

    _add_archive_parser(sub)

    args = parser.parse_args(argv)
    if args.cmd == "parlay":
        return _parlay_command(args)
    if args.cmd == "archive":
        return _archive_command(args)
    if args.cmd == "evaluate":
        try:
            save_as = safe_save_name(args.save_as) if args.save_as else None
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        out_dir = args.out if args.out is not None else default_out_dir()
        payload = evaluate_props_file(args.props, out_dir, n=args.n, threshold=args.threshold)
        if save_as:
            import shutil
            for ext in ("md", "json"):
                shutil.copyfile(out_dir / f"edges_latest.{ext}", out_dir / f"{save_as}.{ext}")
                print(f"Wrote {out_dir / f'{save_as}.{ext}'}")
        print(f"Wrote {payload['out_json']}")
        print(f"Wrote {payload['out_md']}")
        print(
            f"Summary: evaluated={payload['summary']['evaluated']} "
            f"skipped={payload['summary']['skipped']} "
            f"qualified={payload['summary']['qualified']}"
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
