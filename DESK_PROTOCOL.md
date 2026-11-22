# Desk protocol — betting_model (one-pager)

**Never place bets. Probability-first only.**

## When to run

```bash
python -m betting_model.cli evaluate --props props_tonight.json
```

Reports land in `./out/edges_latest.md` and `./out/edges_latest.json` (the folder you ran the command from).

A parlay file is separate, and it will not invent a combined probability:

```bash
python -m betting_model.cli parlay --ticket ticket.json
```

## How to read a row

| Signal | What to do |
|---|---|
| `status` is `QUALIFIED` | Only when the sigma and distribution have been calibrated. This version does not assign it. |
| `status` is `SCREEN_PASS_UNCALIBRATED` | The supplied projection and a fresh price cleared the 3-point threshold. The model is still uncalibrated. Not a bet by itself. |
| `status` is `WATCH` | Something is missing, stale, or close. Do not treat it as a green light. |
| `status` is `REJECTED` | The edge missed the threshold, or a data problem ruled it out. |
| `status` is `NOT EVALUABLE` | A required piece is missing, or the only number is a recent average. A recent average is not a prediction. |
| `positive_edge` is true | Older screen only: model probability minus the vig-inclusive price, above 3 points. It can be true on a row that is still Watch or Not evaluable. |
| No qualified rows | That is an acceptable result. Do not force a ticket. |

## Ownership

- **NBA Props / Lines & Market**: map the shortlist into the props JSON and verify prices.
- **Chief of Staff**: merges the report into the slate brief.
- **Muse**: owns the final NFL / World Series ticket. Model NFL legs are optional inputs only.
- **All desks**: the model is a screen, not a green light.

## What a props row should include

Required to be graded at all: player, sport, market, line, American odds, Over/Under, and a projection if you want a number.

Required before a row can clear the screen (it will still be `SCREEN_PASS_UNCALIBRATED`, not Qualified, until sigma is calibrated):

- `projection_source` — where the projection came from
- `projection_at` — when it was made, with a timezone (`2026-01-01T18:00:00-05:00`)
- `retrieved_at` — when the price was copied, with a timezone
- a market this program knows how to settle (a half-point line on a counting stat is the clear case)

Optional, and only when you really have the other side from the same book at nearly the same time:

```json
"opposite": {"odds": -110, "line": 19.5, "retrieved_at": "2026-01-01T18:00:30-05:00"}
```

Without that, the report uses the raw price and says the vig is still in it.

## Odds key (optional)

`export ODDS_API_KEY=...` for live odds. A missing key is a soft failure. No prices are invented.

See `docs/ODDS_PROVIDER.md` before expecting FanDuel Indiana, bet365, Fanatics, or player props from that key.
