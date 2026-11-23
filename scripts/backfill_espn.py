"""Backfill final scores + player box stats from ESPN public API. Never invents stats:
anything not returned by the source is left out and logged in missing[]."""
import argparse
import json
import sys
import time
import datetime as dt
import os
import tempfile
import urllib.request
from pathlib import Path
DATA = Path(os.environ.get("EDGE_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
SPORTS = {"nba": "basketball/nba", "nhl": "hockey/nhl", "nfl": "football/nfl", "cfb": "football/college-football"}
BASE = "https://site.api.espn.com/apis/site/v2/sports/"


def _atomic_write_text(path, content):
    """Replace a file only after its complete contents are safely written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e; time.sleep(1 + i)
    raise last

def parse_box(summ):
    players = []
    for team in summ.get("boxscore", {}).get("players", []):
        tabbr = team["team"].get("abbreviation")
        for cat in team.get("statistics", []):
            keys = cat.get("keys") or cat.get("names") or []
            cname = cat.get("name") or cat.get("type") or ""
            for a in cat.get("athletes", []):
                if not a.get("stats"): continue
                players.append({"team": tabbr, "player": a["athlete"].get("displayName"),
                                "athlete_id": a["athlete"].get("id"), "category": cname,
                                "stats": dict(zip(keys, a["stats"])), "dnp": a.get("didNotPlay", False)})
    teams = [{"team": t["team"].get("abbreviation"),
              "stats": {s.get("name"): s.get("displayValue") for s in t.get("statistics", [])}}
             for t in summ.get("boxscore", {}).get("teams", [])]
    return players, teams

def _season_metadata(event):
    """Return ESPN season type ID/name/year across object and scalar API shapes."""
    season = event.get("season") or {}
    season_type = season.get("type")
    if isinstance(season_type, dict):
        raw_id = season_type.get("id")
        type_name = season_type.get("name") or season_type.get("abbreviation")
    else:
        # ESPN scoreboards may return type as an integer (or numeric string).
        raw_id = season_type
        type_name = None
    try:
        type_id = int(raw_id) if raw_id is not None and not isinstance(raw_id, bool) else None
    except (TypeError, ValueError, OverflowError):
        type_id = None
    return {"season_year": season.get("year"), "season_type_id": type_id,
            "season_type_name": type_name}


def record_is_complete(record) -> bool:
    """True only when a stored game has the pieces a rerun should not replace.

    A box with at least one player can still be incomplete: a did-not-play flag
    is fine, an empty stat line for someone who played is not, and a missing
    final score is not.
    """
    if not isinstance(record, dict):
        return False
    players = record.get("players")
    if not isinstance(players, list) or not players:
        return False
    if not record.get("date_utc") or not record.get("source"):
        return False
    scores = record.get("scores")
    if not isinstance(scores, dict) or not scores:
        return False
    if any(_score(score) is None for score in scores.values()):
        return False
    for player in players:
        if not isinstance(player, dict):
            return False
        if player.get("dnp"):
            continue
        stats = player.get("stats")
        if not isinstance(stats, dict) or not stats:
            return False
    return True


def run(sport, start, end, fetcher=None, season_types=(2,)):
    """Backfill selected ESPN season types; default 2 is regular season."""
    allowed_season_types = {int(value) for value in season_types}
    fetch = fetcher or get
    path = DATA / sport / "games.json"; path.parent.mkdir(parents=True, exist_ok=True)
    store = json.loads(path.read_text()) if path.exists() else {"games": {}}
    pre = set(store["games"]); added, repaired, missing, events, skipped_season = [], [], [], [], []
    d = start
    while d <= end:
        url = BASE + SPORTS[sport] + f"/scoreboard?dates={d:%Y%m%d}&limit=500" + ("&groups=80" if sport == "cfb" else "")
        for e in fetch(url).get("events", []):
            events.append(e)
        d += dt.timedelta(days=1)
    seen = {}
    for e in events: seen[e["id"]] = e
    for gid, e in seen.items():
        season_meta = _season_metadata(e)
        season_type_id = season_meta["season_type_id"]
        if season_type_id not in allowed_season_types:
            skipped_season.append({"id": gid, "name": e.get("name"),
                "season_type_id": season_type_id, "season_type_name": season_meta["season_type_name"],
                "reason": "missing season type metadata" if season_type_id is None else "season type not selected"})
            continue
        st = e["status"]["type"]
        comp = e["competitions"][0]
        if not st.get("completed"):
            missing.append({"id": gid, "name": e["name"], "reason": f"not final ({st.get('description')})"}); continue
        existing = store["games"].get(gid)
        if existing and record_is_complete(existing):
            continue
        summ_url = BASE + SPORTS[sport] + f"/summary?event={gid}"
        try:
            players, teams = parse_box(fetch(summ_url))
        except Exception as ex:
            players, teams = [], []; missing.append({"id": gid, "name": e["name"], "reason": f"box fetch failed {ex}"})
        if not players:
            missing.append({"id": gid, "name": e["name"], "reason": "no player box in source"})
            continue
        record = {"id": gid, "date_utc": e.get("date"), "name": e.get("name"), "status": st.get("description"), "season_year": season_meta["season_year"],
            "season_type_id": season_meta["season_type_id"], "season_type_name": season_meta["season_type_name"],
            "scores": {c["team"]["abbreviation"]: _score(c.get("score")) for c in comp["competitors"]},
            "home": next(c["team"]["abbreviation"] for c in comp["competitors"] if c["homeAway"] == "home"),
            "week": e.get("week", {}).get("number"), "players": players, "team_stats": teams,
            "source": f"ESPN {summ_url}", "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        if not record["date_utc"] or not isinstance(record["players"], list):
            missing.append({"id": gid, "name": e.get("name"), "reason": "record failed shape check and was not stored"})
            continue
        if any(score is None for score in record["scores"].values()):
            missing.append({"id": gid, "name": e.get("name"), "reason": "final score missing; stored as null, not zero"})
        store["games"][gid] = record
        if gid in pre:
            repaired.append(gid)
        else:
            added.append(gid)
    store["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_write_text(path, json.dumps(store, indent=1))
    rep = {"sport": sport, "events_in_window": len(seen), "selected_season_types": sorted(allowed_season_types), "skipped_season_type": skipped_season,
           "final": len(seen) - len(skipped_season) - sum(1 for m in missing if m["reason"].startswith("not final")),
           "already_covered": len([g for g, event in seen.items() if _season_metadata(event)["season_type_id"] in allowed_season_types and g in pre and record_is_complete(store["games"].get(g, {}))]),
           "added": len(added), "repaired": len(repaired), "missing": missing}
    print(json.dumps({k: v for k, v in rep.items() if k != "missing"}), len(missing))
    return rep

def _score(value):
    """Return an integer score, preserving zero and rejecting invalid values."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill ESPN box scores. Never invents stats.")
    parser.add_argument("sports", nargs="*", help="nba, nhl, nfl, and/or cfb. Default: all four.")
    parser.add_argument("--start", default="2026-10-24", help="First date, YYYY-MM-DD. Default is the original window.")
    parser.add_argument("--end", default="2026-10-31", help="Last date, YYYY-MM-DD.")
    parser.add_argument("--season-types", default="2", help="Comma-separated ESPN season type IDs (1=preseason, 2=regular, 3=postseason). Default: 2.")
    args = parser.parse_args(argv)
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if end < start:
        parser.error("--end must be on or after --start")
    try:
        season_types = tuple({int(value.strip()) for value in args.season_types.split(",") if value.strip()})
        if not season_types:
            raise ValueError("at least one season type is required")
    except ValueError as exc:
        parser.error(f"invalid --season-types: {exc}")
    chosen = args.sports or list(SPORTS)
    unknown = [sport for sport in chosen if sport not in SPORTS]
    if unknown:
        raise SystemExit(f"unsupported sport: {', '.join(unknown)}")
    reps = [run(sport, start, end, season_types=season_types) for sport in chosen]
    dated = DATA / f"backfill_report_{start.isoformat()}_{end.isoformat()}.json"
    dated.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(dated, json.dumps(reps, indent=1))
    if (start, end) == (dt.date(2026, 10, 24), dt.date(2026, 10, 31)):
        _atomic_write_text(DATA / "backfill_report_oct24_31.json", json.dumps(reps, indent=1))
    return reps


def _has_fatal_missing(reports):
    """Provider/shape failures should be visible to shell and workflow callers."""
    fatal_prefixes = (
        "box fetch failed",
        "no player box in source",
        "record failed shape check",
        "final score missing",
    )
    return any(
        str(item.get("reason") or "").startswith(fatal_prefixes)
        for report in reports
        for item in report.get("missing", [])
    )


if __name__ == "__main__":
    reports = main()
    sys.exit(2 if _has_fatal_missing(reports) else 0)
