# Frontend — F1 Pit Strategy dashboard

One page. Connects to the backend's WebSocket, streams a race lap by lap, and
shows the decision engine's verdict with the arithmetic behind it.

## Running it

The backend must be running first:

```bash
cd ../backend
uv run uvicorn backend.main:app --reload      # http://127.0.0.1:8000
```

Then:

```bash
npm install
npm run dev                                    # http://localhost:3000
```

Enter `sample` and press Connect. `sample` streams the offline fixture (58
laps, no network). A numeric key such as `9904` replays a finished race from
OpenF1.

## Configuration

`NEXT_PUBLIC_BACKEND_WS_URL` — WebSocket origin for the backend. Defaults to
`ws://127.0.0.1:8000`. See `.env.example`. Deployment (Day 5) will need `wss://`,
since a page served over https cannot open an insecure WebSocket.

## Layout

| Path | Role |
|---|---|
| `src/lib/types.ts` | the wire protocol, names mirrored from `backend/models.py` |
| `src/lib/useRaceStream.ts` | socket lifecycle + the reducer holding all stream state |
| `src/components/` | ConnectForm, StatusBanner, SourceBadge, Timeline, DecisionPanel |
| `src/app/page.tsx` | the single page |

## Two things worth knowing

**The source badge shows what the server reported**, not what the connect form
requested. Those are different facts, and a dashboard that showed the request
would be able to claim you are watching a live race when you are not.

**`live` mode is in the selector but disabled.** `LiveTickSource` does not exist
until Day 4. The connection layer already passes `mode` through generically, so
enabling it is a one-line change once the backend supports it.

## Not built yet

Reconnect-on-drop (Day 4) — a dropped connection shows an error and waits for you
to press Connect. No deployment (Day 5). No race picker; `session_key` is a plain
text input by design.
