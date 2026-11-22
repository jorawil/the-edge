# Data collection and grading

Checked on November 22, 2026. No API key was used. No live price was stored. The examples in the tests are fixtures, not collected history.

This pipeline stores a pregame evaluation and, later, a verified outcome. It does not place a bet. Counts from `archive quality` are not calibration, profit, ROI, or a betting edge. `archive analyze` is still the report for probabilities and projection error, and it still does not claim those things.

## What was already here

- The SQLite archive stores a pregame snapshot and refuses changes to that row. A repeated observation is rejected. A new line or price is a new row. `archived_at` is the clock, and a projection more than six hours earlier, after the clock, or not before kickoff is refused.
- Results and corrections are append-only. A second result supersedes the first and leaves it in place. Settlement is Win, Loss, Push, Void, or Unknown. A missing stat is not zero. Did-not-play, postponement, and cancellation are Void. An incomplete or suspended game is Unknown.
- `archive analyze` scores what was stored. It does not rewrite rows.
- The Odds API client can request current featured markets and current player props for one event. It does not invent a missing book. Tests use a fake response.
- ESPN box scores can be saved with `scripts/backfill_espn.py`. That file is a local game log. It was not wired to the pregame archive.

## What this milestone adds

`archive collect` writes snapshots through the existing archive. `archive grade` reads a result source and calls `attach_result`. `archive quality` counts the archive and the command journal. `archive run-once` is an optional single pass for cron. There is no daemon and no sportsbook login.

The journal is `data/collection_journal.sqlite`. It records accepts, duplicates, rejections, conflicts, and provider failures. The archive remains the snapshot and the result.

## Odds

| Book | Verified on the provider list | What collection does |
|---|---|---|
| FanDuel Indiana | US key `fanduel` only. No Indiana key. Hard Rock has an Indiana note and is a different book. | A feed price is stored as `fanduel` with region unverified. It is stored as FanDuel Indiana only when the prop has `region`, `region_source`, and `region_verified: true`, and the region is Indiana. |
| DraftKings | US key `draftkings`. | Requested by default on a live collect. State is not verified. |
| bet365 | No US, UK, or EU key. `bet365_au` is paid and limited to AFL and NRL game markets. | Not sent. A price you typed in a props file is stored as the book you typed. That is your copy, not an API result. |
| Fanatics | US key `fanatics`, paid subscriptions only. | Not requested unless you pass `--bookmakers fanatics`. A refusal is not a price. |

Current player props are `GET /v4/sports/{sport}/events/{eventId}/odds`, one event at a time. The featured `/odds` call does not return player props. Both need `ODDS_API_KEY`. If the key is missing, nothing is archived.

Historical odds are a separate paid API: `GET /v4/historical/sports/{sport}/odds` and `GET /v4/historical/sports/{sport}/events/{eventId}/odds`. Player-prop history on that event call is documented from 2023-05-03 and is paid. This program does not call either endpoint. Replaying an old price after the game would also fail the archive clock. Collection is for a price you have before the game starts.

A provider `last_update` older than 30 minutes, or a missing `last_update`, is rejected. The archive's own rules still apply.

## Results

Final stats come from ESPN's public site API. No key. The paths checked on November 22, 2026:

- NBA `basketball/nba` scoreboard and summary. Points, rebounds, assists, and made threes. A rebound value of `"0"` was present next to a non-zero point total. A did-not-play row had an empty stat list.
- NFL `football/nfl` summary. Passing, rushing, and receiving categories use the keys this mapper reads. Passing touchdowns `"0"` occurred with non-zero passing yards.
- NHL `hockey/nhl` summary. Skater `goals`, `assists`, and `shotsTotal`. Goalie `saves`. Season-to-date `ytdGoals` is not used as the game total.
- MLB `baseball/mlb` summary. Batting and pitching are separate, because both categories use names such as `hits` and `strikeouts`.
- WNBA `basketball/wnba` scoreboard path returned HTTP 200. A WNBA box was not downloaded. The NBA key map is used, and a missing key stays missing.
- College football uses the same football keys and the same FBS `groups=80` filter as `scripts/backfill_espn.py`.

Status names that are graded: `STATUS_FINAL` and the other final names only when `completed` is true; postponed; canceled. Suspended, delayed, in progress, and scheduled are left unresolved. Any other name is unresolved. Nothing is attached before the archived start time. The archive refuses a result timestamp before kickoff, so a postponement announced earlier waits until that time and is then stored as Void.

Player ids must match when the snapshot has one. The name is not a fallback for a different id. Names match with accents and punctuation folded, and a trailing Jr/Sr/II/III/IV only when that leaves one person. A first initial matches one full name. A last name alone does not match. Two matches are a conflict and nothing is attached.

A missing market stays missing. It is not graded as a loss. Two different values for one market are a conflict. An existing Win, Loss, Push, or Void is not replaced. An unresolved row can be superseded by a later verified result. That is the existing correction path: the old row stays.

If the archived event id is not on ESPN, grading will use a game only when both teams and the start time (within two minutes) identify one event. Otherwise it stops.

## Commands

```bash
python -m betting_model.cli archive collect --props pregame.json
python -m betting_model.cli archive collect --projections projections.json --odds-fixture odds.json
python -m betting_model.cli archive grade --results-fixture results.json
python -m betting_model.cli archive grade --delay 0.25
python -m betting_model.cli archive quality
python -m betting_model.cli archive analyze
```

`--results-fixture` must be JSON with `"kind": "result_fixture"` and a `source_label`. A file is not described as a live ESPN fetch.

`archive run-once --config schedule.json` does nothing unless the file says `"enabled": true`. After a clean run it waits `min_minutes_between_runs` (default 60). A provider failure does not start that wait. Do not put an API key in the config. The schedule does not call the live odds API; put prices in `--props` or an odds fixture. Grading uses ESPN unless the config names a result fixture.

```json
{
  "enabled": false,
  "min_minutes_between_runs": 60,
  "props": "pregame.json",
  "grade": true
}
```

## Environment

`ODDS_API_KEY` is the only secret, and only for a live collect. ESPN does not need one. Nothing in this repository is a key.
