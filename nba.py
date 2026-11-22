"""NBA recent averages via nba_api. On failure return error dict — never invent stats."""

from __future__ import annotations

from typing import Any


def get_nba_player_recent_avg(player_name: str, n: int = 10) -> dict[str, Any]:
    """Return last N games averages for PTS, REB, AST, MIN, plus games count.

    On any failure returns {ok: False, error: ...} without inventing stats.
    """
    name = (player_name or "").strip()
    if not name:
        return {"ok": False, "error": "player_name required", "player": player_name}

    try:
        from nba_api.stats.static import players
        from nba_api.stats.endpoints import playergamelog
    except ImportError as e:
        return {
            "ok": False,
            "error": f"nba_api not installed: {e}",
            "player": name,
            "hint": "pip install nba_api",
        }

    try:
        matches = players.find_players_by_full_name(name)
        if not matches:
            # try partial / last-name style
            all_p = players.get_players()
            needle = name.lower()
            matches = [
                p
                for p in all_p
                if p.get("is_active")
                and (
                    needle == p["full_name"].lower()
                    or needle in p["full_name"].lower()
                    or p["full_name"].lower().endswith(needle)
                )
            ]
        if not matches:
            return {"ok": False, "error": f"player not found: {name}", "player": name}

        # prefer exact active match
        exact = [p for p in matches if p["full_name"].lower() == name.lower()]
        player = exact[0] if exact else matches[0]
        pid = player["id"]

        # current season first, then prior if empty
        seasons_try = ["2026-27", "2025-26"]
        df = None
        used_season = None
        last_err = None
        for season in seasons_try:
            try:
                log = playergamelog.PlayerGameLog(
                    player_id=pid,
                    season=season,
                    season_type_all_star="Regular Season",
                )
                frame = log.get_data_frames()[0]
                if frame is not None and len(frame) > 0:
                    df = frame
                    used_season = season
                    break
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                continue

        if df is None or len(df) == 0:
            return {
                "ok": False,
                "error": f"no game log for {name}",
                "player": name,
                "player_id": pid,
                "detail": last_err,
            }

        recent = df.head(int(n))
        games = int(len(recent))
        if games == 0:
            return {
                "ok": False,
                "error": "zero games in window",
                "player": name,
                "player_id": pid,
            }

        def _avg(col: str) -> float:
            return float(recent[col].astype(float).mean())

        pts = _avg("PTS")
        reb = _avg("REB")
        ast = _avg("AST")
        mins = _avg("MIN") if "MIN" in recent.columns else None

        return {
            "ok": True,
            "player": player["full_name"],
            "player_id": pid,
            "season": used_season,
            "games": games,
            "n_requested": int(n),
            "PTS": round(pts, 2),
            "REB": round(reb, 2),
            "AST": round(ast, 2),
            "MIN": round(mins, 2) if mins is not None else None,
            "PR": round(pts + reb, 2),
            "PRA": round(pts + reb + ast, 2),
            "PA": round(pts + ast, 2),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"nba_api failure: {type(e).__name__}: {e}",
            "player": name,
        }


def map_market_to_avg(avgs: dict[str, Any], market: str) -> float | None:
    """Map market string to a recent-avg value from get_nba_player_recent_avg."""
    if not avgs.get("ok"):
        return None
    key = market.strip().upper().replace(" ", "_").replace("+", "_")
    aliases = {
        "POINTS": "PTS",
        "REBOUNDS": "REB",
        "ASSISTS": "AST",
        "POINTS_REBOUNDS": "PR",
        "PTS_REB": "PR",
        "POINTS_REBOUNDS_ASSISTS": "PRA",
        "PTS_REB_AST": "PRA",
        "POINTS_ASSISTS": "PA",
        "PTS_AST": "PA",
    }
    key = aliases.get(key, key)
    val = avgs.get(key)
    return float(val) if val is not None else None
