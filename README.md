# F1 Pit Strategy

Live pit-stop decisions for Formula 1. Streams a race lap by lap and answers,
continuously: **pit now, or stay out?**

![Dashboard streaming a race in replay mode](docs/demo-streaming.png)

Tyres degrade — lap times get slower every lap on a set — but a pit stop costs
about 22 seconds, and you only get that back by running long enough on the new
set to earn it. This does that arithmetic every lap and shows every number
behind the answer.

Works against a genuinely live session, or replays a finished race through the
same pipeline.

## Try it

| | |
|---|---|
| Dashboard | https://f1-pit-strategy.vercel.app |
| API | https://f1-pit-strategy.onrender.com |
| Health | https://f1-pit-strategy.onrender.com/health |

- **replay** + `sample` — offline fixture, 58 laps, no network
- **replay** + `9904` — 2025 Azerbaijan GP
- **live** + `latest` — whatever session is running now

> Screenshots and the demo show **replay mode**, marked by the `SAMPLE` /
> `REPLAY` badge. Live mode shows `LIVE`, and only once the server confirms a
> session is actually running.

## Status

Live mode is built and unit-tested but **has not yet run against a live
session** — the first is Azerbaijan practice, 24 September 2026. Replay mode
is verified end to end. See [docs/ENGINEERING.md](docs/ENGINEERING.md#verification-status)
for exactly what has and hasn't been proven.

## Running locally

**Backend** — Python 3.11, [uv](https://docs.astral.sh/uv/):

```bash
cd backend
uv sync
uv run uvicorn backend.main:app --reload    # http://127.0.0.1:8000
uv run pytest                               # 161 tests, no network
uv run python scripts/backtest.py           # score the engine on real races
```

Or the container, which is what production runs:

```bash
cd backend
docker build -t f1-pit-strategy-backend .
docker run --rm -p 8000:8000 -e PORT=8000 \
  -e F1_ALLOWED_ORIGINS=http://localhost:3000 \
  f1-pit-strategy-backend
```

**Frontend** — Node, pnpm:

```bash
cd frontend
pnpm install
pnpm dev                                    # http://localhost:3000
```

Without a browser, `backend/scripts/ws_client.py` prints the raw stream:

```bash
uv run python scripts/ws_client.py --tick-interval 0.05
uv run python scripts/ws_client.py --session-key latest --mode live
```

## Configuration

Read from the environment at startup; nothing is baked into the image.

**Backend** (prefix `F1_`):

| Variable | Default | |
|---|---|---|
| `PORT` | `8000` | Port to bind. Render sets this. |
| `F1_ALLOWED_ORIGINS` | *(empty = allow all)* | Comma-separated browser origins. **Set in production.** |
| `F1_API_KEYS` | *(empty = no auth)* | Comma-separated keys required on the WebSocket. Several allow rotation without downtime. |
| `F1_DECISION_LOG_ENABLED` | `true` | Record every decision for later review |
| `F1_DECISION_LOG_PATH` | `decisions.db` | SQLite file. **Needs a mounted disk to survive a redeploy.** |
| `F1_DECISION_LOG_MAX_RUNS` | `200` | Oldest runs pruned beyond this |
| `F1_OPENF1_BASE_URL` | `https://api.openf1.org/v1` | OpenF1 API root |
| `F1_REPLAY_TICK_INTERVAL_SECONDS` | `0.5` | Replay pacing |
| `F1_PIT_LANE_COST_SECONDS` | `22.0` | Pit-lane time loss |
| `F1_LIVE_POLL_INTERVAL_SECONDS` | `10.0` | Live poll interval |

`/health` echoes the effective configuration, so a misconfiguration shows up in
a `curl` rather than as a frontend that won't connect.

### Reviewing a past race

Every decision is recorded, so a finished stream can be replayed after the fact:

```bash
curl -s localhost:8000/runs                  # recent streams
curl -s localhost:8000/runs/1/decisions      # every decision from one, in lap order
```

> On a host with an ephemeral filesystem — Render's free tier included — this
> file is lost on every deploy and every spin-down. It persists within a
> session, not across them. A mounted disk or a hosted database is needed for
> anything longer-lived.

**Frontend:**

| Variable | |
|---|---|
| `NEXT_PUBLIC_BACKEND_WS_URL` | Backend URL. Accepts `https://`, `http://`, `wss://`, `ws://` or a bare host; the scheme is normalised. |
| `NEXT_PUBLIC_API_KEY` | API key, if the backend requires one. Sent as a WebSocket subprotocol, never in the URL. |

An `https://` value becomes `wss://` automatically — an HTTPS page cannot open
a plain `ws://` connection.

> `NEXT_PUBLIC_*` values are compiled into the browser bundle and readable by
> anyone. The API key deters casual abuse of a rate-limited backend and can be
> rotated; it does not authenticate users. A public single-page app cannot hold
> a secret.

## Deploying

**Backend → Render.** New → Blueprint, pointed at this repo. `render.yaml`
builds from `backend/Dockerfile` rather than the Python buildpack. Set
`F1_ALLOWED_ORIGINS` to the frontend URL.

**Frontend → Vercel.** Import the repo, Root Directory `frontend`. The
backend URL is already set in `frontend/.env.production`, so no dashboard
configuration is needed.

> Render's free tier spins down after 15 minutes idle, with a 30–60 second cold
> start. Hit `/health` a few minutes before a live session.

## Stack

FastAPI · Next.js · [OpenF1](https://openf1.org) · Docker · Render · Vercel

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works: components, the `TickSource` seam, connection lifecycle, concurrency and failure models, deployment |
| [docs/API.md](docs/API.md) | Every endpoint, the message protocol, error codes, full configuration |
| [docs/ENGINEERING.md](docs/ENGINEERING.md) | Why the numbers are what they are, and what has actually been verified |
