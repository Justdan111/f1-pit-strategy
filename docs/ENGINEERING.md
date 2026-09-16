# Engineering notes

Why the numbers are what they are, what has been measured, and what the known
limits are. For how the system is put together see
[ARCHITECTURE.md](ARCHITECTURE.md); for the API surface see [API.md](API.md).

## Contents

- [Three decisions worth explaining](#three-decisions-worth-explaining)
- [Architecture and protocol](#architecture-and-protocol) (moved out)
- [Verification status](#verification-status)
- [Backtest validation](#backtest-validation)
- [Confidence bounds on the fit](#confidence-bounds-on-the-fit)
- [Fuel-burn correction](#fuel-burn-correction)
- [API key on the WebSocket](#api-key-on-the-websocket)
- [Persisted decision log](#persisted-decision-log)
- [Simplifications](#simplifications)
- [Why Docker](#why-docker)
- [Data source](#data-source)
- [What's next](#whats-next)

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

## Architecture and protocol

Moved out of this file as it grew:

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — components, the `TickSource` seam,
  connection lifecycle, concurrency and failure models, deployment topology,
  module map, testing strategy.
- **[API.md](API.md)** — every endpoint, every message field, error codes and
  the full configuration reference.

This file is the record of *why* the numbers are what they are, and what has
been measured.


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

## Backtest validation

`uv run python scripts/backtest.py` replays finished races through the real
decision engine and scores it. Three measurements, which prove very different
things.

Everything is computed incrementally: each prediction uses only laps before
the one being predicted. A test deliberately alters the end of a race and
asserts earlier predictions do not move — verified to fail when a future-data
leak is introduced, so it is not a vacuous check.

### 1. Prediction accuracy — the only non-circular measurement

How close the fitted curve's next-lap estimate is to the lap time that
actually happened, against two baselines a model must beat to earn its place.

Over 8 races from 2025, 312 clean predictions:

| predictor | MAE | RMSE | median AE |
|---|---|---|---|
| model | 0.888s | 3.849s | 0.328s |
| persistence (next lap = this lap) | 1.044s | 5.183s | 0.179s |
| compound mean | 6.121s | 10.338s | 1.020s |

**The mean and the median disagree, and both are reported.** The model beats
persistence by 0.156s on MAE but loses by 0.148s on median error. Read
together: the model is *worse on a typical lap* and *better on the awkward
ones*, because a fitted trend degrades more gracefully than "assume nothing
changes" when something does. Quoting only the MAE would be picking the
flattering number.

Unfiltered — including out-laps, in-laps and safety-car laps — the model loses
to persistence by 0.922s MAE. That is honest and expected: those laps are slow
for reasons the degradation model does not claim to explain, and a trend
extrapolates confidently into them while persistence does not.

### 2. Counterfactual pit lap — circular

Total race time under alternative pit laps, scored by the fitted curves.
**Uses the same model being evaluated**, so it can show internal consistency
and nothing else.

In practice it mostly fails to say anything: 3 of 4 one-stop races produced a
degenerate optimum at the edge of the swept range. That happens whenever the
second compound simply fits faster — with no notion of minimum stint length,
the sweep collapses to "pit immediately". Those are flagged and excluded.
Useful mainly as a demonstration of why the circular measure cannot be
trusted.

### 3. Gap to the actual pit lap — context, not ground truth

Distance between the engine's recommendation and what the team did. Teams also
optimise track position, which this project deliberately does not model, so a
gap is not necessarily an error on either side.

- All races: **10.4 laps** mean absolute gap
- Excluding races with 3+ stops: **4.0 laps**

Three or more stops in a dry race is vanishingly rare; it almost always means
rain or a red flag. Melbourne 2025 (five stops, wet) produced a 39-lap gap on
its own. Weather is out of scope, so those comparisons are meaningless in
either direction and are kept out of the headline.

### The caveat that dominates everything

**Degradation was measurable — a fitted slope above zero — in only 37% of
decisions.** Fitting lap time against tyre age measures degradation *minus*
fuel burn (~−0.03 s/lap). Where the slope comes out flat or negative the
engine reports no break-even point rather than inventing one, so it declines
to recommend a stop at all: it did so in 2 of 8 races.

That is the honest headline. The model is not confidently outperforming race
strategists; it is a defensible one-step predictor that beats a naive baseline
on average, declines to answer more often than not, and would need fuel-burn
correction before its pit recommendations could be taken seriously. Fixing
that is the top item under [what's next](#whats-next).

### Two things the backtest itself exposed

- **The rate limiter matters for batch work too.** Eight races is 16 rapid
  requests; OpenF1 returned a real 429 and a race vanished from the report
  without changing any visible total. The batch now paces at 2.5s between
  requests and retries with backoff rather than dropping a race silently.
- **Malformed-data handling earns its keep on real data.** Melbourne 2025
  returned INTERMEDIATE stints with null lap ranges — wet tyres fitted but
  never run. They are dropped and logged rather than crashing the run.

---

## Confidence bounds on the fit

The engine reports a 95% confidence interval on the fitted degradation slope,
not just a point estimate.

This exists because the backtest exposed a gap: 63% of decisions had a slope
that was not positive, reported as a single binary flag. That flag cannot tell
apart two very different situations — a tyre that genuinely is not slowing, and
three noisy laps that cannot support any conclusion. Acting on the first is
reasonable; acting on the second is guessing.

### How it is computed

Standard error of the slope in a simple linear regression:

```
s²      = SS_residual / (n - 2)
SE(m)   = sqrt( s² / Σ(x - x̄)² )
interval = m ± t(0.025, n-2) · SE(m)
```

Student's t, not the normal. At the engine's 3-sample minimum there is one
degree of freedom, where t is **12.706** against the normal's 1.96 — using the
normal there would understate the interval sevenfold and make three noisy laps
look like a measurement. The critical values are a 30-row table in
`statistics_helpers.py` rather than a scipy dependency: a large addition for
one function, and thirty numbers anyone can check against a textbook are more
auditable than an opaque call.

### What it changes

Three reported states instead of one flag:

| state | meaning |
|---|---|
| `positive` | interval entirely above zero — degradation is confidently real |
| `unclear` | interval spans zero — too few or too noisy samples to tell |
| `negative` | interval entirely below zero — confidently getting faster |

`laps_to_break_even` gains a range, derived from the ends of the slope
interval. A shallower slope means a longer wait, so the low end of the slope
gives the high end of the payback — and when the interval reaches zero the
payback is **unbounded**, because a tyre that might not be slowing might never
repay a stop.

The verdict itself is unchanged. Confidence bounds inform the reader; they do
not silently move the decision, and a test asserts that.

### What it showed

Re-running the backtest, the 63% splits:

| | share of decisions |
|---|---|
| positive — degradation confidently real | 14.4% |
| unclear — cannot tell | 42.3% |
| negative — confidently getting faster | 43.4% |

That is a more useful picture than "37% measurable". Only **14%** of decisions
rest on a slope the data confidently supports. And the 43% negative is not
noise — the interval sits entirely below zero, so fuel burn outweighing tyre
wear is a measured effect rather than a suspicion. It is the strongest
argument yet for fuel-burn correction being the next piece of work.

---

## Fuel-burn correction

A car sheds fuel through a race and gets faster for reasons that have nothing
to do with tyres. Fitting lap time against tyre age therefore measured
degradation *minus* fuel effect, which is why real races produced negative
slopes and the engine declined to recommend a stop 63% of the time.

### Why regression alone cannot fix it

Within a stint, lap number and tyre age increase together — they are perfectly
collinear. No amount of data separates two perfectly correlated predictors, so
the fuel term has to come from outside the data as a physical prior.

```
actual(lap)    = base(age) + k · fuel_remaining(lap)
fuel_remaining = F₀ − burn·(lap − 1)
corrected(lap) = actual(lap) + k·burn·(lap − 1)

⇒ corrected_slope = measured_slope + k·burn
```

Assumed constants, not measured per circuit: **0.03 s of lap time per kg
carried**, and **110 kg** regulation maximum race fuel. Burn per lap is start
fuel over race distance, so a 44-lap race gets a +0.075 s/lap correction and a
78-lap race +0.042. Live mode falls back to an assumed 57 laps, since OpenF1
reports no lap count for a session in progress.

### Two properties that keep it safe

**The fuel term cancels out of the pit/stay comparison.** Both options run the
same lap with the same fuel load, so `delta` reduces to `slope · age −
pit_cost` exactly as before. The comparison improves only through the
corrected slope — it cannot come to depend on how full the tank is.

**Projected times remain real lap times.** The fit is fuel-free, but
`predict_actual` adds that lap's fuel load back, so the numbers stay
comparable to observed lap times and the backtest's accuracy figures remain
meaningful.

Both slopes are reported — `raw_degradation_s_per_lap` alongside the corrected
one, plus the correction applied. A silent adjustment is indistinguishable
from a bug.

### What it did to the results

**Prediction accuracy improved — the non-circular measure:**

| | before | after |
|---|---|---|
| model MAE (clean laps) | 0.888s | **0.822s** |
| model median AE | 0.328s | **0.305s** |
| persistence MAE (unchanged baseline) | 1.044s | 1.044s |

**Signal quality shifted as predicted:**

| | before | after |
|---|---|---|
| positive | 14.4% | 30.1% |
| unclear | 42.3% | 57.2% |
| negative | 43.4% | **12.7%** |

Confidently-negative collapsed from 43% to 13%, confirming most of that
"tyre getting faster" was fuel burn. Note that the largest destination is
`unclear`, not `positive`: removing the bias moves many slopes to *near* zero,
where the data genuinely cannot support a strong claim. That is the correct
outcome, not a disappointing one.

The engine now declines to recommend a stop in only 1 of 8 races, down from 2.

**The gap to actual pit laps got worse, and that is expected.** Like-for-like
on the four dry races present in both runs, mean absolute gap went from 3.5 to
6.0 laps — and every recommendation is now *earlier* than the team's stop:

| race | before | after |
|---|---|---|
| Jeddah | −1 | −13 |
| Sakhir | −7 | −7 |
| Shanghai | +4 | −1 |
| Silverstone | −2 | −3 |

Detecting more degradation means reaching break-even sooner. Teams pit later
than the pure-pace optimum because of track position, traffic and the
undercut — none of which this tool models. So a systematic early bias is the
expected consequence, and this measure was labelled "not ground truth" for
exactly this reason. The measure that constitutes validation improved; the one
that reflects unmodelled strategy moved away. Both are reported.

### The fixture models fuel burn too

The offline fixture now includes a fuel effect, so the correction has
something real to remove. It demonstrates the whole problem without a network:

| compound | true slope | raw (measured) | corrected |
|---|---|---|---|
| HARD | 0.042 | **−0.016** | 0.041 |
| MEDIUM | 0.070 | 0.006 | 0.063 |
| SOFT | 0.110 | 0.057 | 0.114 |

The hard tyre reads as *getting faster* uncorrected, while actually degrading
at 0.042 s/lap — the exact effect seen on real Baku data.

---

## API key on the WebSocket

`F1_API_KEYS` (comma-separated) gates the stream. Empty disables the check,
which is right locally and wrong deployed. Several keys at once is what makes
a key rotatable without downtime.

### Why not a query parameter

Browsers cannot set headers on a WebSocket handshake, so `?api_key=` is the
obvious channel — and the wrong one. Uvicorn's access log writes the full path
including the query string, so every connection would write a live key to
disk. Demonstrated:

```
GET /health?api_key=would-be-logged HTTP/1.1  200 OK     <- written verbatim
```

Two channels are accepted instead:

| channel | for |
|---|---|
| `Authorization: Bearer <key>` | non-browser clients |
| `Sec-WebSocket-Protocol: f1key.<key>` | browsers, via `new WebSocket(url, [proto])` |

Neither appears in the access log. When a subprotocol is offered the server
must echo one back or the browser closes the connection, including on a
refusal — a rejection the client cannot read is indistinguishable from a
crash.

Keys are compared with `secrets.compare_digest`, so comparison time does not
depend on how many leading characters matched. `==` would leak the key one
character at a time to anyone able to measure response latency.

The key itself is never logged, only whether one was offered and whether it
was valid. `/health` reports `websocket_auth_required` so a misconfiguration
is visible from a curl, without echoing the keys.

### What this does and does not buy

**It is not authentication of users.** The frontend's key is a
`NEXT_PUBLIC_*` value compiled into the browser bundle and readable by anyone
who opens devtools. A public single-page app cannot hold a secret.

What it does buy is real but narrow: it stops scrapers and casual scripts from
opening streams against a free-tier backend with a shared OpenF1 rate budget,
and it makes abuse recoverable, since a key can be rotated without redeploying
the backend. Anything stronger needs a server-side token endpoint minting
short-lived credentials, which is a different piece of work.

---

## Persisted decision log

Every decision is recorded, so a finished race can be reviewed rather than
only watched live.

```
GET /runs                  recent streams, newest first
GET /runs/{id}/decisions   every decision from one run, in lap order
```

### Choices worth explaining

**SQLite, and no storage interface.** The workload is one stream at a time at
roughly one row per lap — a single file handles that without a server to run,
back up or pay for. And unlike `TickSource`, which was written as an interface
with one implementation because a second was known and dated, a second storage
backend here is speculative. A concrete class with a narrow surface is the
honest call; an ABC added for symmetry would be the over-engineering the Day 1
case was not.

**Writes happen in a worker thread.** `sqlite3` is synchronous, and calling it
from the event loop would stall every other connected client for the duration
of the write. On a server whose whole job is streaming, that is the one thing
it must not do, so every operation goes through `asyncio.to_thread` behind a
lock, with WAL mode so the read endpoints work while a stream writes.

**The full message is stored as JSON**, alongside a handful of indexed columns
(`lap`, `verdict`, `compound`, `tyre_age`). `DecisionMessage` gained fields
three times in one week — confidence bounds, then fuel correction — and a
column per field would have meant a migration each time. The log's job is to
reproduce what was actually sent, whatever shape it was in.

**Failures are swallowed, not raised.** The log records a race; it is not part
of serving one. A full, read-only or missing disk degrades to "no log" rather
than ending a stream somebody is watching — the same containment principle as
the decision engine's own. If the log cannot even be opened, the service
starts anyway and `/runs` reports `enabled: false`.

**Runs are pruned** beyond `F1_DECISION_LOG_MAX_RUNS`, with the decisions
cascading, so an unattended service cannot grow without bound.

### Durability, plainly

On a host with an ephemeral filesystem — Render's free tier, and most
container platforms without a mounted volume — the file is lost on every
deploy and every spin-down after 15 minutes idle. It persists **within** a
session, not across them. That is enough to review a race you just watched,
and not enough to build a history. A mounted disk or a hosted database is
needed for the latter.

### What gets recorded

The end reason is recorded too, so a partially-watched race is not mistaken
for a complete one:

| reason | |
|---|---|
| `completed` | the stream reached its end message |
| `client_disconnected` | the viewer closed the tab mid-race |
| `no_live_session` | live mode found nothing running |
| `error:<code>` | the stream failed, with the code |
| `internal_error` | an unexpected exception |

---

## Simplifications

Deliberate, not oversights:

- **Answers "is it faster to pit", never "will pitting cost me a position."**
  Track position is a multi-driver question and is out of scope.
- **Pit-lane cost is one constant (22s).** Circuit-dependent in reality; not
  researched per circuit.
- **Degradation is fitted as a straight line.** Real tyres have a cliff a line
  under-predicts.
- **Fuel burn is corrected with assumed constants** (0.03 s/kg, 110 kg),
  not measured per circuit or per team.
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

---

## What's next

1. **Fuel-burn correction.** The single change that would most improve the
   engine: degradation currently reads negative on most real races, which is
   why it declines to recommend a stop 63% of the time.
2. **Multi-lap lookahead.** The one-lap comparison is why `verdict` reads
   `stay_out` almost always and `laps_to_break_even` is the number to act on.
3. **The live test**, 24-26 September 2026.
