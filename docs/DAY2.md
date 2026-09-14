# Day 2 — Decision engine: degradation curve + pit/stay math, computed incrementally

## Goal
By the end of today: the stream emits a `decision` message after every tick — a pit-now/
stay-out verdict with the full math behind it — computed using only ticks seen so far in the
current connection, working identically whether those ticks come from `ReplayTickSource`
(built) or, eventually, `LiveTickSource` (Day 4, not yet built).

## A gap Day 1 left open — deal with this first
The `TelemetryTick` built on Day 1 carries `lap`, `driver_number`, `compound`, `tyre_age`,
`stint_number` — but **no lap time**. You cannot fit a degradation curve without lap times.
Before writing any decision logic:
- [ ] Extend the OpenF1 client to also fetch `/v1/laps` (per-lap `lap_duration`) for a session
- [ ] Merge lap duration into each tick by matching `driver_number` + `lap` number
- [ ] Add `lap_duration_s: float` to the `TelemetryTick` model — this belongs in the shared
      tick contract, not replay-specific code, since `LiveTickSource` will need to supply it
      too once it exists
- [ ] Extend (or rebuild) the offline sample fixture to include realistic synthetic lap
      times that actually degrade with tyre age per compound — the Day 1 fixture only had
      stint metadata, no lap times, so it can't exercise the decision engine as-is

## Scope clarification: gap-to-car-ahead is dropped from this project
Earlier notes mentioned "gap to car ahead" as a factor in the decision. That's a multi-driver
concept (whether pitting costs track position depends on where other cars are) — and
multi-driver strategy interaction is already explicitly out of scope for this project (see
PROJECT.md). Don't build it. The decision this project makes is purely "is it faster to pit
now or not," not "will pitting lose a position" — say this explicitly in the README later so
it's a stated simplification, not a silently missing feature.

## Checklist
- [ ] `decision_engine.py`: maintains, per compound, the set of `(tyre_age, lap_duration)`
      pairs observed so far in the current connection
- [ ] Fit a simple degradation curve per compound (linear regression: lap time as a function
      of tyre age is enough — don't reach for anything fancier) once at least 3 data points
      exist for that compound; before that, there's nothing to fit yet
- [ ] Given the current tick: project next-lap time if staying out on the current compound/age
- [ ] Given the fitted curve for a fresh set of the *same* compound: project the lap time on
      a fresh set (evaluate the curve near tyre_age ≈ 1, not 0 — accounts for an out-lap
      effect, keep this simple, don't overthink it)
- [ ] Fixed pit-lane time cost constant (~20-25s) — put it in config, note that it's
      circuit-dependent and this is a simplification, not researched per-circuit
- [ ] Decision rule: compare (projected time staying out) vs (projected time on fresh tyres +
      pit cost) — verdict is whichever is faster. Simple one-lap lookahead is enough for this
      sprint; a multi-lap lookahead is a real v2 idea, not this scope
- [ ] Wire the decision engine into the WebSocket handler in `main.py`: after forwarding each
      `tick` message, also compute and send a `decision` message (skip it gracefully — no
      crash — if there isn't yet enough data for the current compound)
- [ ] `decision` message shape (draft from SPEC.md, adjust field names if the real math
      suggests better ones — this was written before the model existed):
```json
{
  "type": "decision", "lap": 19, "verdict": "stay_out",
  "current_compound_degradation_s_per_lap": 0.18,
  "projected_time_current_tyres_s": 92.4,
  "projected_time_fresh_tyres_s": 89.1,
  "pit_lane_cost_s": 22.0, "delta_s": -0.7
}
```
- [ ] Basic sanity test: run 3-4 hand-checkable scenarios through the decision function
      directly (not through the WebSocket) and confirm the verdict matches what you'd expect
      by doing the arithmetic yourself

## What "done" looks like today
Connecting to the stream (sample fixture, now with real lap-time data) and watching a
`decision` message follow every `tick`, with numbers that visibly respond to tyre age
increasing — degradation should make "stay out" flip to "pit now" as a stint gets old, and you
should be able to explain exactly why, using the numbers shown, not just trust the verdict.

## Explicitly not today
- No `LiveTickSource` (Day 4)
- No frontend (Day 3) — verify this by watching raw WebSocket messages, not a UI
- No gap-to-car-ahead / multi-driver logic (out of scope for the whole project)
- No multi-lap lookahead — one lap ahead is the MVP

## Notes
This is the first day where correctness actually matters in a way you can get subtly wrong —
a decision engine that runs without crashing but gives bad advice is worse than one that
visibly fails, since a bad number looks fine until you check it by hand. Do the hand-check
step above before considering this done, not after.