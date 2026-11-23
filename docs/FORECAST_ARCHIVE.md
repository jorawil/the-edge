# Immutable forecast archive

The SQLite forecast archive records point forecasts before an event without requiring sportsbook odds. It is separate from the prop/evaluation archive, which requires a line and price.

Each snapshot stores a stable player ID, event and kickoff, market, projected value, optional opportunity and efficiency components, model version, projection timestamp, optional input-data timestamp and source, warnings, and the original payload. Snapshots cannot be updated or deleted. Results are appended later; a correction must explicitly supersede an earlier result.

Example Python use:

```python
from pathlib import Path
from betting_model.forecast_archive import archive_forecast, attach_forecast_result, forecast_metrics

saved = archive_forecast(snapshot, Path("data/forecasts.sqlite"))
# After the event is final, attach the sourced actual stat:
attach_forecast_result(
    saved["snapshot_id"], actual_stat=24, game_status="final",
    result_source="ESPN", retrieved_at="2026-11-24T05:00:00Z",
    path=Path("data/forecasts.sqlite"),
)
report = forecast_metrics(Path("data/forecasts.sqlite"))
```

The archive refuses a projection made after kickoff, a stale projection, an input-data timestamp after the projection, and postgame fields in the original snapshot. It does not allow callers to backdate the archive clock or attach a result with a future retrieval timestamp. A missing input-data timestamp remains missing; it is never inferred from the game date.

`forecast_metrics` reports forecast-error sample counts, MAE, RMSE, and signed bias by sport, market, and model version. It does not claim calibrated probabilities, evaluate sportsbook odds, calculate ROI, or assign `QUALIFIED`. A residual distribution must be fit and evaluated chronologically in the separate calibration gate.

For error metrics, multiple daily snapshots for the same player, event, market, and model version are not counted as independent observations. The metrics select the latest available pregame snapshot for that event/player/market/model. All immutable snapshots remain stored for audit and comparison.

The scheduled workflow stores the SQLite archive as `forecasts/forecast_archive.sqlite.gz` on the historical-data branch to reduce repository size. It decompresses the file before grading/generation, then recompresses it before committing. The runtime archive path remains `forecasts/forecast_archive.sqlite`.


## Residual interval diagnostics

The daily workflow runs `scripts/validate_forecast_distributions.py` after grading. It reads only active final results from the immutable SQLite archive and keeps the latest pregame snapshot per event/player/market/model version. Events are split chronologically 60/20/20 so one game cannot appear in more than one slice.

The report compares central 80% and 90% residual intervals from two diagnostics: a Normal residual fit and empirical residual quantiles. Both are fitted on the earliest training slice; validation reports coverage, width, and interval score; the later test slice is reported separately. Groups below the minimum fit or validation sample remain explicitly insufficient.

This is deliberately not yet a probability calibration system: the forecast archive does not contain sportsbook lines/prices for every forecast, and interval coverage alone does not validate Over/Under probabilities, Brier score, log loss, or betting edge. No distribution is selected or registered, no sigma default changes, and no forecast is promoted to `QUALIFIED`. More distribution families and market-price probability tests require a separately reviewed validation design and sufficient settled data.
