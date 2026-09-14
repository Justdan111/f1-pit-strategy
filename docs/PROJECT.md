# F1 Pit Strategy Simulator

## The real problem
During a race, a strategist has to answer one question, over and over, **as the race is
happening**: **pit now, or stay out?** Tires degrade — lap times get slower every lap on a
given set — but pitting costs ~20-25 seconds. The right call depends on: how degraded the
current tires are, how much faster a fresh set would be, and whether the time lost pitting is
worth the time gained.

## Scope — two modes, one primary aim

**Primary aim: this must work against a genuinely live F1 session while it's happening.**
That's the actual test of whether this is real-time software or just a replay with a live
label on it. Every architecture decision below is made to support this.

**Secondary: it must also work against historical (already-finished) races.** This isn't a
downgrade — it's what makes the project usable for development and demoing on any day, not
just during a race weekend. It reuses the exact same decision engine and the exact same
message protocol as live mode; only the data source differs.

Two modes, one shared pipeline:
- **Live mode** — connects to OpenF1 while a real session is in progress, polling (or
  subscribing via MQTT) for genuinely new data as it happens
- **Replay mode** — takes an already-finished race (or the offline sample fixture) and paces
  it out to feel live, for development, testing, and demoing outside a race weekend

**Correction from earlier in this project's planning:** I'd previously written that OpenF1
only exposes historical data and that a genuinely live feed wasn't possible without team-level
access. That was wrong — I checked properly and OpenF1 does provide real-time data during a
session (updates roughly every 3-4 seconds, live from 30 minutes before a session starts to
30 minutes after it ends), plus an MQTT feed for real push-based updates instead of polling.
Live mode is a legitimate, buildable part of this project, not a stretch fantasy.

## Real-world test checkpoint
**Azerbaijan Grand Prix, Baku — Saturday, September 26, 2026** (moved from its usual Sunday
slot this year). This is the target date to have live mode working against a real session —
not a soft deadline, an actual test: if it can't connect to and correctly stream that real
race weekend, live mode isn't done yet, regardless of how it performs against replay data.

## What's explicitly OUT of scope (do not exceed)
- Multiple simultaneous races
- Multi-driver strategy interactions (undercut/overcut, traffic)
- Weather modeling
- Safety car probability modeling
- Real-time-accurate lap pacing in **replay mode specifically** (live mode is naturally
  paced by real events; replay mode's pacing is a documented, deliberate simplification for
  usability — see telemetry_stream notes)

If any of these start creeping in, stop — that's a v2, not this sprint.

## Architecture
```
                    ┌───────────────────────────┐
                    │   TickSource (interface)    │
                    │   "give me the next tick"    │
                    └──────────────┬────────────────┘
                                   │
                 ┌─────────────────┴─────────────────┐
                 ▼                                     ▼
   ┌───────────────────────────┐        ┌───────────────────────────┐
   │   LiveTickSource            │        │   ReplayTickSource          │
   │   polls OpenF1 (or MQTT)    │        │   paces out historical/      │
   │   while a session is live   │        │   sample data                │
   │   (Day 4)                    │        │   (Day 1 — done)             │
   └──────────────┬────────────────┘        └──────────────┬────────────────┘
                  │                                        │
                  └───────────────────┬────────────────────┘
                                       ▼
                          ┌───────────────────────┐
                          │  decision_engine.py      │
                          │  (Day 2) same logic,      │
                          │  regardless of source     │
                          └───────────┬─────────────┘
                                      ▼
                          ┌───────────────────────┐
                          │   FastAPI WebSocket       │
                          │   /ws/race/{...}            │
                          └───────────┬─────────────┘
                                      ▼
                          ┌───────────────────────┐
                          │   Next.js dashboard        │
                          │   (Day 3) shows LIVE vs      │
                          │   REPLAY mode explicitly     │
                          └───────────────────────┘
```

**Why a shared `TickSource` interface matters:** the decision engine must never know or care
whether a tick came from a live poll or a historical replay — it only ever sees "the next
tick." Designing this seam on Day 1 (even though `LiveTickSource` isn't built until Day 4)
means live mode slots in later without rewriting the decision engine or the WebSocket layer.
Get this abstraction right early; it's the difference between "add a mode" and "rebuild it."

## Data source: OpenF1
[OpenF1 API](https://openf1.org) — free, no API key required.

- **Historical data** (2023 onwards): free, always accessible, no auth.
- **Live data**: available while a session is running — from 30 minutes before it starts to
  30 minutes after it ends. Updates roughly every 3-4 seconds via REST polling.
- **Rate limits**: free tier is roughly 3 requests/second, 30 requests/minute — live polling
  must respect this explicitly, not assume unlimited calls.
- **MQTT feed** (`mqtt.openf1.org`): genuine push-based real-time updates instead of polling —
  worth using for live mode instead of polling if the added complexity is worth it; polling is
  the simpler fallback and fine for Day 4's first pass.
- Key endpoints: `/v1/sessions` (find a session_key, including `session_key=latest` for the
  most recent/current one), `/v1/stints`, `/v1/laps`, `/v1/intervals`.

**Outstanding verification:** the exact response shapes above were confirmed conceptually via
documentation, not against a live call from this build environment (network here is
allowlisted to package registries only). Verify field names against a real call before
trusting Day 2's math to real data — this applies to both historical and live endpoints.

## Day-by-day (this project's slice of the 5-day sprint)
- **Day 1 (done):** WebSocket streaming skeleton, built around the `TickSource` interface —
  `ReplayTickSource` implemented and tested end-to-end (58-lap sample stream, correct
  compound/tyre-age tracking). `LiveTickSource` not yet built, but the seam for it exists.
- **Day 2:** decision engine — degradation curve fit + pit/stay math, computed incrementally
  per tick. Must be written against the `TickSource` interface, not against replay-specific
  assumptions, since it needs to work unmodified once live mode lands on Day 4.
- **Day 3:** Next.js dashboard, connected to the WebSocket, showing live ticks + the decision
  — and showing explicitly which mode is active (LIVE vs REPLAY), never ambiguous to the user
  which one they're watching.
- **Day 4:** build `LiveTickSource` — polling OpenF1 (or MQTT) during a real or upcoming
  session, respecting rate limits, handling "no live session right now" gracefully instead of
  erroring unhelpfully. Plus the originally planned hardening: dropped connections, reconnects,
  malformed data, backpressure, tests.
- **Day 5:** deploy (confirm WebSocket support survives the host) + README + demo — **and the
  real test**: connect live mode to the actual Azerbaijan GP session window on September 26
  and confirm it streams correctly against a real, currently-running race.