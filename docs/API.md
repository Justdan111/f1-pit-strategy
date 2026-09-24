# API reference

Two surfaces: a small REST API, and a WebSocket that streams a race. The
WebSocket is the product; the REST routes exist for health checking, debugging
and reviewing past runs.

Base URL in production: `https://f1-pit-strategy.onrender.com`

## Contents

- [Authentication](#authentication)
- [REST endpoints](#rest-endpoints)
- [WebSocket: streaming a race](#websocket-streaming-a-race)
- [Message protocol](#message-protocol)
- [Error codes](#error-codes)
- [Configuration](#configuration)

---

## Authentication

Only the WebSocket is authenticated, and only when `F1_API_KEYS` is set on the
server. When it is unset the stream is open — right for local development,
wrong for a deployed service. `/health` reports `websocket_auth_required` so
you can tell which you are talking to, without the keys being echoed.

Browsers cannot set headers on a WebSocket handshake, so two channels are
accepted:

| channel | for | example |
|---|---|---|
| `Authorization: Bearer <key>` | non-browser clients | `websockets.connect(url, additional_headers={"Authorization": "Bearer k"})` |
| `Sec-WebSocket-Protocol: f1key.<key>` | browsers | `new WebSocket(url, ["f1key.k"])` |

A query parameter is **not** accepted. Uvicorn's access log writes the full
path including the query string, so every connection would write a live key to
disk.

When a subprotocol is offered the server echoes it back on accept — including
on refusals, since a browser closes the connection if the server names nothing
back, and a rejection the client cannot read is indistinguishable from a crash.

Several keys may be configured at once, which is what makes a key rotatable
without downtime.

> The frontend's key is a `NEXT_PUBLIC_*` value compiled into the browser
> bundle and readable by anyone. It deters casual abuse of a rate-limited
> backend and can be rotated; it does not authenticate users. A public
> single-page app cannot hold a secret.

---

## REST endpoints

### `GET /health`

Liveness probe and effective configuration. Does no I/O — a health check that
called OpenF1 would report the service unhealthy whenever somebody else's API
was down.

```json
{
  "status": "ok",
  "service": "f1-pit-strategy",
  "modes_available": ["replay", "live"],
  "live_poll_interval_seconds": 10.0,
  "live_window_margin_minutes": 30,
  "replay_tick_interval_seconds": 0.5,
  "pit_lane_cost_seconds": 22.0,
  "min_samples_for_fit": 3,
  "allowed_origins": ["*"],
  "websocket_auth_required": false
}
```

`allowed_origins` and `websocket_auth_required` are echoed so a
misconfiguration is visible from a `curl`, rather than discovered as a frontend
that will not connect. Keys themselves are never echoed.

Also the endpoint to hit to wake a sleeping free-tier service before a session.

### `GET /race/sample/stints`

**Debug only, not the product surface.** The raw stints of the offline fixture,
before any flattening — so that when a tick looks wrong you can compare it
against the input and tell whether the bug is in the data or in the builder.

```json
[
  {"driver_number": 1, "stint_number": 1, "lap_start": 1, "lap_end": 18,
   "compound": "MEDIUM", "tyre_age_at_start": 0,
   "session_key": null, "meeting_key": null}
]
```

### `GET /runs`

Recent recorded streams, newest first.

| parameter | type | default | |
|---|---|---|---|
| `limit` | int, 1–200 | 50 | how many runs |

```json
{
  "enabled": true,
  "runs": [
    {
      "id": 2,
      "session_key": "9904",
      "source": "historical_replay",
      "driver_number": 1,
      "total_laps": 51,
      "started_at": "2026-09-17T09:14:02.113Z",
      "ended_at": "2026-09-17T09:14:03.247Z",
      "total_ticks": 51,
      "total_decisions": 42,
      "end_reason": "completed"
    }
  ]
}
```

`enabled: false` with an empty list means the decision log is switched off or
could not be opened. The service still streams.

`end_reason` is one of `completed`, `client_disconnected`, `no_live_session`,
`error:<code>` or `internal_error` — so a partially-watched race is not
mistaken for a complete one.

### `GET /runs/{run_id}/decisions`

Every decision from one run, in lap order. This is how a finished race is
reviewed after the fact.

```json
{
  "run": { "...as above..." },
  "decisions": [ { "type": "decision", "lap": 4, "..." } ]
}
```

Each element is the complete `decision` message exactly as it was sent.

**404** if the run does not exist, or if the decision log is disabled.

### `GET /`

A signpost listing the endpoints, so hitting the root in a browser is not a
404.

---

## WebSocket: streaming a race

```
WS /ws/race/{session_key}
```

| parameter | in | default | |
|---|---|---|---|
| `session_key` | path | — | `sample`, `latest`, or a numeric OpenF1 key |
| `mode` | query | `replay` | `replay` or `live` |
| `driver_number` | query | — | defaults to the car that ran furthest |
| `tick_interval` | query | server default | seconds between ticks; server-clamped |
| `total_laps` | query | from the data | race distance. Only needed in **live** mode, where OpenF1 reports no lap count for a session in progress. Without it the verdict falls back to a one-lap comparison and says so |

### Choosing a session key

| | meaning |
|---|---|
| `sample` | the offline fixture: 58 laps, no network. **Replay only.** |
| a number, e.g. `9904` | an OpenF1 session. Replay: a finished race. Live: that specific session. |
| `latest` | OpenF1's shortcut for the most recent or currently-running session. Used with `mode=live`. |

### Examples

```bash
# offline fixture, paced fast
wscat -c 'ws://localhost:8000/ws/race/sample?mode=replay&tick_interval=0.05'

# a real finished race, one driver
wscat -c 'ws://localhost:8000/ws/race/9904?mode=replay&driver_number=1'

# whatever is running now
wscat -c 'wss://f1-pit-strategy.onrender.com/ws/race/latest?mode=live'
```

The repo ships a client, since curl cannot speak WebSocket:

```bash
uv run python scripts/ws_client.py --tick-interval 0.05
uv run python scripts/ws_client.py --session-key 9904 --driver-number 1
uv run python scripts/ws_client.py --session-key latest --mode live
uv run python scripts/ws_client.py --api-key "$F1_API_KEY"
```

### Message order

```
start  →  (tick  →  decision?)*  →  end
```

or a single terminal message instead: `error`, or `no_live_session`.

A `decision` follows a `tick` only when the engine has enough clean samples of
the current compound. Early laps of a stint produce ticks with no decision —
normal, not a failure. In the sample race, 58 ticks produce 49 decisions: the
nine missing are the first three laps of each of the three stints.

The connection is accepted before anything is validated, so a rejection arrives
as a readable `error` rather than an opaque handshake failure.

---

## Message protocol

Every message has a `type`. The union is discriminated on it, on both sides.

### `start`

First message on every stream. Describes what is coming.

| field | type | |
|---|---|---|
| `type` | `"start"` | |
| `session_key` | string | |
| `source` | `"sample"` \| `"historical_replay"` \| `"live"` | **what the server actually did**, not what was requested |
| `total_laps` | int \| null | **null in live mode** — a race in progress has no known total |
| `driver_number` | int \| null | |

```json
{"type": "start", "session_key": "sample", "source": "sample",
 "total_laps": 58, "driver_number": 1}
```

`source` is the field a dashboard should display. A replayed historical race is
never `live`, however live it looks.

### `tick`

One lap of one driver. **Identical shape in every mode** — the decision engine
cannot tell which source produced it.

| field | type | |
|---|---|---|
| `type` | `"tick"` | |
| `lap` | int | |
| `driver_number` | int | |
| `compound` | string | `SOFT`, `MEDIUM`, `HARD`, `INTERMEDIATE`, `WET`, `UNKNOWN` |
| `tyre_age` | int | laps on this set, counting a used set's prior life |
| `stint_number` | int | |
| `lap_duration_s` | float \| null | **null when the lap was never completed** — render as a dash, never as 0 |
| `is_pit_out_lap` | bool | excluded from the degradation fit |

```json
{"type": "tick", "lap": 19, "driver_number": 1, "compound": "HARD",
 "tyre_age": 0, "stint_number": 2, "lap_duration_s": 108.654,
 "is_pit_out_lap": true}
```

There is deliberately no mode field. Mode is stated once, in `start`.

### `decision`

The pit/stay call and every number behind it, so the verdict can be recomputed
by hand and disagreed with.

**The verdict**

| field | type | |
|---|---|---|
| `type` | `"decision"` | |
| `lap`, `driver_number`, `compound`, `tyre_age` | | which lap this is about |
| `verdict` | `"pit_now"` \| `"stay_out"` | |
| `verdict_basis` | `"race_remaining"` \| `"next_lap_only"` | which question was answered |
| `laps_remaining` | int \| null | null when the race distance is unknown |
| `net_gain_s` | float \| null | **seconds saved by stopping now, over the laps that remain.** What the verdict is based on |

**The fitted curve**

| field | type | |
|---|---|---|
| `current_compound_degradation_s_per_lap` | float | slope, **fuel-corrected** |
| `raw_degradation_s_per_lap` | float | the uncorrected slope, so the adjustment is auditable |
| `fuel_correction_s_per_lap` | float | what was added back; 0 when disabled |
| `fit_intercept_s` | float | |
| `fit_r_squared` | float | |
| `samples_used` / `samples_seen` | int | they differ because contaminated laps are excluded |
| `slope_std_error_s_per_lap` | float | |
| `slope_ci_low_s_per_lap` / `slope_ci_high_s_per_lap` | float | 95% interval |
| `degradation_significance` | `"positive"` \| `"unclear"` \| `"negative"` | where the interval sits relative to zero |

**The one-lap comparison**

| field | type | |
|---|---|---|
| `projected_time_current_tyres_s` | float | next lap on the current set |
| `projected_time_fresh_tyres_s` | float | next lap on a fresh set, read at tyre age 1 |
| `pit_lane_cost_s` | float | fixed constant |
| `delta_s` | float | **time saved by pitting**, over the next lap |

Sign convention: `delta_s = projected_current − (projected_fresh + pit_cost)`,
so `delta_s > 0` means pitting wins the next lap (the `next_lap_only` verdict
still also requires significant degradation, below).

**What the call actually turns on**

| field | type | |
|---|---|---|
| `fresh_tyre_advantage_s_per_lap` | float | slope × *current* tyre age; constant on every future lap |
| `laps_to_break_even` | float \| null | `pit_cost / advantage`. null when the advantage is not positive |
| `laps_to_break_even_low` / `_high` | float \| null | the same at the ends of the slope interval. `high` is **null when unbounded** |
| `degradation_is_measurable` | bool | false when the slope is not positive |
| `note` | string \| null | human-readable caveat |

```json
{
  "type": "decision", "lap": 58, "driver_number": 1,
  "compound": "SOFT", "tyre_age": 19, "verdict": "stay_out",
  "current_compound_degradation_s_per_lap": 0.1139,
  "raw_degradation_s_per_lap": 0.057,
  "fuel_correction_s_per_lap": 0.0569,
  "fit_intercept_s": 88.943, "fit_r_squared": 0.9774,
  "samples_used": 17, "samples_seen": 18,
  "slope_std_error_s_per_lap": 0.00447,
  "slope_ci_low_s_per_lap": 0.1044, "slope_ci_high_s_per_lap": 0.1235,
  "degradation_significance": "positive",
  "projected_time_current_tyres_s": 91.222,
  "projected_time_fresh_tyres_s": 89.057,
  "pit_lane_cost_s": 22.0, "delta_s": -19.835,
  "fresh_tyre_advantage_s_per_lap": 2.1647,
  "laps_to_break_even": 10.16,
  "laps_to_break_even_low": 9.38, "laps_to_break_even_high": 11.09,
  "degradation_is_measurable": true, "note": null
}
```

**`verdict` compares over the laps that remain**, using `net_gain_s`:

```
net_gain_s = fresh_tyre_advantage_s_per_lap x laps_remaining - pit_lane_cost_s
verdict    = pit_now when net_gain_s > 0
                      and degradation_significance == "positive"
```

A positive `net_gain_s` on degradation whose 95% interval reaches zero is noise
that landed on the positive side, so it gives `stay_out` with a `note` saying
the signal is insufficient. Every `pit_now` in a replay of Baku 2025 without
this rule came from exactly that case.

`delta_s` is the same comparison over a *single* lap, kept for transparency. It
is essentially always negative — a 22-second stop cannot be repaid in one lap —
which is why a verdict built on it alone was structurally incapable of ever
saying `pit_now`.

**Check `verdict_basis`.** `next_lap_only` means the race distance was unknown
and the verdict fell back to that one-lap comparison, so it is *not actionable*.
Supply `?total_laps=` to fix it, or read `laps_to_break_even`.

**Check `degradation_significance` too.** `unclear` means the slope's interval
spans zero: the data cannot yet distinguish degradation from none, which is
different from the tyre not degrading.

**Check `degradation_significance` before trusting either.** `unclear` means
the slope's interval spans zero: the data cannot yet distinguish degradation
from none, which is different from the tyre not degrading.

### `no_live_session`

**A message type of its own, not an error.** Live mode spends most of the year
with nothing to connect to; rendering that as a failure would train the user to
ignore the component meant to flag real breakage.

| field | type | |
|---|---|---|
| `type` | `"no_live_session"` | |
| `detail` | string | why, in prose |
| `checked_at` | ISO-8601 | so a stale page cannot look current |
| `next_session_key` | int \| null | |
| `next_session_name` | string \| null | e.g. `"Practice 1 at Baku"` |
| `next_session_start` | ISO-8601 \| null | |

```json
{"type": "no_live_session",
 "detail": "No live session right now. The most recent session (Race at Madrid) is over; live data closed at 2026-09-13T15:30:00+00:00. Next up: Practice 1 at Baku at 2026-09-24T08:30:00+00:00.",
 "checked_at": "2026-09-14T19:38:24.969036Z",
 "next_session_key": 11370,
 "next_session_name": "Practice 1 at Baku",
 "next_session_start": "2026-09-24T08:30:00Z"}
```

### `end`

Sent when the stream finishes normally: a replay exhausted, or a live session's
window closed.

| field | type | |
|---|---|---|
| `type` | `"end"` | |
| `session_key` | string | |
| `total_ticks` | int | |
| `total_decisions` | int | reported separately, because ticks without decisions are normal |
| `reason` | string | `"completed"` |

```json
{"type": "end", "session_key": "sample", "total_ticks": 58,
 "total_decisions": 49, "reason": "completed"}
```

### `error`

The stream failed or was refused.

| field | type | |
|---|---|---|
| `type` | `"error"` | |
| `detail` | string | for humans |
| `code` | string \| null | for machines — see below |

```json
{"type": "error",
 "detail": "No stint data for driver 99 in session '9904'. Drivers present: [1, 4, 5, ...]",
 "code": "no_data"}
```

---

## Error codes

| code | meaning | fix |
|---|---|---|
| `unauthorized` | no valid API key | send `Authorization: Bearer` or the `f1key.` subprotocol |
| `origin_not_allowed` | browser origin not in `F1_ALLOWED_ORIGINS` | add it; the message names the configured list |
| `unknown_mode` | `mode` was not `replay` or `live` | |
| `sample_is_replay_only` | `session_key=sample` with `mode=live` | the fixture is not a live session |
| `no_data` | valid request, nothing to stream | check the session key; the message lists drivers present |
| `no_live_session` | *(carried on the dedicated message type)* | nothing is running; the message says what is next |
| `openf1_unavailable` | OpenF1 unreachable, timed out, or 5xx | upstream problem, not the request |
| `openf1_rate_limited` | OpenF1 returned 429 | slow down; live mode backs off automatically |
| `openf1_bad_response` | 4xx, non-JSON, or an unexpected shape | |
| `live_polling_failed` | repeated poll failures during a live session | |
| `tick_source_error` | generic source failure | |
| `openf1_error` | generic OpenF1 failure; the base of the three above | |
| `internal_error` | an unexpected exception | server logs carry the traceback |

---

## Configuration

All backend settings are read from the environment at startup with an `F1_`
prefix. Nothing is baked into the container image.

### Core

| variable | default | |
|---|---|---|
| `PORT` | `8000` | port to bind; Render sets this |
| `F1_ALLOWED_ORIGINS` | *(empty = all)* | comma-separated browser origins |
| `F1_API_KEYS` | *(empty = no auth)* | comma-separated keys; several allow rotation |
| `F1_OPENF1_BASE_URL` | `https://api.openf1.org/v1` | |
| `F1_OPENF1_TIMEOUT_SECONDS` | `10.0` | |

### Replay pacing

| variable | default | |
|---|---|---|
| `F1_REPLAY_TICK_INTERVAL_SECONDS` | `0.5` | a deliberate simplification, not real lap timing |
| `F1_MIN_TICK_INTERVAL_SECONDS` | `0.0` | bound on the per-connection override |
| `F1_MAX_TICK_INTERVAL_SECONDS` | `10.0` | |

### Live mode

| variable | default | |
|---|---|---|
| `F1_LIVE_WINDOW_MARGIN_MINUTES` | `30` | before and after a session. **Do not widen to ease testing** |
| `F1_LIVE_POLL_INTERVAL_SECONDS` | `10.0` | 2 requests per poll = 12 req/min |
| `F1_OPENF1_MIN_REQUEST_INTERVAL_SECONDS` | `0.4` | per-second guard: 2.5 req/s |
| `F1_LIVE_BACKOFF_INITIAL_SECONDS` | `5.0` | |
| `F1_LIVE_BACKOFF_MAX_SECONDS` | `60.0` | |
| `F1_LIVE_MAX_CONSECUTIVE_FAILURES` | `6` | before giving up |
| `F1_LIVE_IDLE_TIMEOUT_SECONDS` | `900.0` | generous: a red flag stops a race for a while |

### Decision engine

| variable | default | |
|---|---|---|
| `F1_PIT_LANE_COST_SECONDS` | `22.0` | circuit-dependent in reality |
| `F1_MIN_SAMPLES_FOR_FIT` | `3` | two points always fit a line perfectly |
| `F1_FRESH_TYRE_REFERENCE_AGE` | `1.0` | lap one on a new set is not its best |
| `F1_MAX_LAP_TIME_EXCESS_FOR_FIT_S` | `2.5` | outlier threshold; catches in-laps (see SPEC §14.3) |
| `F1_FIT_OUTLIER_WINDOW_LAPS` | `3` | neighbourhood a lap is judged against |
| `F1_EXCLUDE_PIT_OUT_LAPS_FROM_FIT` | `true` | |

### Fuel correction

| variable | default | |
|---|---|---|
| `F1_FUEL_CORRECTION_ENABLED` | `true` | |
| `F1_FUEL_EFFECT_S_PER_KG` | `0.03` | assumed, not measured per circuit |
| `F1_RACE_START_FUEL_KG` | `110.0` | regulation maximum |
| `F1_ASSUMED_RACE_LAPS` | `57` | fallback when the race length is unknown, i.e. live mode |

### Decision log

| variable | default | |
|---|---|---|
| `F1_DECISION_LOG_ENABLED` | `true` | |
| `F1_DECISION_LOG_PATH` | `decisions.db` locally, `/data/decisions.db` in the container | `/app` is root-owned and the container runs as a non-root user, so the log lives in `/data`. **Mount a volume there to survive a redeploy.** |
| `F1_DECISION_LOG_MAX_RUNS` | `200` | oldest pruned beyond this |

### Frontend

| variable | |
|---|---|
| `NEXT_PUBLIC_BACKEND_WS_URL` | accepts `https://`, `http://`, `wss://`, `ws://` or a bare host; the scheme is normalised, so `https://` becomes `wss://` |
| `NEXT_PUBLIC_API_KEY` | sent as a WebSocket subprotocol, never in the URL |
