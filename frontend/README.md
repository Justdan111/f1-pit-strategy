# Frontend

Next.js dashboard for the F1 Pit Strategy backend. One page: connect, then
watch the race stream in with the decision updating each lap.

```bash
pnpm install
pnpm dev        # http://localhost:3000
```

The backend must be running. Set `NEXT_PUBLIC_BACKEND_WS_URL` if it isn't at
`http://127.0.0.1:8000` — see the [root README](../README.md#configuration).

| Path | |
|---|---|
| `src/lib/types.ts` | wire protocol, mirrored from `backend/models.py` |
| `src/lib/useRaceStream.ts` | socket lifecycle and stream state |
| `src/components/` | connect form, status banner, source badge, timeline, decision panel |
