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
from .books import BOOKS
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

    props = attach_opposites(props)
    results = [
        evaluate_prop(p, n=n, file_meta=meta, threshold=threshold) for p in props
    ]
    ranked = sorted(results, key=_rank_key, reverse=True)
    positive = [r for r in ranked if r.get("positive_edge")]
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
            "evaluated": len(evaluated),
            "skipped": len(skipped),
            "positive_edge": len(positive),
            "qualified": len(groups["QUALIFIED"]),
            "screen_pass_uncalibrated": len(groups["SCREEN_PASS_UNCALIBRATED"]),
            "watch": len(groups["WATCH"]),
            "rejected": len(groups["REJECTED"]),
            "not_evaluable": len(groups["NOT_EVALUABLE"]),
            "edge_threshold": threshold if threshold is not None else 0.03,
            "note": (
                "positive_edge is the older vig-inclusive screen. "
                "SCREEN_PASS_UNCALIBRATED cleared that screen with an uncalibrated "
                "sigma and normal curve. QUALIFIED is reserved for a calibrated model. "
                "None of these grades places a bet."
            ),
        },
        "qualified": groups["QUALIFIED"],
        "screen_pass_uncalibrated": groups["SCREEN_PASS_UNCALIBRATED"],
        "watch": groups["WATCH"],
        "rejected": groups["REJECTED"],
        "not_evaluable": groups["NOT_EVALUABLE"],
        "positive_edges": positive,
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
        "# Edges latest (probability-first)",
        "",
        f"- Generated: **{payload['generated_at']}** ({payload['timezone']})",
        f"- Props: `{payload['props_path']}`",
        f"- Evaluated: {s['evaluated']} / {s['total']} | Skipped: {s['skipped']} | "
        f"**Positive edge (>3%): {s['positive_edge']}**",
        "",
        "> Model alone never juices a ticket. Only legs with `positive_edge=True` "
        "**and** desk research agreement become Strong/Med. Muse owns final NFL/WS ticket.",
        "",
        "## Positive edges",
        "",
    ]
    pos = payload.get("positive_edges") or []
    if not pos:
        lines.append("_None tonight._")
    else:
        lines += [
            "| Rank | Player | Sport | Market | Side | Line | Odds | Proj | Model% | Imp% | Edge | Source |",
            "|---:|---|---|---|:---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for i, r in enumerate(pos, 1):
            lines.append(
                f"| {i} | {r.get('player')} | {r.get('sport')} | {r.get('market')} | "
                f"{_side_label(r.get('is_over'))} | {r.get('line')} | {r.get('odds')} | "
                f"{r.get('projected')} | {100*float(r.get('model_prob') or 0):.1f}% | "
                f"{100*float(r.get('implied_prob') or 0):.1f}% | "
                f"{100*float(r.get('edge') or 0):+.1f}% | {r.get('proj_source') or r.get('source') or ''} |"
            )

    multi = [r for r in payload["all"] if r.get("books")]
    if multi:
        lines += ["", "## By book (FanDuel IN / DraftKings / bet365 / Fanatics)", "",
                  "| Player | Market | Side | Proj | " + " | ".join(lbl for _, lbl in BOOKS) + " | Best | Edge | +? |",
                  "|---|---|:---:|---:|" + "---|" * len(BOOKS) + "---|---:|:---:|"]
        for r in multi:
            cells = []
            for key, _ in BOOKS:
                b = r["books"].get(key) or {}
                if b.get("missing") or not b.get("evaluated"):
                    cells.append("missing")
                else:
                    cells.append(f"{b['line']} ({int(b['odds']):+d}) {100*float(b['edge']):+.1f}%")
            pe = "Y" if r.get("positive_edge") else ""
            lines.append(
                f"| {r.get('player')} | {r.get('market')} | {_side_label(r.get('is_over'))} | "
                f"{r.get('projected')} | " + " | ".join(cells) + f" | {r.get('best_book','')} {r.get('line')} "
                f"({r.get('odds')}) | {100*float(r.get('edge') or 0):+.1f}% | {pe} |"
            )
        lines += ["", "### Price cites", ""]
        seen = set()
        for r in multi:
            for key, lbl in BOOKS:
                b = r["books"].get(key) or {}
                tag = (r.get('player'), r.get('market'), key)
                if tag in seen:
                    continue
                seen.add(tag)
                cite = b.get("source") if not b.get("missing") else f"MISSING — {b.get('reason')}"
                lines.append(f"- {r.get('player')} {r.get('market')} · {lbl}: {cite}")

    lines += ["", "## All evaluated legs (ranked by edge)", ""]
    evaluated = [r for r in payload["all"] if r.get("evaluated")]
    if not evaluated:
        lines.append("_None evaluated._")
    else:
        lines += [
            "| Player | Sport | Market | Side | Book | Line | Odds | Proj | Edge | +? |",
            "|---|---|---|:---:|---|---:|---:|---:|---:|:---:|",
        ]
        for r in evaluated:
            pe = "Y" if r.get("positive_edge") else ""
            lines.append(
                f"| {r.get('player')} | {r.get('sport')} | {r.get('market')} | "
                f"{_side_label(r.get('is_over'))} | {r.get('best_book') or ''} | {r.get('line')} | {r.get('odds')} | "
                f"{r.get('projected')} | {100*float(r.get('edge') or 0):+.1f}% | {pe} |"
            )

    skipped = [r for r in payload["all"] if r.get("skipped")]
    lines += ["", "## Skipped (no projection / API miss)", ""]
    if not skipped:
        lines.append("_None._")
    else:
        for r in skipped:
            lines.append(
                f"- **{r.get('player')}** ({r.get('sport')} {r.get('market')} "
                f"{_side_label(r.get('is_over'))}{r.get('line')} @ {r.get('odds')}): "
                f"{r.get('skip_reason')}"
            )
            if r.get("source"):
                lines.append(f"  - cite/source: {r.get('source')}")

    lines += ["", "---", "_Never place bets from this file alone._", ""]
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


def _db_path(args: argparse.Namespace) -> Path:
    return args.db if args.db is not None else default_db_path()


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
            f"positive_edge={payload['summary']['positive_edge']}"
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
