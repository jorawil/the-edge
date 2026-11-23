# THE EDGE (betting_model)

This program compares a player prop with a projection. It tells you whether the gap is large enough to look at. **It does not place bets.**

You do not need to be a programmer to read the report. You do need a props file. The program will not invent the projection or the price.

## Install

From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

That installs `numpy`, `requests`, and `nba_api`. To run the tests as well:

```bash
pip install -e ".[dev]"
pytest -q
```

Python 3.10 or newer.

Optional live odds, only if you already have a key from https://the-odds-api.com/ :

```bash
export ODDS_API_KEY="paste-your-key-here"
```

Copy `.env.example` if you want a reminder of the name. Do not commit a real key. If the key is missing, odds calls say so and stop. They do not make up a price.

## Run a props file

```bash
python -m betting_model.cli evaluate --props props_rush_w11_sat.json
```

Writes, in the folder you ran the command from:

- `out/edges_latest.md` — the report to read
- `out/edges_latest.json` — the same rows for another program

Useful flags:

```bash
python -m betting_model.cli evaluate --props props_tonight.json --out out --save-as saturday
python -m betting_model.cli evaluate --props props_tonight.json --threshold 0.03
```

`--threshold` defaults to `0.03`, which means 3 percentage points. The edge must be **greater than** 0.03. Exactly 0.03 does not pass. `--save-as` is a file name, not a path.

A parlay ticket (legs are graded; the combined probability is not invented):

```bash
python -m betting_model.cli parlay --ticket ticket.json
```

## What a row needs

```json
{
  "player": "Example Player",
  "sport": "nfl",
  "market": "passing_yards",
  "line": 250.5,
  "odds": -110,
  "is_over": true,
  "projected": 270,
  "projection_source": "where this number came from",
  "projection_at": "2026-01-01T12:00:00-05:00",
  "retrieved_at": "2026-01-01T12:05:00-05:00",
  "book": "draftkings"
}
```

The example numbers above are a shape, not a real price and not a real projection.

`"is_over": "false"` is the Under. The word false is not treated as true.

The desk evaluator requires an explicit projection for NBA props. It does not fetch a recent average and use it as a fallback. Historical averages remain available as research context, but they are not forecasts.

## How to read the report

The report is diagnostic only. Its evaluation table shows the supplied projection, available price, projection-to-line gap in stat units, and row status. It does not publish a betting-edge shortlist or rank props by legacy probability fields.

| Grade | Meaning |
|---|---|
| QUALIFIED | Disabled until approved out-of-sample calibration evidence is integrated. No current evaluation can assign it. |
| SCREEN PASS UNCALIBRATED | A legacy screening condition was met, but the probability assumptions are uncalibrated. This is not an edge or a recommendation. |
| WATCH | Close, or missing a timestamp, a source, or a fresh price. |
| REJECTED | The legacy diagnostic condition did not clear its threshold. |
| NOT EVALUABLE | A required input is missing, a market settlement rule is unsupported, or vendor projections conflict. |

Probability and EV fields are diagnostic only. The current distribution and sigma assumptions are not calibrated, so no probability, edge, or EV output is a betting signal. The legacy \`positive_edge\` flag remains in raw JSON for compatibility only; ignore it. User-facing reports do not show it.

- **Raw implied probability** is what American odds say before removing the sportsbook's vig.
- **Fair probability** removes the vig only when both sides are present for the same player, market, line, book, and sufficiently close timestamps.
- **Push handling** separates win, loss, and push outcomes. Expected value is profit per 1 unit staked using that split, but remains an uncalibrated diagnostic—not a recommendation.

Conflicting projection values for the same event, player, and market make every affected row NOT EVALUABLE. The system does not average vendors or silently choose one. Exact duplicate observations are removed, while distinct books and prices remain separate.

Sigma values are the old defaults (points 6, rebounds and assists 3, rush or receiving yards 25, passing yards 40, and so on). They are **not** measured from forecast misses. The program will not replace them until pregame projections have been saved and checked. See the calibration section below.

## Supported sports and markets

Graded when you supply a projection:

| Sport | In the evaluator |
|---|---|
| NBA | explicit projection required; no recent-average fallback |
| WNBA | yes, projection required |
| NFL | yes, projection required |
| MLB | yes, projection required |
| NHL, college football | game logs can be stored; there is no prop grader yet |
| Anything else | the old math still runs if you supply a projection, and the grade cannot be Qualified |

Markets with a documented sigma: `PTS`, `REB`, `AST`, `PR`, `PRA`, `PA`, `MIN`, `RUSH_YDS`, `REC_YDS`, `RUSH_REC_YDS`, `PASS_YDS`, plus the usual names (`points`, `rushing_yards`, `player_receiving_yards`, and so on).

Other names, including receptions, completions, and pass attempts, still use the old generic sigma of 6. They are labeled unverified and cannot be Qualified. `REC` still means receiving yards, which is the old behavior. Use `receptions` for the counting stat.

Whole-number lines on those counting stats can push. The report splits win, loss, and push with a continuity correction on the same normal curve. That correction is not a new calibrated model. Half-point lines cannot push, and their legacy edge matches the previous program. Unusual fractions, and markets we have not listed, do not get a push number.

Distributions other than the normal curve (Poisson, negative binomial, and so on) were not added. There is no out-of-sample test that says they are better.

## Odds

Priority for books you type in: FanDuel IN, DraftKings, bet365, Fanatics. A missing book is "not in this file" or "not returned by this source." It is not "the book does not offer it."

Live calls need `ODDS_API_KEY`. What that key can actually return is written in `docs/ODDS_PROVIDER.md`. Short version: the default call is game markets for FanDuel and DraftKings, the FanDuel feed is not Indiana-specific, bet365 is not requested, and Fanatics is paid-only and not requested unless you ask.

## Historical games and calibration

ESPN box scores are not in this repository. Rebuild them, when you want, from the public ESPN endpoints:

```bash
python scripts/backfill_espn.py nba --start 2026-10-24 --end 2026-10-31
```

With no dates, the script uses October 24–31, 2026, which is the original window. It is safe to rerun: a complete game is skipped, and an incomplete box is fetched again. A missing stat is not stored as zero.

Historical projection benchmarks are available for NBA and NFL game logs. The original rolling-mean benchmark remains available, and the comparative benchmark evaluates rolling means (5/10/20 by default), season-to-date means, and linearly recency-weighted means over the same configured 5/10/20-game lookback windows. It reports chronological train/validation/test MAE, RMSE, and signed error. Method rankings are based on validation MAE only; the held-out test split is never used to select a method. Season-to-date history resets by ESPN season year. These are forecast baselines—not calibrated betting projections—and do not use odds, calculate EV, or evaluate profitability:

```bash
python scripts/walk_forward_backtest.py nba
python scripts/walk_forward_backtest.py nfl --windows 5,10,20 --min-history 3 --out out/nfl_baseline.json
python scripts/compare_projection_methods.py nba --windows 5,10,20 --min-history 3 --out out/nba_comparison.json
```

The benchmark requires explicit ESPN regular-season metadata (`season_type_id=2`), timezone-aware event timestamps, and stable athlete IDs. Missing or unknown season metadata, DNPs, missing IDs, and missing stat values are not converted to zero. Events with the same kickoff timestamp stay together in one chronological split, and all same-time forecasts are generated before any actual from that timestamp is added to history. Multiple fixed windows are reported separately; the held-out test slice is not used to select a winner. The JSON report flags test metrics with fewer than 30 events as descriptive only, not evidence of predictive skill. Even larger samples do not establish calibration or profitability.

Calibration does **not** run off those box scores alone. It needs a projection that was saved before the game. `betting_model.calibration` and `betting_model.backtest` score an older JSON snapshot file. `archive analyze` scores the SQLite archive. The archive writers refuse a projection that was not saved before the event, and the report leaves those rows out if one is ever stored. None of these tools edit an old snapshot, replace `SIGMA_DEFAULTS`, or mark a row Qualified. This repository does not ship a filled archive, so there is no calibrated sigma and no claim that the model wins money.

## Pregame archive

The archive is a local SQLite file of evaluations saved **before** the game starts. It is the record you will need later to see whether projections and edges were any good. Saving a row does not make the model calibrated, and it does not make a Screen pass into Qualified.

What is saved, when you supply it: the player, event, market, side, line, your projection and where it came from, the price and when you copied it, the legacy edge, the no-vig numbers when both sides were present, and the screening grade. What you do not supply is stored as empty, not as zero. A FanDuel price is not marked as a verified Indiana price unless you also give `region`, `region_source`, and `region_verified: true`.

Timestamps are stored in UTC. The original text you typed is kept beside them. `archived_at` is the clock time of the save. You cannot set it. The projection time must be before the game, not after the save, and not more than six hours before the save. That blocks backdating a projection after you already know the result. If the game has started, the save is refused.

The same observation saved twice is rejected and the first row is left as it was. A later line or price is a new snapshot, so 24.5 at -110 and 25.5 at -115 are two records.

Results are attached afterward. They never edit the snapshot. A result time before kickoff is refused. A missing stat stays missing. A did-not-play, postponement, or cancellation is Void, not zero. An incomplete or suspended game stays Unknown until it is final. If a file contains both the Over and the Under for the same player, game, line, and book at nearly the same time, the no-vig price is stored. Otherwise that field stays empty. A whole-number counting stat that lands on the line is a Push. A second result is added beside the first; it does not delete it. A correction note works the same way: the original number stays, and the note sits next to it.

```bash
python -m betting_model.cli archive add --props pregame.json
python -m betting_model.cli archive list --player "Example Player"
python -m betting_model.cli archive show --id <archive-id>
python -m betting_model.cli archive attach-result --id <archive-id> \
  --game-status final --actual 18 --source "ESPN box" \
  --retrieved-at 2026-11-24T05:00:00Z
python -m betting_model.cli archive incomplete
python -m betting_model.cli archive export --out archive.json
python -m betting_model.cli archive stats
python -m betting_model.cli archive analyze
python -m betting_model.cli archive analyze --db data/pregame_archive.sqlite --out out
python -m betting_model.cli archive analyze --format json
```

`archive analyze` does not modify archived snapshots, results, or corrections. Opening an existing database can apply a pending schema migration, the same way the other archive commands do, so the command is not strictly read-only when a migration is pending. It reports how many predictions were archived, how many settled, how many can be scored, and why the rest were left out. Conditional win probability (pushes left out), unconditional win/loss/push probabilities, and projection error (MAE, RMSE, and bias) are separate. Each of those groups also stays inside one archived model version. A missing version is labeled and is not combined with a known version. A snapshot with two results that nobody superseded is left out of the scores. Events are split by kickoff so a later result is not used to score an earlier prediction. A small sample is printed with a warning. The report does not claim the model is calibrated, does not change Qualified, and does not report profit or ROI. If `--db` points at a file that is not there, the report says the archive is empty and does not create the file. `--out` writes `calibration_report.md` and `calibration_report.json`.

The default file is `data/pregame_archive.sqlite`. Pass `--db` to use another path. `pregame.json` must include `event_id`, `event_start`, `projection_at`, `projection_source`, `retrieved_at`, and `book`, plus the usual prop fields. There is no historical archive in this repository. Nothing here was reconstructed from old averages.

## Daily pregame forecasts

The scheduled workflow refreshes NBA/NFL box scores, grades prior forecast snapshots, and archives new point forecasts for upcoming games. It does not fetch odds or generate betting recommendations. See `docs/DAILY_FORECASTS.md` for the market configurations and limitations.

## Forecast-only archive

The separate `betting_model.forecast_archive` stores immutable pregame point forecasts even when no sportsbook price is available. Results are appended later, and corrections supersede earlier results without rewriting them. This is the source for genuine forecast-error samples used by future calibration work. It does not calculate betting edge or qualify a pick. See `docs/FORECAST_ARCHIVE.md`.

## Collect prices and grade results

Collection writes the same archive. It does not place a bet, and a successful collection is not a claim that the model is calibrated or profitable. Details and provider limits are in `docs/DATA_COLLECTION.md`.

```bash
python -m betting_model.cli archive collect --props pregame.json
python -m betting_model.cli archive grade --results-fixture results.json
python -m betting_model.cli archive quality
python -m betting_model.cli archive analyze
```

`archive grade` without `--results-fixture` reads public ESPN box scores. That source needs no key. It does not turn a missing stat into zero, and it does not grade a game that is not final. A FanDuel price from The Odds API is stored as FanDuel, not as FanDuel Indiana, unless the prop itself includes a verified Indiana region and a source for that check.

Live current props, only when you already have a key:

```bash
python -m betting_model.cli archive collect --projections projections.json \
  --odds-live --sport-key basketball_nba --event EVENT_ID --markets player_points
```

The historical odds endpoints are paid and are not called. The archive would reject a backdated projection anyway. `archive run-once --config schedule.json` runs at most one pass and only when the file says `"enabled": true`. It does not start a background service.

## Run historical collection and benchmarks from GitHub

These workflows are designed to run in GitHub without a local computer. They become available after their pull requests are merged into `main`.

1. Open the repository on GitHub and select **Actions**.
2. Choose **Historical Data Backfill**, select NFL or NBA, keep season type `2` for regular season, and run the workflow. The default date range is September 1 through November 22, 2026; change it when collecting another period.
3. Run the other sport separately. The workflow saves public ESPN game logs to the separate `historical-data` branch. It does not use Odds API credits.
4. After both sport folders exist on `historical-data`, choose **Historical Baseline Benchmark** and run **both**.
5. Open the completed benchmark run to view its artifact. The JSON reports are also committed under `reports/` on the `historical-data` branch.

The workflow produces rolling-window reports, comparative method reports, and field-coverage audits for each selected sport. The comparison includes rolling means, season-to-date means, and recency-weighted means. It ranks methods on validation MAE, while reporting test results separately without using them for selection. A feature audit reports the availability of historical box-score fields without exposing player identities. These remain historical baselines—not calibrated player projections, sportsbook edge, EV, or profit tests. Empty or small samples must not be treated as evidence of predictive skill. The workflow requires the historical-data branch and the selected sport's game file; run backfill first.

The opportunity-aware benchmark additionally tests lagged opportunity/rate projections: NBA points, rebounds, and assists use prior minutes and per-minute production; NBA 3-pointers use prior three-point attempts and makes per attempt; NFL passing yards use prior pass attempts and yards per attempt; receptions and receiving yards use prior targets and catch/yard rates; rushing yards use prior rush attempts and yards per attempt. It compares each candidate with a rolling-mean baseline on the exact same eligible player-game sample for each market and window. It only uses prior appearances, groups simultaneous games before adding outcomes, and reports validation rankings separately from held-out test results. Historical opportunity is not a live pregame minutes/targets/attempts projection, and the method is not calibrated.

The new v1 projection engine is a separate candidate model. It forecasts opportunity from prior player games and multiplies by an efficiency rate, with an optional league-rate shrinkage prior. Its validation script compares it with a rolling-mean baseline on identical eligible samples, chooses parameters on validation only, and reports held-out test results separately:

```bash
python scripts/validate_projection_engine.py --sport nba --data-root data
python scripts/validate_projection_engine.py --sport nfl --data-root data
```

This v1 engine does not apply opponent, rest, home/away, injury, lineup, or role adjustments. It can produce a PRA point estimate from PTS + REB + AST, but does not model their joint residual covariance or PRA uncertainty. It does not create intervals, estimate calibrated probabilities, or qualify bets. Full methodology and limitations: `docs/PROJECTION_ENGINE_V1.md`.

## What this version does not do

- It does not place a bet or log in to a sportsbook.
- It does not use live projected minutes, attempts, targets, pace, injuries, lineup changes, or weather.
- It does not know who is hurt unless you put that in the file (`role_uncertain` or `injury_status`).
- It does not estimate parlay correlation. Every parlay is `correlation_not_modeled`.
- It does not scrape book websites.
- It has not been run against a live odds key in this upgrade.

An empty Qualified list is a valid answer.

## Layout

```text
betting_model/     the package (python -m betting_model.cli)
scripts/backfill_espn.py
tests/
docs/BASELINE_AUDIT.md
docs/ODDS_PROVIDER.md
```

The old root-level modules were moved back into `betting_model/` so install and tests work without renaming `__init__.py`.
