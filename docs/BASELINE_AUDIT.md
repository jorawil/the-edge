# Baseline audit (before code changes)

**Date:** November 22, 2026
**Repo:** `jorawil/the-edge`
**Branch at audit:** `main` at commit `f83cb10` (working tree clean)
**This document describes the repository as it was before the upgrade.** Later commits on this branch change the code. The pull request says what those commits did.

THE EDGE is an analysis tool. It does not place bets.

## How the code is laid out

The README and `pyproject.toml` describe a Python package named `betting_model`. That is also how the share zip in the repo is organized (`betting_model/` plus `scripts/backfill_espn.py`).

On `main`, those modules sit at the repository root instead:

- `core.py` — American odds, normal-distribution probability, edge
- `sports.py` — per-sport evaluation and multi-book props
- `books.py` — FanDuel IN, DraftKings, bet365, Fanatics
- `odds.py` — The Odds API client
- `nba.py` — recent NBA averages through `nba_api`
- `data_store.py` — reader for `data/<sport>/games.json`
- `cli.py` — `evaluate` command and the markdown/JSON report
- `__init__.py`, `__main__.py`
- `backfill_espn.py` — ESPN box-score backfill (the README says this lives in `scripts/`)
- `tests/test_core.py` — 10 math tests
- `.github/workflows/tests.yml`

`pyproject.toml` only packages directories named `betting_model*`. There is no such directory on `main`, so `pip install -e .` does not install the code the README tells you to import.

The modules use relative imports (`from .core import ...`). Those imports work only when Python treats the folder as a package. They do not work as a normal `python core.py` script, and `python -m betting_model` cannot work because the `betting_model` folder does not exist.

## How a person runs it today

Documented command:

```bash
python -m betting_model.cli evaluate --props /workspace/betting_model/props_tonight.json
```

That command does not match this checkout:

- There is no `betting_model` package to run.
- The default output folder is the hardcoded path `/workspace/betting_model/out`, which is a path from another machine.
- `ODDS_API_KEY` is optional. If it is missing, `fetch_live_odds` returns `{ok: false, reason: "no ODDS_API_KEY"}` and does not invent prices.

What does work, after the GitHub Actions workaround of renaming `__init__.py` out of the way:

```bash
pytest -q tests/test_core.py
```

Sample inputs and old reports are checked in at the repo root (`props_*.json`, `edges_latest.*`, `w11_rushing_sat.*`, `backfill_report_oct24_31.json`). Raw ESPN game files `data/{nba,nhl,nfl,cfb}/games.json` are not in the repo.

## Tests, run on this machine (Python 3.12.3)

| How | Result |
|---|---|
| `pytest -q` with the repo as it is | **10 errors.** Pytest treats root `__init__.py` as a test module. That file imports `odds.py`, which imports `requests`, which was not installed. Collection dies before the math tests run. |
| Same tests after the workflow's workaround (`mv __init__.py` aside, then `pytest -q tests/test_core.py`) | **10 passed in 0.07s.** |

The workflow installs only `pytest` and `numpy`, then renames `__init__.py` for the test step. That is a workaround, not a package install. `requests` and `nba_api` are declared dependencies and are not installed in CI.

## What the math does today

`implied_probability`:

- Negative American odds: `|odds| / (|odds| + 100)`
- Positive American odds: `100 / (odds + 100)`
- Odds of 0 raise `ValueError`
- Odds between -99 and 99 (other than the zero check) are not rejected
- `True`/`False` and non-numeric values are not cleanly rejected (`int(True)` is `1`)

`calculate_edge`:

- Model is `Normal(projection, sigma)` using `math.erf`
- `model_prob` is P(stat > line) for Over and the complement for Under
- That complement ignores pushes. A whole-number line and a half-point line are treated the same way
- `implied_prob` is the raw, vig-inclusive price
- `edge = model_prob - implied_prob`
- `positive_edge` is true only when `edge > 0.03` (exactly 0.03 does not pass)
- There is no no-vig (fair) probability, no expected value, and no separate name making the vig obvious
- Sigma defaults: PTS/PR/PA 6, PRA 7, REB/AST 3, MIN 5, rush/rec yards 25, rush+rec 30, pass yards 40, otherwise 6
- An explicit sigma is not checked for NaN or negative until the probability step, and NaN slips through (`NaN <= 0` is false)

`bool(prop["is_over"])` is used in `sports.py`. The string `"false"` is boolean True in Python. Real JSON booleans in the sample files are fine. A hand-edited `"false"` string would be scored as Over.

One bad prop (bad odds, missing line) raises and stops the whole file. The sample files happen to be well formed.

## Checked against the spec's known weaknesses

1. **Recent averages used as projections.** Confirmed for NBA only. If `projected` is missing, `evaluate_nba_prop` fills it from `nba_api` last-N averages and still sets `positive_edge` from that number. The label is `proj_source`, not a clear "this is not a forecast." NFL, WNBA, and MLB skip when `projected` is missing. They do not invent a number.
2. **Sigmas are uncalibrated defaults.** Confirmed. Nothing in the repo stores a pregame projection and later compares it with the actual stat. `data_store.recent_avg` is a historical average, which the spec says is the wrong calibration target. Game logs themselves are not in the repo.
3. **Vig is not explained.** Confirmed. The report says `Imp%` and `Edge`. Those are raw implied probability and model minus raw implied. The markdown says "Positive edge (>3%)", which is easy to misread as profit.
4. **Input validation is incomplete.** Confirmed. See the math notes above.
5. **Pushes are ignored.** Confirmed. Over and Under model probabilities are forced to sum to 1 even on a whole-number line.
6. **No parlay model.** Confirmed. There is no parlay code. Nothing multiplies legs, and nothing warns about correlation either.
7. **Odds client is game markets, not player props.** Confirmed. `fetch_live_odds` calls `/v4/sports/{sport}/odds` with default markets `h2h,spreads,totals`. Official docs say that endpoint's markets are h2h, spreads, totals, and outrights. Player props are a different endpoint: `/v4/sports/{sport}/events/{eventId}/odds`, one event at a time.
8. **Freshness and missing markets.** Partial. HTTP errors return `ok: false`. There is no price timestamp, stale flag, or "not available from this source" record. A missing book in a user file is shown as `missing`, which is good. Quota headers are kept when a request is made. The API key is placed in the query string, so a `requests` exception can echo the key.
9. **Historical context is thin.** The backfill stores ESPN player name, athlete id, team, category, and the raw stat strings, plus a source URL. `player_log` then matches on name only. `PRA` uses `value or 0`, so a missing points/rebounds/assists component is stored as zero. `data/` game files are not checked in. `data_store.py` looks one directory above the repo (`parents[1] / "data"`) because the file used to live in `betting_model/`. At the repo root that path is wrong.
10. **Packaging is broken.** Confirmed. See the layout section.
11. **Tests cover core math only.** Confirmed. Ten tests. No CLI, odds, data-store, or validation tests.

## Odds provider, checked against current public docs

Pages read on November 22, 2026 (no API key, no live odds call):

- https://the-odds-api.com/liveapi/guides/v4/
- https://the-odds-api.com/sports-odds-data/betting-markets.html
- https://www.the-odds-api.com/sports-odds-data/bookmaker-apis.html

| Book the owner wants | What the docs actually list |
|---|---|
| FanDuel Indiana | US key `fanduel` only. No Indiana-specific key. A generic FanDuel price is not proof of the Indiana price. |
| DraftKings | US key `draftkings`. No paid-only note on the bookmaker table. |
| bet365 | No `bet365` key in the US, UK, or EU tables. `bet365_au` is Australian, paid, and limited to h2h, spreads, and totals for AFL and NRL. The code currently sends bookmaker key `bet365`, which is not on that list. An invalid key can fail the whole request (`INVALID_BOOKMAKERS`). |
| Fanatics | US key `fanatics`, note: "Only available on paid subscriptions." |

Player-prop market keys exist in the docs for NFL, NBA, WNBA, MLB, and NHL (for example `player_points`, `player_pass_yds`). They were not called from this checkout. Coverage still depends on the key's plan, the sport, and the book. A missing market means "not returned by this source," not "the sportsbook does not offer it."

No paid plan was added. No key was stored.

## What this upgrade will not pretend to do

- It will not invent projections, prices, injuries, or results.
- It will not replace sigma defaults with "calibrated" numbers. There are no archived pregame projections to measure forecast error.
- It will not fit a new distribution (Poisson, negative binomial, and so on) and claim it is better. There is no out-of-sample test to support that.
- It will not scrape sportsbooks.
- It will not call a live odds API in tests or in CI.
- It will not mark a prop qualified just because the old `positive_edge` flag is true.

## Implementation order on this branch

The spec suggested six pull requests. This run uses one branch and one pull request, with separate commits, because the later phases are blocked on data the repo does not have and the owner asked for one reviewable branch. Commit order:

1. This audit.
2. Put the package back so install, CLI, and tests work without renaming `__init__.py`.
3. Validation, vig labeling, no-vig math when both sides exist, push handling, expected value.
4. Separate recent averages from projections, and add plain-English statuses.
5. Calibration and backtest tools that run only on saved pregame snapshots.
6. Safer odds client, parlay warning (no fake joint probability), data-store checks, docs.

## Assumptions

- Restoring the `betting_model` package name is the right layout. The README, the console script, and the desk protocol already use that name. The spec's `the_edge/` tree was an example, not an instruction to rename a working API.
- Old fields `implied_prob`, `model_prob`, `edge`, and `positive_edge` keep their current formulas so old reports stay comparable. New fields carry the clearer names.
- Exactly 0.03 does not clear the threshold, matching `edge > 0.03` today.
- User-supplied `projected` values stay in the file. The program labels them as supplied by the file. It does not recompute them.
- Branch name is `cursor/model-reliability-upgrade-e47a` because this agent's branch rule requires that form. The spec's example name was `feature/model-reliability-upgrade`.
