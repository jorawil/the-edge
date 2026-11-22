"""Backfill final scores + player box stats from ESPN public API. Never invents stats:
anything not returned by the source is left out and logged in missing[]."""
import argparse
import json
import sys
import time
import datetime as dt
import urllib.request
from pathlib import Path
DATA = Path(__file__).resolve().parents[1] / "data"
SPORTS = {"nba": "basketball/nba", "nhl": "hockey/nhl", "nfl": "football/nfl", "cfb": "football/college-football"}
BASE = "https://site.api.espn.com/apis/site/v2/sports/"

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

def run(sport, start, end):
    path = DATA / sport / "games.json"; path.parent.mkdir(parents=True, exist_ok=True)
    store = json.loads(path.read_text()) if path.exists() else {"games": {}}
    pre = set(store["games"]); added, missing, events = [], [], []
    d = start
    while d <= end:
        url = BASE + SPORTS[sport] + f"/scoreboard?dates={d:%Y%m%d}&limit=500" + ("&groups=80" if sport == "cfb" else "")
        for e in get(url).get("events", []):
            events.append(e)
        d += dt.timedelta(days=1)
    seen = {}
    for e in events: seen[e["id"]] = e
    for gid, e in seen.items():
        st = e["status"]["type"]
        comp = e["competitions"][0]
        if not st.get("completed"):
            missing.append({"id": gid, "name": e["name"], "reason": f"not final ({st.get('description')})"}); continue
        if gid in store["games"] and store["games"][gid].get("players"): continue
        summ_url = BASE + SPORTS[sport] + f"/summary?event={gid}"
        try:
            players, teams = parse_box(get(summ_url))
        except Exception as ex:
            players, teams = [], []; missing.append({"id": gid, "name": e["name"], "reason": f"box fetch failed {ex}"})
        if not players: missing.append({"id": gid, "name": e["name"], "reason": "no player box in source"})
        record = {"id": gid, "date_utc": e.get("date"), "name": e.get("name"), "status": st.get("description"),
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
        added.append(gid)
    store["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path.write_text(json.dumps(store, indent=1))
    rep = {"sport": sport, "events_in_window": len(seen), "final": len(seen) - sum(1 for m in missing if m["reason"].startswith("not final")),
           "already_covered": len([g for g in seen if g in pre]), "added": len(added), "missing": missing}
    print(json.dumps({k: v for k, v in rep.items() if k != "missing"}), len(missing))
    return rep

def _score(value):
    """A missing score stays missing. A real zero stays zero."""
    if value is None or value == "":
        return None
    return int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill ESPN box scores. Never invents stats.")
    parser.add_argument("sports", nargs="*", help="nba, nhl, nfl, and/or cfb. Default: all four.")
    parser.add_argument("--start", default="2026-10-24", help="First date, YYYY-MM-DD. Default is the original window.")
    parser.add_argument("--end", default="2026-10-31", help="Last date, YYYY-MM-DD.")
    args = parser.parse_args(argv)
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    chosen = args.sports or list(SPORTS)
    unknown = [sport for sport in chosen if sport not in SPORTS]
    if unknown:
        raise SystemExit(f"unsupported sport: {', '.join(unknown)}")
    reps = [run(sport, start, end) for sport in chosen]
    dated = DATA / f"backfill_report_{start.isoformat()}_{end.isoformat()}.json"
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(json.dumps(reps, indent=1))
    if (start, end) == (dt.date(2026, 10, 24), dt.date(2026, 10, 31)):
        (DATA / "backfill_report_oct24_31.json").write_text(json.dumps(reps, indent=1))
    return reps


if __name__ == "__main__":
    main()
