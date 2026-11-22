"""Backfill final scores + player box stats from ESPN public API. Never invents stats:
anything not returned by the source is left out and logged in missing[]."""
import json, sys, time, datetime as dt, urllib.request
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
        store["games"][gid] = {"id": gid, "date_utc": e["date"], "name": e["name"], "status": st.get("description"),
            "scores": {c["team"]["abbreviation"]: int(c.get("score") or 0) for c in comp["competitors"]},
            "home": next(c["team"]["abbreviation"] for c in comp["competitors"] if c["homeAway"] == "home"),
            "week": e.get("week", {}).get("number"), "players": players, "team_stats": teams,
            "source": f"ESPN {summ_url}"}
        added.append(gid)
    store["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(store, indent=1))
    rep = {"sport": sport, "events_in_window": len(seen), "final": len(seen) - sum(1 for m in missing if m["reason"].startswith("not final")),
           "already_covered": len([g for g in seen if g in pre]), "added": len(added), "missing": missing}
    print(json.dumps({k: v for k, v in rep.items() if k != "missing"}), len(missing))
    return rep

if __name__ == "__main__":
    s, e = dt.date(2026, 10, 24), dt.date(2026, 10, 31)
    reps = [run(sp, s, e) for sp in (sys.argv[1:] or SPORTS)]
    (DATA / "backfill_report_oct24_31.json").write_text(json.dumps(reps, indent=1))
