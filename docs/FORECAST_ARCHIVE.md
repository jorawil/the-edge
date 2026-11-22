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
