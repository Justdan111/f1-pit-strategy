# Day 1 — Streaming skeleton, built around a swappable data source

## Goal
By the end of today: a WebSocket endpoint that streams a race lap by lap, as if it were live
— start message, then one tick per lap arriving over time, then an end message. Built against
a `TickSource` interface, not hardcoded to replay — so that when live polling is added on Day
4, it slots in without rewriting anything built today. No decision logic yet — that's Day 2.

## Why this order, and why the interface matters today
The primary aim of this whole project is working against a **genuinely live** F1 session —
that's the real test, targeted for the September 26 Azerbaijan GP. Historical replay is the
secondary mode, used for development and demoing on days without a race. If Day 1 is built
as "fetch historical data, replay it" with no abstraction, adding live polling later means
reworking the streaming layer. Building a `TickSource` interface today — even with only one
implementation (`ReplayTickSource`) behind it — means Day 4's `LiveTickSource` is a new
implementation of the same interface, not a rewrite.

## Checklist
- [x] FastAPI skeleton running, `/health` responding
- [x] Pydantic models for stint data + streaming message envelopes (start/tick/end/error)
- [ ] **Define the `TickSource` interface explicitly** — something like "give me the next
      tick" as an async generator contract, so any implementation (replay or live) can be
      swapped in behind the same WebSocket handler
- [x] `ReplayTickSource` — replay engine that turns stint data into a paced sequence of
      per-lap ticks (this was previously just called the "replay engine" — same code,
      now framed explicitly as one implementation of `TickSource`, not the only one)
- [x] `GET /race/sample/stints` — debug-only REST route to inspect raw stint data
- [x] `WS /ws/race/sample` — full stream tested end to end: start message → 58 tick messages
      in correct lap order with correct compound/tyre-age tracking across stint changes → end
      message
- [ ] Confirm the `start` message's `source` field can express all three eventual states
      clearly: `"sample"`, `"historical_replay"`, and (Day 4) `"live"` — don't let `"live"` end
      up meaning two different things later
- [ ] **OpenF1 client verified against a live historical call** — do this next, on a machine
      with open network access
- [ ] Real historical race picked and `session_key` confirmed working through
      `WS /ws/race/{session_key}` in replay mode

## What "done" looks like today
Connecting to `WS /ws/race/sample` (or a real historical `session_key`) streams correct,
paced, lap-by-lap ticks — **and** the code is structured so a `LiveTickSource` can be added on
Day 4 without touching the WebSocket handler or the message models built today.

## Explicitly not today
- No live polling or MQTT connection (Day 4)
- No degradation curve fitting or pit/stay decision (Day 2)
- No frontend (Day 3)
- No deployment (Day 5)

## Notes
The OpenF1 client and its response-shape assumptions are still unverified against a real live
call (this sandbox can't reach `api.openf1.org`). This matters more now, not less — Day 4's
live polling depends on the same client, so get the shape right against historical data first,
where mistakes are cheap to catch.