# Projection engine v1: opportunity × efficiency

This is a research-stage projection engine, not a qualified betting model.

## Model definition

The engine forecasts expected opportunity from a player's prior opportunities, then multiplies by an efficiency estimate:

- NBA PTS / REB / AST: minutes × production per minute.
- NBA 3PM: three-point attempts × makes per attempt.
- NFL PASS_YDS: pass attempts × yards per attempt.
- NFL RUSH_YDS: rush attempts × yards per attempt.
- NFL REC_YDS: receiving targets × yards per target.
- NFL REC: receiving targets × catch rate.

The efficiency estimate may be shrunk toward a league rate known before the target event. The shrinkage strength is a candidate parameter selected on the validation split, not a hand-tuned production constant. The v1 benchmark uses a league-wide prior because position-specific priors are not yet validated.

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

The output is JSON and can be saved with --out reports/projection_nba.json. The separate opportunity/rate benchmark also reports expected-opportunity MAE, RMSE, signed bias, and validation-selected lookback windows for minutes, attempts, and targets. This matters because an accurate efficiency estimate cannot rescue a poor minutes/attempts forecast. The held-out test result is reported only after the lookback is selected on validation. The script does not place bets, fetch prices, calculate EV, or create probability distributions.

## Deliberate limitations

- The opportunity forecast is currently a prior-game average. Expected-opportunity diagnostics are reported separately so NBA minutes and NFL attempts/targets can be assessed independently of efficiency. The diagnostic does not itself change the live forecast method.
- No opponent, home/away, rest, pace, game-script, injury, lineup, teammate-out, starter/bench, or role-change adjustment is applied yet.
- The league efficiency prior is not position-specific.
- ESPN's historical game timestamps are present, but ingestion timestamps are not. The feature builder therefore leaves projection_input_at unknown instead of inventing it.
- PRA is projected as PTS + REB + AST for a point estimate. Its joint uncertainty requires covariance from archived PTS/REB/AST forecast residuals, so no PRA sigma or interval is produced.
- Sigma, 80%/90% intervals, and calibrated distributions are intentionally absent. The engine always returns calibrated=false.
- A projection from this engine cannot receive QUALIFIED status.

The next research steps are to add and validate schedule/opponent features, improve opportunity forecasting (especially NBA minutes), and collect genuine pregame forecast snapshots. Only then can residual distributions be fitted and evaluated in a separate calibration gate.
