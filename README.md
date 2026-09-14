# F1 Pit Strategy

Answers one question, continuously, while a race is happening: **pit now, or stay out?**

Tyres degrade — lap times get slower every lap on a given set — but pitting costs
about 22 seconds. This tool makes that trade-off explicit and numeric, recalculated
every lap, using only data available at that point in the race.

Two modes, one decision engine and one message protocol:

- **live** — connects to a genuinely live F1 session via OpenF1 while it is running
- **replay** — paces out a finished race, or an offline fixture, so the thing is
  testable and demoable on any day rather than only during a race weekend

---

## What has actually been verified, and what has not

**Live mode has never been run against a live session.** There has not been one since
it was built. The first real test is Azerbaijan practice on **24 September 2026**.

This section exists so the claim "live mode works" is never made on the strength of
tests that cannot, by their nature, prove it.

### Verified

| | How |
|---|---|
| Live-session detection, including both 30-minute window edges | 24 unit tests against **real** OpenF1 session metadata with a mocked clock, asserted at one-second resolution |
| Rate-limit compliance (~3 req/s, 30 req/min) | Virtual-clock tests asserting the poll loop never exceeds its interval; measured 12 req/min, 40% of the allowance |
| Backoff on 429 / transient failure, and giving up after repeated failure | Unit tests with injected failures |
| Live and replay produce **identical** tick shapes | Contract test comparing both implementations field by field on the same data |
| The decision engine reaches identical conclusions from either source | Same contract test, comparing full decision messages |
| Malformed / partial OpenF1 data degrades instead of crashing | 14 tests: null durations, inverted lap ranges, absurd lap counts, non-JSON, 404, 429, 500, timeouts |
| "No live session right now" against the real API | Run against live OpenF1; correctly reports the next session |
| Replay mode end to end | 58-lap fixture and a real historical race, in the browser |
| Frontend reconnect with backoff | Backend killed mid-stream; observed 1s→2s→4s, recovery, and honest failure after 6 attempts |

### Not verified, and cannot be until 24 September 2026

- That a genuinely live session is detected as live **by the real clock**
- That polling actually receives **new laps as they happen** during a session
- That OpenF1's live data has the shape its historical data has
- That the free-tier rate limit holds up across a full session under real load

The polling logic is exercised against a fake client whose data grows between polls.
That proves the *logic* — laps are emitted once, in order, held back while still
running — but not that OpenF1 behaves that way live.

**Today's bar is "correctly built and unit-verified, ready to point at a real
session", not "proven against live data".**

---

## Running it

Backend:

```bash
cd backend
uv sync
uv run uvicorn backend.main:app --reload     # http://127.0.0.1:8000
uv run pytest                                # 71 tests
```

Frontend:

```bash
cd frontend
pnpm install
pnpm dev                                     # http://localhost:3000
```

Open `localhost:3000`:

- **replay** + `sample` — the offline fixture, 58 laps, no network needed
- **replay** + `9904` — Baku 2025, replayed from OpenF1
- **live** + `latest` — whatever is running now; outside a race weekend this correctly
  reports no live session and tells you when the next one is

Without a browser, `backend/scripts/ws_client.py` prints the raw stream.

---

## How it fits together

```
        TickSource (interface)
        ├── ReplayTickSource   finished race or fixture, paced
        └── LiveTickSource     polls OpenF1 during a live session
                    │
                    ▼
            decision_engine     same code for both, cannot tell them apart
                    │
                    ▼
         FastAPI  WS /ws/race/{session_key}?mode=replay|live
                    │
                    ▼
            Next.js dashboard
```

The interface was built on Day 1 with only one implementation behind it. Adding live
mode on Day 4 changed exactly one function (`_build_source`, one branch) and nothing
downstream. Both implementations share one tick builder, so their output cannot drift.

---

## Stated simplifications

These are deliberate, not omissions:

- **This answers "is it faster to pit", never "will pitting cost me a position."**
  Track position is a multi-driver question and is out of scope for the whole project.
- **Pit-lane cost is one constant (22s).** It is circuit-dependent in reality; we have
  not researched it per circuit.
- **Degradation is fitted as a straight line.** Real tyres have a cliff that a line
  under-predicts.
- **No fuel-burn correction.** Lap times fall through a stint as the car lightens
  (~-0.03 s/lap), so fitting lap time against tyre age measures degradation *minus*
  fuel effect. On real Baku data this is enough to make a hard tyre's measured
  degradation come out **negative**. The engine reports that honestly rather than
  hiding it — but it means real historical races often yield no actionable advice.
- **Replay pacing is not real lap timing.** A fixed interval, for usability.
- **One driver at a time.**

---

## Data source

[OpenF1](https://openf1.org) — free, no API key. Historical data from 2023; live data
from 30 minutes before a session starts to 30 minutes after it ends.

One quirk worth knowing: a query that matches nothing returns
`404 {"detail": "No results found."}` rather than `200 []`. That is an empty result,
not a rejected request, and the client normalises it.
