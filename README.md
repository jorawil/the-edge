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

If `projected` is missing on an NBA row, the program can pull a recent average from `nba_api`. That average is shown and then marked **not evaluable**. It is not a forecast.

## How to read the report

The top of the report is the grade. The tables further down are the older screen and can disagree with the grade.

| Grade | Meaning |
|---|---|
| QUALIFIED | Reserved for a calibrated sigma and distribution. This version does not assign it. |
| SCREEN PASS UNCALIBRATED | The projection and a fresh price cleared the threshold. The sigma and the normal curve are still uncalibrated, so this is not Qualified. |
| WATCH | Close, or missing a timestamp, a source, or a fresh price. |
| REJECTED | The edge does not clear the threshold. |
| NOT EVALUABLE | Something required is missing, the market's pushes are not defined, or the only number is a recent average. |

Two different "edge" ideas:

- **Raw implied probability** is what the American odds say by themselves. The sportsbook's vig is still in that number. On a whole-number line that can push, that price is the chance of winning given that the bet does not push, so the pass/fail edge uses win / (win + loss). Expected value still uses the raw win, loss, and push chances. The old field `edge` is the original model probability minus the raw price, and it ignores pushes. `positive_edge` uses that old difference.
- **Fair probability** divides the two sides so the vig is taken out. It is filled in only when the file has both the Over and the Under for the same player, market, line, and book, with timestamps a few minutes apart. If either side is missing, the fair number is left blank.

Expected value is profit per 1 unit staked, using the win/loss/push split. It is omitted when that split is not defined. It is not a guaranteed profit.

Sigma values are the old defaults (points 6, rebounds and assists 3, rush or receiving yards 25, passing yards 40, and so on). They are **not** measured from forecast misses. The program will not replace them until pregame projections have been saved and checked. See the calibration section below.

## Supported sports and markets

Graded when you supply a projection:

| Sport | In the evaluator |
|---|---|
| NBA | yes, and a recent average can be fetched and is then marked not a forecast |
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

Calibration does **not** run off those box scores alone. It needs a projection that was saved before the game. Nothing in the current files is that archive, so there is no calibrated sigma and no claim that the model wins money.

The functions are `betting_model.calibration` and `betting_model.backtest`. They refuse a projection timestamp that is not before the game, they do not edit an old snapshot, and they do not change `SIGMA_DEFAULTS`.

## Pregame archive

The archive is a local SQLite file of evaluations saved **before** the game starts. It is the record you will need later to see whether projections and edges were any good. Saving a row does not make the model calibrated, and it does not make a Screen pass into Qualified.

What is saved, when you supply it: the player, event, market, side, line, your projection and where it came from, the price and when you copied it, the legacy edge, the no-vig numbers when both sides were present, and the screening grade. What you do not supply is stored as empty, not as zero. A FanDuel price is not marked as a verified Indiana price unless you also give `region`, `region_source`, and `region_verified: true`.

Timestamps are stored in UTC. The original text you typed is kept beside them. `archived_at` is the clock time of the save. You cannot set it. The projection time must be before the game, not after the save, and not more than six hours before the save. That blocks backdating a projection after you already know the result. If the game has started, the save is refused.

The same observation saved twice is rejected and the first row is left as it was. A later line or price is a new snapshot, so 24.5 at -110 and 25.5 at -115 are two records.

Results are attached afterward. They never edit the snapshot. A missing stat stays missing. A did-not-play, postponement, or cancellation is Void, not zero. A whole-number counting stat that lands on the line is a Push. A second result is added beside the first; it does not delete it. A correction note works the same way: the original number stays, and the note sits next to it.

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
```

The default file is `data/pregame_archive.sqlite`. Pass `--db` to use another path. `pregame.json` must include `event_id`, `event_start`, `projection_at`, `projection_source`, `retrieved_at`, and `book`, plus the usual prop fields. There is no historical archive in this repository. Nothing here was reconstructed from old averages.

## What this version does not do

- It does not place a bet or log in to a sportsbook.
- It does not build its own projection from minutes, pace, injuries, or weather.
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
