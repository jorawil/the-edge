"""CLI: python -m betting_model.cli evaluate --props PATH"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .books import BOOKS
from .sports import evaluate_prop

ET = ZoneInfo("America/New_York")
DEFAULT_OUT_DIR = Path("/workspace/betting_model/out")


def _side_label(is_over: bool) -> str:
    return "O" if is_over else "U"


def _rank_key(row: dict[str, Any]) -> float:
    if row.get("skipped") or not row.get("evaluated"):
        return -999.0
    return float(row.get("edge") or -999.0)


def evaluate_props_file(props_path: Path, out_dir: Path, n: int = 10) -> dict[str, Any]:
    raw = json.loads(props_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "props" in raw:
        props = raw["props"]
        meta = {k: v for k, v in raw.items() if k != "props"}
    elif isinstance(raw, list):
        props = raw
        meta = {}
    else:
        raise ValueError("props JSON must be a list or {props: [...]}")

    results = [evaluate_prop(p, n=n) for p in props]
    ranked = sorted(results, key=_rank_key, reverse=True)
    positive = [r for r in ranked if r.get("positive_edge")]
    evaluated = [r for r in ranked if r.get("evaluated")]
    skipped = [r for r in ranked if r.get("skipped")]

    now = datetime.now(ET)
    payload = {
        "generated_at": now.isoformat(),
        "timezone": "America/New_York",
        "props_path": str(props_path),
        "meta": meta,
        "summary": {
            "total": len(ranked),
            "evaluated": len(evaluated),
            "skipped": len(skipped),
            "positive_edge": len(positive),
        },
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
                f"{_side_label(bool(r.get('is_over')))} | {r.get('line')} | {r.get('odds')} | "
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
                f"| {r.get('player')} | {r.get('market')} | {_side_label(bool(r.get('is_over')))} | "
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
                f"{_side_label(bool(r.get('is_over')))} | {r.get('best_book') or ''} | {r.get('line')} | {r.get('odds')} | "
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
                f"{_side_label(bool(r.get('is_over')))}{r.get('line')} @ {r.get('odds')}): "
                f"{r.get('skip_reason')}"
            )
            if r.get("source"):
                lines.append(f"  - cite/source: {r.get('source')}")

    lines += ["", "---", "_Never place bets from this file alone._", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="betting_model", description="Probability-first edge screening")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ev = sub.add_parser("evaluate", help="Evaluate a props JSON shortlist")
    ev.add_argument("--props", required=True, type=Path, help="Path to props JSON")
    ev.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="Output directory")
    ev.add_argument("--save-as", type=str, default=None, help="Also save md/json copies as out/<name>.md/.json")
    ev.add_argument("--n", type=int, default=10, help="NBA recent games window")

    args = parser.parse_args(argv)
    if args.cmd == "evaluate":
        payload = evaluate_props_file(args.props, args.out, n=args.n)
        if args.save_as:
            import shutil
            for ext in ("md", "json"):
                shutil.copyfile(args.out / f"edges_latest.{ext}", args.out / f"{args.save_as}.{ext}")
                print(f"Wrote {args.out / f'{args.save_as}.{ext}'}")
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
