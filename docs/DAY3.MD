# Day 3 — Next.js dashboard, connected to the live stream

## Goal
By the end of today: a Next.js page that connects to the WebSocket, shows the race streaming
in lap by lap, and shows the decision engine's verdict updating live — with the connection
state and the active mode (replay vs. live) always explicit, never ambiguous or silently
broken.

## Why this order
The backend (Days 1-2) is real and tested, but right now the only way to see it work is
reading raw JSON off a WebSocket. Today makes it a legible, demoable product — which matters
for two different audiences: you, verifying the decision engine's behavior is sane over a full
race instead of 3-4 hand-picked scenarios, and later, anyone you show this to who isn't going
to read `check_decisions.py`.

## Design note: build the connection UI mode-generic now, even though live mode isn't real yet
`LiveTickSource` doesn't exist until Day 4 — but the WebSocket route already accepts a `mode`
param (`replay` / `live`) per SPEC.md. Build the frontend's connection logic to pass `mode`
through generically now, rather than hardcoding `mode=replay` everywhere. When Day 4 lands,
this should mean flipping a UI toggle, not restructuring the connection code. Don't build a
mode selector that pretends live mode works yet, though — see scope below.

## Checklist
- [ ] Scaffold a Next.js app (TypeScript). Keep it to one main page — this isn't a multi-page
      product, it's a single live view.
- [ ] A connection utility/hook that opens `ws://<backend>/ws/race/{session_key}?mode=...` and
      tracks state: `connecting` → `streaming` → `ended`, or `error`. Every one of these states
      needs a visible, distinct UI — no blank screen while "connecting," no silent failure on
      `error`.
- [ ] Parse incoming messages by their `type` field (`start`, `tick`, `decision`, `end`,
      `error`) — matches the backend's `StartMessage` / `TickMessage` / `DecisionMessage` /
      `EndMessage` / `ErrorMessage` naming exactly; keep frontend types named the same way so
      there's no translation layer to keep in sync by hand.
- [ ] **Mode/source badge**: show the `source` field from the `start` message
      (`sample` / `historical_replay` / `live`) prominently and literally — this is what the
      server actually reports, not what the user requested. If live mode is ever requested but
      unavailable, whatever the backend actually did should be what's shown, not what was asked
      for.
- [ ] A simple connect form: `session_key` text input (accepting `"sample"` or a real numeric
      key), a Connect button. A `mode` selector can exist in the UI, but `live` should be
      visibly disabled/marked "not yet available" until Day 4 actually implements it — don't
      let the UI promise something the backend can't do yet.
- [ ] Live timeline: a running list (table or cards) of ticks as they arrive — lap number,
      compound, tyre age, lap duration. This should visibly grow as messages come in, not
      require a refresh.
- [ ] Decision panel: shows the latest `decision` message's full contents (verdict +
      degradation rate + both projected times + pit cost + delta), updating every time a new
      one arrives. Before the first `decision` message arrives (there won't be one until at
      least 3 laps of a compound exist, per Day 2), show an explicit "gathering data" state —
      not a blank panel that looks broken.
- [ ] `end` message: show a clear "stream ended" state, distinguishable from `error`.

## What "done" looks like today
Opening the page, entering `sample`, hitting Connect, and watching: the mode badge appear
correctly, the timeline grow lap by lap, and the decision panel go from "gathering data" to a
live-updating verdict with numbers — all without a page reload, and without the browser
console showing unhandled errors on any of the message types.

## Explicitly not today
- No reconnect-on-drop logic — that's Day 4. If the connection drops today, showing `error`
  clearly is enough; automatically retrying is tomorrow's problem.
- No live mode functionality (the toggle can exist in the UI, disabled) — `LiveTickSource`
  isn't built yet.
- No deployment (Day 5).
- No visual polish beyond "clear and correct." A plain table and a plain panel that update
  live and never lie about connection state beats a styled dashboard that hides a broken state.
- No historical-race browser/picker — a plain text input for `session_key` is enough; building
  a race-search UI against `/v1/sessions` is a nice-to-have, not this scope.

## Notes
If the decision panel and the timeline ever visibly disagree — e.g. the panel shows a verdict
for a lap that hasn't appeared in the timeline yet — that's a real bug worth stopping for, not
a display quirk. It usually means the frontend is out of sync with message order, which is the
same class of bug that would also be silently wrong in a genuinely live session later.