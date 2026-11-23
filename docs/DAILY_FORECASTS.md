# Daily pregame forecast pipeline

The Daily Pregame Forecast Archive workflow runs at 10:00 UTC each day. It refreshes completed NBA/NFL ESPN box scores for yesterday and today, grades pending forecast snapshots against completed ESPN boxes, then creates new point forecasts for the current day’s regular-season slate. The scheduled horizon defaults to one day to limit overlapping snapshots and archive growth; a manual workflow dispatch can extend it to up to 14 days.

The pipeline uses ESPN public scoreboard and roster endpoints plus the historical game logs stored on the private-to-the-workflow `historical-data` branch (the branch is public because the repository is public). It does not call The Odds API and does not consume sportsbook API credits.

## Model choices

The market/window/shrinkage choices are fixed from the validation-only selections in the 2026-11-22 historical benchmark:

| Sport | Market | Method | Window | Shrinkage |
|---|---|---|---:|---:|
| NBA | PTS | Opportunity × efficiency | 10 | 20 |
| NBA | REB | Opportunity × efficiency | 20 | 20 |
| NBA | AST | Rolling mean | 20 | — |
| NBA | 3PM | Rolling mean | 20 | — |
| NBA | PRA | Opportunity × efficiency | 10 | 10 |
| NFL | PASS_YDS | Opportunity × efficiency | 20 | 20 |
| NFL | REC | Opportunity × efficiency | 20 | 20 |
| NFL | REC_YDS | Opportunity × efficiency | 20 | 20 |
| NFL | RUSH_YDS | Opportunity × efficiency | 5 | 20 |

These are candidate point-forecast configurations, not calibrated probability models. The validation report showed small differences from rolling means, not a demonstrated betting edge.

## Important limits

- ESPN roster membership is not a confirmed active lineup. Injuries, starters, teammate availability, role changes, opponent adjustments, pace, and game script are not modelled.
- A rostered player with insufficient prior history is skipped.
- PRA is a point estimate only; joint residual covariance is not available.
- No sigma, interval, calibrated probability, sportsbook line, edge, EV, or bet recommendation is produced.
- FanDuel's Odds API feed is not state-verified as Indiana. This workflow avoids sportsbook feeds entirely.
- Historical game stats are updated from ESPN; the pipeline records the data-store update timestamp as input provenance and never backdates a forecast.

Reports and the immutable SQLite forecast archive are committed to the historical-data branch. The grading report leaves missing players/stats unresolved rather than treating them as zero; listed DNPs are voided.
