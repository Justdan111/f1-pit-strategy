# Day 4 — LiveTickSource + hardening

## Goal
By the end of today: a second `TickSource` implementation that can connect to a genuinely live
F1 session, plus the hardening (reconnects, malformed data, real automated tests) that was
always planned for this day. This is the day the project's primary aim — live mode — actually
gets built, not just specced.

## The honest limit on what "done" can mean today
There is no live F1 session happening right now, and there won't be one until practice starts
September 24. That means `LiveTickSource`'s core behavior — polling for genuinely new laps
during a live window — **cannot be fully proven today**, no matter how good the code is. Don't
let that become an excuse to skip verification entirely, but be honest about what kind of
verification is actually possible before the race weekend:
- **Can verify today:** session-detection logic (is a given session live right now, using
  mocked/fake "current time" against real historical session metadata), rate-limit compliance
  (does the poll loop actually respect the interval), error handling (bad session key,
  unreachable API, no live session), and that `LiveTickSource` produces ticks in the exact
  same shape `ReplayTickSource` does
- **Can only verify September 24-26:** that it actually detects a real live session and streams
  genuinely new laps as they happen

Today's done bar is "correctly built and unit-verified, ready to point at a real session" — not
"proven against real live data," because that test literally cannot happen yet. Say this
explicitly in the README rather than implying more confidence than is possible right now.

## Checklist

### LiveTickSource
- [ ] Determine whether a session is currently live: query `/v1/sessions` (including the
      `session_key=latest` shortcut) and check the current time against that session's
      live window (30 minutes before start to 30 minutes after end)
- [ ] If no session is currently live, this must be a specific, clearly-labeled state — not a
      generic error. Add a distinct case to `ErrorMessage` (or a dedicated message type) that
      says plainly "no live session right now," ideally with when the next one starts
- [ ] Poll `/v1/stints` and `/v1/laps` on an interval that respects the free-tier rate limit
      (~3 req/s, 30 req/min) — pick an interval with real margin, not the theoretical max
- [ ] Track which laps have already been emitted (per driver) so a poll that sees no new laps
      emits nothing, rather than re-sending already-seen ticks
- [ ] Produce ticks in the exact same shape as `ReplayTickSource` (same fields, including
      `lap_duration_s`) — the decision engine and frontend must not need to change at all
- [ ] Handle a 429 (rate limited) or any transient API failure with backoff, not a crash
- [ ] Write a contract test: assert both `ReplayTickSource` and `LiveTickSource` satisfy the
      same `TickSource` interface and produce ticks with the same required fields — this is
      what actually proves the Day 1 abstraction paid off

### Wiring
- [ ] `WS /ws/race/{session_key}?mode=live` selects `LiveTickSource`; `mode=replay` (existing)
      selects `ReplayTickSource`. `session_key=sample` remains replay-only.
- [ ] Frontend: enable the previously-disabled "live" option in the mode selector now that
      it's real

### Hardening (originally scoped for today regardless of live mode)
- [ ] Frontend reconnect logic: on an unexpected drop, retry with backoff, and show a
      `reconnecting` state distinct from the initial `connecting` state
- [ ] Backend: malformed or partially missing data from OpenF1 (either endpoint) should degrade
      gracefully — skip the bad record, don't crash the stream
- [ ] Confirm (don't just assume) the decision engine's existing "not enough data yet" handling
      from Day 2 still holds correctly when ticks come from `LiveTickSource`, not just replay
- [ ] Turn `check_decisions.py`'s scenarios into a real automated test suite (add `pytest` as a
      dev dependency via `uv add --dev pytest`) — this was flagged as owed back on Day 2,
      pay it off now
- [ ] Add a test for rate-limit compliance in the polling loop (mock time/requests, assert it
      never exceeds the configured interval)

## What "done" looks like today
`pytest` passes, including the `TickSource` contract test proving both implementations are
interchangeable. Pointing `LiveTickSource` at a mocked "current time" inside a known past
session's window correctly reports it as live; outside that window, it correctly reports no
live session. The frontend's live toggle is enabled and connects without error (even though,
today, it will correctly report "no live session" every time, since none is happening).

## Explicitly not today
- Multi-driver support
- Deployment (Day 5)
- Proving `LiveTickSource` against a genuinely live session — not possible until September 24
- Any further frontend polish beyond the reconnect state

## Notes
Resist the temptation to "test" live mode by loosening the live-window check so historical data
passes as live — that would validate the wrong thing and hide a real bug if the actual window
logic is off. If you want more confidence before the 24th, add more unit tests around the
window-detection edge cases (exactly 30 minutes before, exactly 30 minutes after) rather than
weakening what counts as "live."