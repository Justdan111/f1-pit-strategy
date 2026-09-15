# Engineering notes

Detail that doesn't belong in the README: why things are built the way they
are, what's been verified, and what the known limits are.

## Contents
- [Three decisions worth explaining](#three-decisions-worth-explaining)
- [Architecture](#architecture)
- [Message protocol](#message-protocol)
- [Verification status](#verification-status)
- [Simplifications](#simplifications)
- [Why Docker](#why-docker)

---

## Three decisions worth explaining

### 1. `TickSource`: an interface built with one implementation

Day 1 had no live mode and no way to build one — the race weekend was three
weeks out. The obvious move was "fetch a historical race, replay it, add live
later".

Instead the streaming layer was written against an interface with a single
implementation behind it. That usually is over-engineering. Here it wasn't,
because live and replay differ in *shape*, not just data source: replay knows
the whole race up front and fakes the waiting; live doesn't know when the race
ends and does real waiting. Code written against replay's shape silently
assumes things only replay guarantees — that `total_laps` exists, that data is
already in memory, that iteration can't fail halfway.

**Cost:** an abstraction with one implementation for three days.
**Payoff:** adding live mode changed one function — `_build_source` gained a
single branch. The decision engine, protocol, and entire frontend were
untouched. A contract test runs both implementations over the same data and
asserts equality field by field, so they cannot drift.

### 2. The null-duration catch

The bug that would have quietly ruined live mode.

A lap appears in OpenF1's `/v1/laps` the moment a car **starts** it, with
`lap_duration: null` until it finishes. The poll loop must emit each lap once
and never re-send it.

Those two facts combine badly. Emit a lap as soon as it appears and you send a
tick whose lap time is permanently `null`, because under the no-duplicates
rule it will never be sent again. The degradation curve would lose a data
point on **every lap of the race**, and the fit would be built from whichever
laps happened to be slow enough to still be pending.

Nothing crashes. The dashboard shows a verdict. The verdict is garbage.

Fix: hold back the newest lap while it has no lap time; release it once a
duration arrives or a higher lap number proves it is over.

Replay never hits this — every lap already has its final duration. It was
found by reasoning about what the API does during a session, not by a test.

### 3. Reporting negative degradation instead of hiding it

Fitting lap time against tyre age on the real 2025 Azerbaijan GP gives a slope
of **−0.066 s/lap**. The tyre appears to get *faster* as it wears.

That isn't a bug. A car sheds ~100kg of fuel over a stint, worth about
−0.03 s/lap, which on a hard tyre at Baku cancels degradation outright. The
model measures (degradation − fuel effect), and this project doesn't correct
for fuel.

Two options: clamp the slope to zero and always show a plausible number, or
report it. The tool reports it — `degradation_is_measurable: false`, no
break-even figure, and a note explaining that fuel burn can mask tyre wear.

A tool that always produces a confident number is indistinguishable from one
that produces a correct number, right up until someone acts on it.

Getting there took two corrections, both caught by hand-checking rather than
by tests passing:

- An outlier filter keyed to the fastest lap of the stint discarded every
  genuinely degraded lap on a fast-wearing tyre, leaving one sample and no
  decision at all.
- A contaminated-neighbourhood case (Baku laps 1–4 were a standing start plus
  a safety car) let lap 1 survive as the minimum of its own bad window and
  drag the fitted slope to −0.243 s/lap.

Both rules now run: a lap is dropped if it is much slower than the best lap at
a *similar* tyre age, or much slower than the best lap on an *older* tyre.
They're complements — the first fails when a whole neighbourhood is
contaminated, the second can't catch contamination at the end of a stint.

---

## Architecture

```
        TickSource (interface)
        ├── ReplayTickSource   a finished race or the offline fixture, paced
        └── LiveTickSource     polls OpenF1 during a live session
                    │
                    ▼             both produce identical TickMessages
            decision_engine       one engine; it cannot tell them apart
                    │
                    ▼
        FastAPI   WS /ws/race/{session_key}?mode=replay|live
                    │
                    ▼
            Next.js dashboard     shows which mode is active, always
```

**The constraint that shapes everything:** every number is computed from data
seen so far, never from the full race in hindsight. The decision engine only
ever sees "the next tick". It cannot look ahead, because during a live race
there is nothing ahead to look at. That's also what makes replay honest —
replaying a finished race produces exactly what you'd have seen live.

### The maths

Fit lap time as a straight line in tyre age, per compound:

```
lap_time(age) = b + m * age
```

For a set of age `A`, comparing the next lap:

```
stay out:  b + m*(A + 1)
pit now:   b + m*1 + pit_cost
delta   :  m*A - pit_cost
```

Extending over N laps, every `b` and `m*N(N+1)/2` cancels, leaving
`m*N*A - pit_cost`. So the per-lap advantage of a fresh set is **`m * A`** —
slope times *current* age, constant on every future lap — and:

```
laps_to_break_even = pit_cost / (m * A)
```

Not `pit_cost / m`, which would overstate the pit window by a factor of the
tyre's age. The one-lap `verdict` reads `stay_out` almost always;
`laps_to_break_even` is the number to act on.

---

## Message protocol

`start` → (`tick` → `decision`)* → `end`, or `error`, or `no_live_session`.

`no_live_session` is a message type of its own rather than an error, because
live mode spends most of the year with nothing to connect to. Rendering that
as a failure would train you to ignore the component meant to tell you when
something is genuinely broken.

`TickMessage` deliberately carries no mode field — mode is stated once, in
`start`, so the decision engine is structurally unable to branch on it.

WebSocket handshakes are **not** subject to CORS: browsers send `Origin` but
don't preflight, and no CORS header can refuse one. Stream access is therefore
checked explicitly in the handler. Configuring only CORS would look correct
and protect nothing.

---

## Verification status

**Live mode has never run against a live session**, because there hasn't been
one since it was built. The first real test is Azerbaijan practice on
**24 September 2026**.

### Verified

| | How |
|---|---|
| Live-session detection, both 30-minute window edges | 24 tests against real OpenF1 session metadata with a mocked clock, at one-second resolution |
| Rate-limit compliance (~3 req/s, 30 req/min) | Virtual-clock tests; measured 12 req/min, 40% of the allowance |
| Backoff on 429 / transient failure, honest give-up | Tests with injected failures |
| Live and replay emit identical ticks | Contract test comparing both implementations field by field |
| Malformed / partial data degrades, never crashes | 14 tests: null durations, inverted ranges, absurd lap counts, non-JSON, 404, 429, 500, timeouts |
| Container matches local behaviour | Full message stream diffed: 109 messages identical |
| Replay end to end | 58-lap fixture and a real historical race, in a browser, through the container |
| Reconnect with backoff | Backend killed mid-stream: 1s→2s→4s, recovery, honest failure after 6 attempts |

### Not verified, and cannot be until the race weekend

- That a live session is detected as live **by the real clock**
- That polling receives **genuinely new laps as they happen**
- That OpenF1's live data has the shape its historical data has
- That the rate limit holds across a full session under real load

The polling logic runs against a fake client whose data grows between polls.
That proves the logic; it does not prove OpenF1 behaves that way live.

### The live test

During the Azerbaijan weekend (24–26 September 2026):

1. Wake the Render service (`GET /health`) a few minutes before each session
   window — free tier spins down after 15 minutes idle with a 30–60s cold
   start, and the first connection of a live session is the worst possible
   time to discover that.
2. Connect the deployed frontend in live mode during a session window.
3. Confirm live status is detected, new laps stream in, and the decision panel
   updates from genuinely live data.
4. If it fails, note exactly what broke — window detection, rate limiting,
   null-duration handling, something else. That's the real final task.

---

## Simplifications

Deliberate, not oversights:

- **Answers "is it faster to pit", never "will pitting cost me a position."**
  Track position is a multi-driver question and is out of scope.
- **Pit-lane cost is one constant (22s).** Circuit-dependent in reality; not
  researched per circuit.
- **Degradation is fitted as a straight line.** Real tyres have a cliff a line
  under-predicts.
- **No fuel-burn correction** — see decision 3.
- **Replay pacing is not real lap timing.** A fixed interval, for usability.
- **One driver at a time.**

---

## Why Docker

Reproducibility, and nothing more.

Render can deploy this from its own Python buildpack. The buildpack detects
`pyproject.toml`, resolves dependencies itself, and guesses a start command —
none of which this repo controls or can reproduce locally. When something
behaves differently in production, there's no local equivalent to compare
against.

The Dockerfile installs with `uv sync --frozen` against the committed
`uv.lock`, so the container gets the exact versions tested rather than whatever
resolves at build time. That claim was checked, not assumed: the full message
stream from the container was diffed against the same stream from a local
`uvicorn`, and all 109 messages were identical.

---

## Data source

[OpenF1](https://openf1.org) — free, no API key. Historical data from 2023;
live data from 30 minutes before a session starts to 30 minutes after it ends.

One quirk: a query matching nothing returns `404 {"detail": "No results
found."}` rather than `200 []`. That's an empty result, not a rejected
request, and the client normalises it.
