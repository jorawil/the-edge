# Odds provider notes

Checked on November 22, 2026 from the public pages below. No API key was used and no live prices were downloaded.

- https://the-odds-api.com/liveapi/guides/v4/
- https://the-odds-api.com/sports-odds-data/betting-markets.html
- https://www.the-odds-api.com/sports-odds-data/bookmaker-apis.html

## What the featured odds endpoint returns

`GET /v4/sports/{sport}/odds` documents these markets: `h2h`, `spreads`, `totals`, `outrights`.

That is the call `fetch_live_odds` makes. It does not return player props. If you ask it for a `player_` market, the client refuses the call so a key is not spent on the wrong endpoint.

## Where player props actually are

`GET /v4/sports/{sport}/events/{eventId}/odds`, one event at a time.

The docs list player-prop keys for NFL, NBA, WNBA, MLB, and NHL (for example `player_points`, `player_pass_yds`, `player_reception_yds`). Coverage still depends on the plan, the sport, and the book. A market that is missing from a response is "not available from this source." It is not proof that the sportsbook has no line.

`fetch_event_player_props` calls this endpoint. Tests use a fake response. It has not been run against a live key in this upgrade.

## Books

| Book you asked for | What the provider list shows | What this program does |
|---|---|---|
| FanDuel Indiana | US key `fanduel` only. No Indiana key. | Can request `fanduel`. Every record says the state is not verified. A user-typed FanDuel IN price is kept and labeled as coming from the file. |
| DraftKings | US key `draftkings`. No paid-only note on the bookmaker table. | Requested by default with FanDuel. State is not verified. |
| bet365 | No `bet365` key in the US, UK, or EU tables. `bet365_au` is paid and limited to h2h, spreads, and totals for AFL and NRL. | Not sent. Sending an unknown key can make the provider reject the whole request. |
| Fanatics | US key `fanatics`. Note: only on paid subscriptions. | Not requested unless you pass `bookmakers="fanatics"`. A paid plan was not added. |

Hard Rock has an Indiana note on the same page. That is a different book. It is not used as a stand-in for FanDuel Indiana.

## Quota and secrets

The key stays in the `ODDS_API_KEY` environment variable. Error text is scrubbed so a URL that contains `apiKey=` is stored as `REDACTED`. Quota headers (`x-requests-remaining`, `x-requests-used`) are kept when the provider sends them. The key is not copied into the report.

## Still unknown until you use a key

- Whether your plan includes player props.
- Whether Fanatics returns data on that plan.
- Whether any returned FanDuel number matches the Indiana app.

Those need a key and a side-by-side check against the book. This upgrade does not do that check.


## Manual one-event player-prop scan

A manual GitHub Actions workflow can compare the latest fresh archived point forecasts with current Odds API Over/Under prices for one selected NBA or NFL event. It makes at most one event-specific API request per workflow run and refuses to call the provider when the event has started, the forecast is older than six hours, or no requested market has a matching forecast.

From the repository's **Actions** tab, choose **Manual Event Player Prop Scan** and enter:
- Sport and the ESPN event ID shown in the daily forecast report.
- The separate Odds API event ID for the same matchup. Provider event IDs are not ESPN event IDs; copy the matching ID from the Odds API events list and confirm the teams and start time.
- Odds API market keys for the requested props.
- Bookmaker keys (default: `fanduel,draftkings`).

NBA keys supported by the scan: `player_points`, `player_rebounds`, `player_assists`, `player_threes`, `player_points_rebounds_assists`.

NFL keys supported by the scan: `player_pass_yds`, `player_rush_yds`, `player_reception_yds`, `player_receptions`. The forecast engine's `REC` market is catches and is mapped to `RECEPTIONS` for matching; it is not treated as receiving yards.

The workflow requires both IDs because the ESPN and Odds API use different event identifiers. It remaps provider prices to the ESPN event only after the user supplies both IDs; the existing collector still checks the sportsbook commence time against the archived forecast start before storing a price. The report records the actual quota headers returned by the provider, prices that were stored or rejected, and missing books. The number of credits used can depend on the requested markets; check the returned `requests_last` and `requests_remaining` values. This workflow is manual only—there is no daily automatic Odds API polling.

The FanDuel feed is the generic US feed, not verified as FanDuel Indiana. Prices are archived as FanDuel, never relabeled as Indiana. Forecasts are still uncalibrated point estimates; a screen pass is not a qualified bet. The workflow does not build or place tickets.
