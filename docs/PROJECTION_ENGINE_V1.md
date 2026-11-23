# Projection engine v2: separate opportunity × efficiency windows

This is a research-stage projection engine, not a qualified betting model.

## Model definition

The engine forecasts expected opportunity from a player's shorter or equal trailing opportunity history, then multiplies it by an efficiency estimate from a separately selected trailing history:

- NBA PTS / REB / AST: minutes × production per minute.
- NBA 3PM: three-point attempts × makes per attempt.
- NFL PASS_YDS: pass attempts × yards per attempt.
- NFL RUSH_YDS: rush attempts × yards per attempt.
- NFL REC_YDS: receiving targets × yards per target.
- NFL REC: receiving targets × catch rate.

The efficiency estimate may be shrunk toward a league rate known before the target event. The shrinkage strength is a candidate parameter selected on the validation split, not a hand-tuned production constant. The v2 live engine uses separate trailing windows for expected opportunity and efficiency where held-out event-bootstrap comparisons support the change. It retains a league-wide prior because position-specific priors are not yet validated.

## Leakage controls

The new validation script reuses the existing historical backtest's event rows and chronological event split. It:

1. Requires regular-season rows with stable ESPN athlete IDs.
2. Uses only prior appearances for the player's opportunity and efficiency.
3. Builds league efficiency priors only from earlier event-time observations.
4. Scores every game at the same timestamp before appending any outcomes from that timestamp.
5. Compares opportunity-rate projections with a rolling-mean baseline on the same eligible player-game sample.
6. Selects the candidate by validation MAE/RMSE and reports its held-out test result separately. The test split does not select the model.

Example:

```bash
python scripts/validate_projection_engine.py --sport nba --data-root data
python scripts/validate_projection_engine.py --sport nfl --data-root data
```

The output is JSON and can be saved with --out reports/projection_nba.json. The separate opportunity/rate benchmark also reports expected-opportunity MAE, RMSE, signed bias, and validation-selected lookback windows for minutes, attempts, and targets. The separate-window benchmark tests opportunity and efficiency lookbacks independently (5/10/20 by default), evaluates all combinations on the same eligible target rows, and selects the pair using validation MAE/RMSE only. It reports the corresponding held-out test result and a rolling-mean comparison separately. The selected candidate is also compared with the matching rolling-mean baseline using a paired bootstrap that resamples whole test events; the 95% interval is for candidate MAE minus baseline MAE. This avoids treating thousands of correlated player rows as independent. The diagnostic is only reported when at least 30 paired test events are available and never changes the live configuration automatically. This matters because an accurate efficiency estimate cannot rescue a poor minutes/attempts forecast. Neither benchmark changes the live model configuration automatically. The scripts do not place bets, fetch prices, calculate EV, or create probability distributions.

## Live v2 lookback selections

The separate-window benchmark (run 45095216422) selected the following NBA settings on validation. The held-out test comparison then used 1,003 events and a 1,000-resample paired event bootstrap:

| NBA market | Opportunity window | Efficiency window | Shrinkage | Held-out test result vs rolling mean |
|---|---:|---:|---:|---|
| PTS | 5 | 20 | 5 | Candidate better; MAE difference 95% CI [-0.089, -0.041] |
| REB | 5 | 20 | 20 | Candidate better; CI [-0.034, -0.015] |
| AST | 5 | 20 | 0 | Candidate better; CI [-0.028, -0.015] |
| PRA | 5 | 20 | 5 | Candidate better; CI [-0.165, -0.084] |
| 3PM | Rolling mean 20 | — | — | Rolling mean better; candidate CI [0.042, 0.047] |

For NFL, the 255-event held-out test supported the existing 5/5/20 rush-yard setting. The separate-window comparisons for passing yards, receptions, and receiving yards were inconclusive, so those live settings were left unchanged. The NBA changes use distinct 5-game opportunity and 20-game efficiency histories; 3PM remains a 20-game rolling mean. These are point-forecast improvements only, not evidence of calibrated betting probabilities or profitability.

The live generator now records both lookback windows and the number of observations used by each component in the forecast metadata. Historical snapshots remain immutable and retain their original model versions.

## Deliberate limitations

- The opportunity forecast is currently a prior-game average. The live v2 lookbacks are validation-selected and event-bootstrap checked, but still omit future role and lineup context.
- No opponent, home/away, rest, pace, game-script, injury, lineup, teammate-out, starter/bench, or role-change adjustment is applied yet.
- The league efficiency prior is not position-specific.
- ESPN's historical game timestamps are present, but ingestion timestamps are not. The feature builder therefore leaves projection_input_at unknown instead of inventing it.
- PRA is projected as PTS + REB + AST for a point estimate. Its joint uncertainty requires covariance from archived PTS/REB/AST forecast residuals, so no PRA sigma or interval is produced.
- Sigma, 80%/90% intervals, and calibrated distributions are intentionally absent. The engine always returns calibrated=false.
- A projection from this engine cannot receive QUALIFIED status.

The next research steps are to add and validate schedule/opponent features, improve opportunity forecasting (especially NBA minutes), and collect genuine pregame forecast snapshots. Only then can residual distributions be fitted and evaluated in a separate calibration gate.
