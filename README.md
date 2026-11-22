# betting_model — probability-first edge screening

Reusable Python package for Joey Wills desks and Chief of Staff. **Never places bets.**

## Install (box)

```bash
cd /workspace/betting_model
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# or: pip install --user -e .
```

Deps: `numpy`, `requests`, `nba_api`.

## Public API

```python
from betting_model import implied_probability, calculate_edge
from betting_model import get_nba_player_recent_avg, fetch_live_odds

implied_probability(-110)  # ~0.524
calculate_edge(projected=25.0, line=23.5, odds=-106, is_over=True, market="PR")
# -> projected, line, odds, implied_prob, model_prob, edge, positive_edge (>3%)
```

### Sigma defaults (`SIGMA_DEFAULTS`)

| Market family | sigma |
|---|---|
| PTS / PR / PA | 6 |
| PRA | 7 |
| REB / AST | 3 |
| RUSH_YDS / REC_YDS | 25 |
| PASS_YDS | 40 |

Model: `Normal(projected, sigma)`; `model_prob` = P(side hits); `edge = model_prob - implied_prob`.

## CLI

```bash
python -m betting_model.cli evaluate --props /workspace/betting_model/props_tonight.json
```

Writes:

- `/workspace/betting_model/out/edges_latest.json`
- `/workspace/betting_model/out/edges_latest.md` (phone-readable)

If `projected` is missing and `sport=nba`, pulls `nba_api` recent avg (last 10) and maps market → PTS/REB/AST/PR/PRA/PA.

## The Odds API

```bash
export ODDS_API_KEY="your_key"   # https://the-odds-api.com/
```

`fetch_live_odds("basketball_nba")` — if key missing returns `{ok: false, reason: "no ODDS_API_KEY"}` (no invented prices).

## Desk usage rules

1. Only legs with **`positive_edge=True` AND desk research agreement** become Strong / Med.
2. **Model alone never juices a ticket.**
3. Muse owns final NFL / World Series ticket.
4. Unverified / placeholder odds in research cites must be live-checked before Strong.

See `DESK_PROTOCOL.md` for the one-pager bots follow.

## Local results/box store (added Oct 31 2026)

`data/{nba,nhl,nfl,cfb}/games.json` — final scores + player box stats from ESPN public API
(`scripts/backfill_espn.py`, source URL stored per game). Loader: `betting_model.data_store`
(`load_games`, `player_log`, `recent_avg`). Rolling L5 snapshots: `data/<sport>/rolling_l5_oct31.json`.
NHL and CFB had no prior support; this is their first store. Backfill report: `data/backfill_report_oct24_31.json`.

## Books (Oct 31: Fanatics added)

Order everywhere: **FanDuel IN, DraftKings, bet365, Fanatics** (`betting_model/books.py`).
Props may carry `books: {fanduel_in|draftkings|bet365|fanatics: {line, odds, source} | null}`; each book is
priced, missing books are shown as `missing`, headline row = best-edge book. `--save-as NAME` writes a named copy.
Receiving markets: use `receiving_yards` / `REC_YDS` (sigma 25). `player_receiving_yards` now aliases to REC_YDS
(previously fell to DEFAULT sigma 6 — the old sigma-6 bug).
