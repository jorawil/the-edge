"""Audit historical ESPN box-score feature coverage without exposing player identities."""
from __future__ import annotations
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def audit_feature_coverage(sport: str, *, root: Path) -> dict[str, Any]:
    path = Path(root) / sport / "games.json"
    if not path.exists():
        return {"ok": False, "reason": "unavailable", "detail": f"no game file at {path}"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "invalid_data", "detail": str(exc)}
    games = raw.get("games") if isinstance(raw, dict) else None
    if not isinstance(games, dict):
        return {"ok": False, "reason": "invalid_shape", "detail": "games file has no games object"}
    selected_games = played_rows = dnp_rows = 0
    category_rows: Counter[str] = Counter()
    key_counts: dict[str, Counter[str]] = defaultdict(Counter)
    numeric_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for game in games.values():
        if not isinstance(game, dict) or game.get("season_type_id") != 2:
            continue
        selected_games += 1
        for player in game.get("players") or []:
            if not isinstance(player, dict):
                continue
            if player.get("dnp"):
                dnp_rows += 1
                continue
            stats = player.get("stats")
            if not isinstance(stats, dict):
                continue
            category = str(player.get("category") or "uncategorized")
            played_rows += 1
            category_rows[category] += 1
            for key, value in stats.items():
                if value is None or str(value).strip() == "":
                    continue
                field = str(key)
                key_counts[category][field] += 1
                numeric_part = str(value).strip().replace(",", "").split("-", 1)[0].split("/", 1)[0]
                try:
                    float(numeric_part)
                except (TypeError, ValueError, OverflowError):
                    continue
                numeric_counts[category][field] += 1
    categories = {}
    for category in sorted(category_rows):
        denominator = category_rows[category]
        fields = [
            {"field": key, "rows_present": count,
             "coverage_pct": round(100.0 * count / denominator, 2),
             "numeric_rows": numeric_counts[category][key]}
            for key, count in sorted(key_counts[category].items(), key=lambda item: (-item[1], item[0]))
        ]
        categories[category] = {"played_player_rows": denominator, "fields": fields}
    return {
        "ok": True, "sport": sport, "regular_season_games": selected_games,
        "played_player_rows": played_rows, "dnp_player_rows_excluded": dnp_rows,
        "categories": categories,
        "limitations": [
            "Field availability is not forecast skill or feature importance.",
            "Presence does not establish accuracy, pregame availability, or predictive value.",
            "Only records with season_type_id=2 are counted.",
            "Player names and athlete IDs are not included.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit historical ESPN player-stat feature coverage.")
    parser.add_argument("sport", choices=("nba", "nfl"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    result = audit_feature_coverage(args.sport, root=args.data_root)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
