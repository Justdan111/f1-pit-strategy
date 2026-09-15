# Day 5 — Deploy + docs + demo + the real test

## Goal
By the end of today: the whole thing deployed and reachable by a stranger with no local setup
— and then kept an eye on through the actual race weekend, since that's the real completion
criterion for this project's primary aim, not today's deploy itself.

## Hosting — corrected from earlier docs
Earlier docs said "Railway/Fly.io free tier." That's now wrong and worth knowing why, not just
fixing: **Railway dropped its free tier** (small plan fee + metered usage now), and **Fly.io
requires a credit card with no ongoing free allowance for new accounts**. Free hosting still
exists, it's just not where the earlier docs said.

**Backend → Render.** Genuinely free, no credit card required, and its docs specifically cover
FastAPI WebSocket deployment — this isn't a workaround, it's a supported case.

**Frontend → Vercel.** Unchanged from the original plan — free tier is generous for a Next.js
app like this one.

**The one real trade-off to know about, and plan around:** Render's free web services spin
down after 15 minutes of inactivity, with a 30-60 second cold start on the next request. For a
demo clip, that's a minor annoyance (hit the health endpoint once before recording). For the
actual live test during the Azerbaijan GP weekend, it's a real operational risk: if the service
has been asleep, the first connection attempt during a live session could time out or feel
broken for those 30-60 seconds. Plan to explicitly wake it (a simple request to `/health`) a
few minutes before each session window you intend to test against, rather than assuming it's
instantly ready.

## Containerizing the backend
Deploy via a Dockerfile instead of Render's native Python buildpack. This isn't just "add a
Dockerfile because it's expected" — Render building from a Dockerfile you wrote (vs. it
auto-detecting a `pyproject.toml` and guessing the run command) is a genuinely more honest,
more portable deployment: the exact same image runs identically on your machine, in CI, or on
any other host, which the buildpack path doesn't guarantee.

- [ ] Write a `Dockerfile` for the backend: Python base image, install dependencies via `uv`
      (there's an official `uv`-in-Docker pattern — install `uv` in the image, then
      `uv sync --frozen` against the committed `uv.lock` so the container gets exactly the
      versions you tested with, not "whatever resolves at build time")
- [ ] Run the container **locally** first — `docker build` then `docker run`, hit `/health` and
      the sample WebSocket stream against the container, not against `uv run uvicorn` — this is
      the actual point: prove the containerized version behaves identically to what you've been
      testing all week, don't assume it
- [ ] Confirm the container exposes the right port and reads config from environment variables
      passed in at `docker run` time (`-e OPENF1_BASE_URL=...`), not baked into the image —
      same principle as the Render dashboard env vars, just enforced by the container boundary
      instead of by convention
- [ ] Point Render at the Dockerfile instead of its auto-detected Python runtime
- [ ] Add a one-line note to the README on why Docker: reproducibility, not resume-padding —
      say what problem it actually solves for this project specifically

## Checklist
- [ ] Backend deployed to Render **via the Dockerfile**, `/health` reachable at the public URL
- [ ] **`wss://` in production, not `ws://`** — this was flagged as a Day 4 finding: a page
      served over HTTPS cannot open a plain `ws://` connection (browsers block it as mixed
      content). Render provides TLS automatically; confirm the frontend's WebSocket URL is
      built from the deployed `https://` origin so it resolves to `wss://`, not hardcoded to
      `ws://`.
- [ ] Environment variables (OpenF1 base URL, any config) set via Render's dashboard, not
      baked into the repo
- [ ] Frontend deployed to Vercel, pointed at the deployed backend's URL (via an environment
      variable, not hardcoded, so this doesn't need a code change if the backend URL ever
      changes)
- [ ] CORS configured on the backend to explicitly allow the deployed Vercel origin — don't
      leave this wide open, and don't discover it's misconfigured only when the deployed
      frontend can't connect
- [ ] Full smoke test against the **deployed** URLs (not localhost): connect in replay mode
      with `session_key=sample`, watch it stream end-to-end, confirm the decision panel
      updates correctly
- [ ] README finalized: the real problem, why live mode over retrospective analysis, the core
      technical decisions across all 5 days (the `TickSource` abstraction, the null-duration
      catch from Day 4, the negative-degradation handling), what's next
- [ ] Demo clip recorded — replay mode is fine for this since it's very likely happening before
      the race weekend; note in the clip/README that it's demonstrating replay mode
      specifically, don't let a viewer assume it's live

## The real test — not optional, not today specifically
- [ ] Wake the Render service (`GET /health`) shortly before each session window during the
      Azerbaijan GP weekend (practice starts September 24, race is Saturday the 26th)
- [ ] Connect the **deployed** frontend in live mode during an actual session window
- [ ] Confirm: live status is correctly detected, new laps stream in as they happen, the
      decision panel updates using genuinely live data
- [ ] If it fails, that's real information, not a bad outcome — note exactly what broke
      (window detection, rate limiting, the null-duration handling, something else) and treat
      it as the actual final task of this project, not a footnote

## What "done" looks like
A stranger can open the deployed frontend link, connect to a real historical race, and watch
the full pipeline work — and, during the race weekend specifically, the same is true pointed at
a genuinely live session. Both halves matter; replay working well doesn't finish this project on
its own, since live mode was the whole point from Day 1's redesign onward.

## Explicitly not today
- No new features. Day 5 is deploy and verify, not "one more thing"
- No multi-driver support, no weather/safety-car modeling — same exclusions as every day

## Notes
Treat the live test as genuinely unresolved until it happens, not as a formality after a clean
deploy. Everything in Days 1-4 was built and tested against data that behaves predictably —
historical data, mocked clocks, controlled scenarios. September 24-26 is the first time this
touches something that doesn't wait for you to be ready.