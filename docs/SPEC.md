# F1 Pit Strategy Simulator — Product Specification

**Status:** Day 1 of 5 complete — every Day 1 checklist item built and verified running,
replay mode only. Day 2 not started.
**Owner:** Dan
**Last updated:** 2026-09-14 — Day 1 built and verified end to end; OpenF1 response shapes
confirmed against the real API; see "Current status" (section 13) for exactly what runs today.

> **Note on the previous status line.** This file previously read "Day 1 of 5 complete" while
> the repository contained no application code at all — the status was aspirational, written
> ahead of the work. It is now accurate: everything claimed below in section 13 has been run
> and its output checked. Keep it that way — a spec that overstates progress is worse than no
> spec, because it stops you noticing what's missing.

---

## 0. Correction to earlier scope
Earlier planning stated OpenF1 only exposes historical/completed session data, and that a
genuinely live feed wasn't achievable without team-level access. That was checked properly and
found to be wrong: OpenF1 provides real live data while a session is running (updates roughly
every 3-4 seconds, live from 30 minutes before a session starts to 30 minutes after it ends),
plus an MQTT feed for real push-based updates. This spec is corrected accordingly: **live mode
is the primary aim of this project, not a stretch goal.**

## 1. What this is
A pit strategy decision tool for Formula 1 with two modes:
- **Live mode (primary aim):** connects to a genuinely live F1 session as it happens, and
  continuously answers: should the car pit for fresh tires now, or stay out?
- **Replay mode (secondary):** does the same thing against an already-finished historical race
  (or an offline sample fixture), paced to feel live — used for development, testing, and
  demoing on any day, not just during a race weekend.

Both modes share the same decision engine and the same message protocol. Only the data source
differs. The project is only genuinely "done" once live mode has been tested against a real,
currently-running session — replay mode working well is necessary but not sufficient.

## 2. The problem it solves
Tires degrade every lap. Pitting for a fresh set costs roughly 20-25 seconds. A strategist has
to weigh time lost per lap to degradation against time lost pitting. This tool makes that
trade-off explicit and numeric, recalculated continuously, using only data available at that
point — whether that point is "3 seconds ago in a real live race" or "this lap in a replayed
historical one."

## 3. Who it's for (in the fiction of the product)
A race strategist or performance engineer watching a session live — the actual live session,
not a recording — who wants a running, explainable second opinion on the pit call in real time.

## 4. Core design principle
**Everything is computed incrementally, against data seen so far, never against the full race
in hindsight.** This was always true for the decision engine's math; it's now also the reason
live and replay modes can share one decision engine unmodified — a decision engine that only
ever sees "the next tick" doesn't care whether that tick just happened in the real world 3
seconds ago or is being replayed from a race that finished last year.

## 5. Real-world test checkpoint
**Azerbaijan Grand Prix, Baku — Saturday, September 26, 2026** (moved from Sunday this year to
avoid a scheduling clash). This is the target date for live mode to be tested against a real,
currently-running session. Practice begins September 24. If live mode can't connect to and
correctly stream that real weekend, it isn't done, regardless of how well replay mode performs.

---

## 6. System architecture

```
                    ┌───────────────────────────┐
                    │   TickSource (interface)    │
                    └──────────────┬────────────────┘
                                   │
                 ┌─────────────────┴─────────────────┐
                 ▼                                     ▼
   ┌───────────────────────────┐        ┌───────────────────────────┐
   │   LiveTickSource (Day 4)     │        │   ReplayTickSource (Day 1)   │
   │   polls OpenF1 (or MQTT)     │        │   paces out historical or     │
   │   during a real live session │        │   sample data                 │
   └──────────────┬────────────────┘        └──────────────┬────────────────┘
                  │                                        │
                  └───────────────────┬────────────────────┘
                                       ▼
                          ┌───────────────────────┐
                          │  decision_engine.py       │
                          │  (Day 2) mode-agnostic     │
                          └───────────┬─────────────┘
                                      ▼
                          ┌───────────────────────┐
                          │   FastAPI WebSocket        │
                          │   /ws/race/{...}             │
                          └───────────┬─────────────┘
                                      ▼
                          ┌───────────────────────┐
                          │   Next.js dashboard         │
                          │   (Day 3) shows active mode  │
                          └───────────────────────┘
```

---

## 7. Components

### 7.1 `openf1_client.py` — data source integration (partially built, Day 1)
Handles both historical and live REST calls to OpenF1. Live calls must respect the free-tier
rate limit (~3 req/s, 30 req/min). Fails loudly and specifically on unreachable API, bad
session_key, or unexpected response shape.

**Built (Day 1):** `get_stints(session_key, driver_number=None)`, returning validated `Stint`
models. Typed failures: `OpenF1Unavailable` (unreachable / timeout / 5xx) and
`OpenF1BadResponse` (4xx / non-JSON / unexpected shape). Individual unparseable rows are
dropped and logged rather than failing the whole session — a car that retired mid-stint must
not make the other nineteen unusable. The client does not own its `httpx.AsyncClient`; one
pooled client is created in the FastAPI lifespan and injected.

**Not built:** the rate limiter (Day 4, with `LiveTickSource`). Replay makes one request per
connection, comfortably inside the free tier; live polling will not be.

**Verified against the real API on 2026-09-14** (this was the outstanding Day 1 item):
- Field names on `/v1/stints` confirmed exactly as modelled: `driver_number`, `stint_number`,
  `lap_start`, `lap_end`, `compound`, `tyre_age_at_start`. Day 2's maths can trust these.
- **API quirk found:** OpenF1 answers a query that matched nothing with
  `404 {"detail": "No results found."}`, not `200 []`. That is an empty result, not a rejected
  request, and the client now normalises it to an empty list instead of reporting a bad
  request. Day 4 will hit this constantly — "no live session right now" is the same shape.

### 7.2 `TickSource` — shared interface (built, Day 1)
The contract both data sources implement: "give me the next tick." Neither the decision engine
nor the WebSocket handler should need to know which implementation is behind it.

**As built,** an ABC with three methods:
- `async open() -> StartMessage` — do the fallible setup (replay: fetch and flatten a race;
  live: check a session is actually running) and describe the stream. Kept separate from
  `ticks()` so "couldn't start" stays distinguishable from "started, then died", and so
  `total_laps` can be reported before the first tick.
- `ticks() -> AsyncIterator[TickMessage]` — an async generator. Async because producing the
  next tick means waiting (a timer in replay, an HTTP poll in live) without blocking the event
  loop; an iterator because the consumer *pulls*, which gives backpressure for free.
- `async close()` — non-abstract, defaults to a no-op.

Failures raise `TickSourceError` (or `NoDataError`), each carrying a machine-readable `code`,
so the WebSocket layer can render them as `error` envelopes rather than 500s.

### 7.3 `ReplayTickSource` (built and verified, Day 1)
Flattens historical (or sample) stint data into a paced sequence of per-lap ticks. Pacing is a
documented, deliberate simplification for usability — not a claim about real lap timing.

Takes its stints from an injected async `loader`, so it never imports the OpenF1 client or the
fixture directly. Two factories: `from_sample()` (source `"sample"`) and `from_openf1()`
(source `"historical_replay"`). With no `driver_number` given it replays the car that completed
the most laps, tie-broken by lowest number — deterministic, and biased towards a finisher
rather than a car that retired on lap 3.

**The one calculation Day 2 depends on:** tyre age is
`tyre_age_at_start + (lap - lap_start)`, *not* `lap - lap_start`. Real data has stints starting
on used sets (Baku 2025, car #1, stint 2 starts at age 4), so the naive version is wrong on
real data while passing every test against a naive fixture. The sample fixture deliberately
includes a used set (stint 3, age 2) so this stays covered offline.

### 7.4 `LiveTickSource` (planned, Day 4)
Polls OpenF1 on an interval (or subscribes to the MQTT feed) while a real session is live.
**Responsibilities:**
- Determine whether a session is currently live (check against `/v1/sessions`, including the
  `session_key=latest` shortcut, and the session's time window)
- If no session is currently live, fail clearly and helpfully — e.g. "no live session right
  now, next one is [X]" — not a generic error
- Respect rate limits explicitly (don't poll faster than the free tier allows)
- Emit ticks in the same shape `ReplayTickSource` does, so downstream components are identical

### 7.5 `decision_engine.py` (planned, Day 2)
Fits a tire degradation curve incrementally and computes pit/stay, using only ticks seen so
far — regardless of whether those ticks came from `LiveTickSource` or `ReplayTickSource`.
Output includes every number behind the verdict, not just the verdict.

### 7.6 FastAPI WebSocket layer (built, Day 1)
`WS /ws/race/{session_key}?mode=live` or `?mode=replay` — mode selects which `TickSource`
implementation backs the connection. `session_key=sample` only valid under `mode=replay`.

**As built:** `mode` defaults to `replay`; `mode=live` returns an `error` envelope with code
`live_mode_not_implemented` until Day 4. Optional `?driver_number=` and `?tick_interval=`
(server-clamped) query params. The handler accepts the socket *before* validating anything, so
a bad request gets a readable `error` envelope rather than an opaque handshake failure.

The handler references only `TickSource` — never stints, replay, or OpenF1. Adding live mode on
Day 4 is one extra branch in `_build_source`; nothing downstream of it changes. That was the
entire point of building the interface on Day 1.

Also exposed: `GET /health` (no I/O — it must not report us unhealthy when OpenF1 is down) and
`GET /race/sample/stints`, a **debug-only** route for comparing raw stint input against emitted
ticks. The WebSocket is the product surface; that REST route is not.

### 7.7 Next.js dashboard (planned, Day 3)
Must show which mode is active (LIVE vs REPLAY) prominently and unambiguously — a user should
never be uncertain whether they're watching a real live race or a replayed one.

---

## 8. Message protocol

**`start`**
```json
{ "type": "start", "session_key": "9987", "source": "live", "total_laps": null }
```
`source` is one of `"sample"`, `"historical_replay"`, or `"live"`. `total_laps` is `null` for
live mode (the total isn't known in advance — the race hasn't finished yet), and a real number
for replay mode.

**`tick`** — unchanged shape regardless of mode
```json
{ "type": "tick", "lap": 19, "driver_number": 1, "compound": "HARD", "tyre_age": 0, "stint_number": 2 }
```

**`decision`** (Day 2, draft)
```json
{
  "type": "decision", "lap": 19, "verdict": "stay_out",
  "current_compound_degradation_s_per_lap": 0.18,
  "projected_time_current_tyres_s": 92.4,
  "projected_time_fresh_tyres_s": 89.1,
  "pit_lane_cost_s": 22.0, "delta_s": -0.7
}
```

**`end`** — sent when the replay finishes, or when a live session ends (per its live window)

**`error`** — includes a new expected case for live mode: no session currently live
```json
{ "type": "error", "detail": "No live session right now. Next session starts [time]." }
```

---

## 9. Explicit scope

**In scope:**
- Live mode: connecting to and correctly streaming a genuinely live F1 session
- Replay mode: historical races and the offline sample fixture, for dev/testing/demo
- Tire degradation modeling and pit/stay decision, computed incrementally, mode-agnostic
- Full WebSocket streaming architecture including reconnects
- A dashboard showing the live stream, the decision, and which mode is active

**Explicitly out of scope:**
- Multiple simultaneous races
- Multi-driver strategy interactions
- Weather modeling, safety car probability modeling
- Real-time-accurate pacing in replay mode specifically (live mode is naturally real-paced)

## 10. Non-functional requirements
- **Rate-limit compliance is mandatory for live mode** — exceeding OpenF1's free-tier limits
  isn't just bad practice, it will get requests rejected mid-session, which is the worst
  possible failure mode for the primary use case.
- **Graceful handling of "no live session right now"** — live mode will spend most of its
  life with no session to connect to; this must be an expected, clearly-communicated state,
  not an error.
- Explainability over accuracy in the decision engine, same as before.
- Fail loud, not silent, same as before.
- Deployed WebSocket support must be explicitly verified against the host.

## 11. Tech stack
Unchanged: Python/FastAPI backend, Next.js frontend, OpenF1 as the data source (both its
historical and live surfaces now), no paid services required for either mode at the free tier.

## 12. Success criteria
- ~~Replay mode: a real historical race streams correctly end-to-end~~ **met, 2026-09-14** —
  verified for both the sample fixture (58 ticks) and a real historical session_key
  (9904, Baku 2025, car #1: 51 ticks, HARD→MEDIUM at lap 41)
- **Live mode: connects to and correctly streams a genuinely live session — tested against the
  real Azerbaijan GP on September 26, 2026.** This is the primary bar, not a bonus.
- The decision engine produces a defensible, explainable pit/stay call under both modes
- The dashboard clearly shows which mode is active at all times
- The whole thing survives a dropped connection without breaking
- Deployed with WebSocket support confirmed working through the actual host

## 13. Current status
*Accurate as of 2026-09-14. Everything under "Built and verified" was run and its output
checked; nothing here is claimed on the strength of the code merely existing.*

### Built and verified (Day 1)
| Component | State |
|---|---|
| Pydantic models — `Stint` + `start` / `tick` / `end` / `error` envelopes | built |
| `TickSource` — explicit ABC (`open` / `ticks` / `close`) | built |
| `ReplayTickSource` — sample and historical, one implementation | built |
| `openf1_client.py` — `get_stints`, typed failures | built, verified against the real API |
| `GET /health`, `GET /race/sample/stints` (debug only) | built |
| `WS /ws/race/{session_key}` wired to `ReplayTickSource` | built |

Verified by running it:
- **Sample fixture:** `start` → **58 ticks** → `end`, laps contiguous 1–58, tyre age resetting
  correctly at both stops (lap 19 → age 0, lap 41 → age 2 on a used set).
- **Real historical race:** `session_key=9904` (Baku 2025), car #1 → 51 ticks, HARD→MEDIUM at
  lap 41, `source: "historical_replay"`. This closes the "real historical session_key still to
  be confirmed" item.
- **Pacing is genuinely temporal**, not a list dumped down a socket — tick arrival times are
  spaced by the configured interval.
- **Concurrency:** three simultaneous streams finished in 4.34s wall-clock against a 4.33s
  longest single stream, i.e. served in parallel, not serialised. The `await asyncio.sleep`
  pacing does not block the event loop.
- **Error envelopes** delivered in-protocol for: `mode=live` (not built), unknown session_key,
  and a driver absent from the session (which lists the drivers who *are* present).
- **Mid-stream disconnect** logged as an ordinary event and the socket closed cleanly — a
  client closing a tab is not an error.

### The `source` field
`"sample"`, `"historical_replay"` and `"live"` are all expressible today, and `"live"` is
reserved exclusively for a genuinely live session. A replayed historical race reports
`"historical_replay"`, never `"live"`, however live it looks in the UI. `TickMessage`
deliberately carries no mode field — mode is stated once, in `start`, so the Day 2 decision
engine is structurally unable to branch on it.

### Not built
- `LiveTickSource` (Day 4) — the seam for it exists: one branch in `_build_source`, with
  nothing downstream of it changing.
- OpenF1 rate limiter (Day 4, needed only once live polling starts).
- `decision_engine.py` and the `decision` message (Day 2).
- Next.js dashboard (Day 3).
- Deployment and WebSocket-through-host verification (Day 5).
- Automated tests. Day 1's verification was by running the stack and reading the output, which
  is evidence but not a regression guard. Day 4's hardening should turn the checks listed
  above into actual tests.

**Next up: Day 2** — the decision engine, written against `TickSource` only.