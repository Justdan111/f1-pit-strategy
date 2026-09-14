"""FastAPI application: health, a debug REST route, and the WebSocket stream.

The WebSocket handler is the part worth reading closely. Note what it does
NOT do: it never mentions replay, never touches stint data, never knows where
a tick came from. It talks to a `TickSource` and nothing else. That is the
Day 1 goal stated in DAY1.md — on Day 4, `_build_source` gains one branch
that returns a LiveTickSource, and everything below it is untouched.
"""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .models import EndMessage, ErrorMessage, Stint
from .openf1_client import OpenF1Client, OpenF1Error
from .replay import ReplayTickSource
from .sample_data import SAMPLE_SESSION_KEY, sample_stints
from .tick_source import TickSource, TickSourceError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own long-lived resources for the process, not per request.

    One pooled httpx.AsyncClient is created at startup and closed at
    shutdown. Every OpenF1 call in the process shares its connection pool, so
    a replay connection doesn't pay a fresh TCP + TLS handshake, and nothing
    leaks when a handler raises. This matters more on Day 4, when live mode
    polls the same host every few seconds for an entire race.
    """
    settings = get_settings()
    async with httpx.AsyncClient(headers={"Accept": "application/json"}) as http:
        app.state.settings = settings
        app.state.http = http
        app.state.openf1 = OpenF1Client(http, settings)
        logger.info("Startup complete. OpenF1 base URL: %s", settings.openf1_base_url)
        yield
    logger.info("Shutdown complete.")


app = FastAPI(
    title="F1 Pit Strategy Simulator",
    version="0.1.0",
    summary="Day 1: streaming skeleton built around the TickSource interface.",
    lifespan=lifespan,
)


# --- REST ----------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe. Deliberately does no I/O.

    A health check that called OpenF1 would report us unhealthy whenever
    somebody else's API was down, and would fail deploys for no good reason.
    This answers one question: is this process up and serving?
    """
    settings: Settings = get_settings()
    return {
        "status": "ok",
        "service": "f1-pit-strategy",
        "day": 1,
        "modes_available": ["replay"],
        "modes_planned": ["live"],
        "replay_tick_interval_seconds": settings.replay_tick_interval_seconds,
    }


@app.get("/race/sample/stints")
async def get_sample_stints() -> list[Stint]:
    """DEBUG ONLY: the raw sample stints, before any flattening.

    Not the product surface — the product surface is the WebSocket. This
    exists so that when a tick looks wrong you can compare it against the
    input with curl, and immediately tell whether the bug is in the data or
    in ReplayTickSource._flatten.
    """
    return sample_stints()


# --- WebSocket -----------------------------------------------------------


def _build_source(
    *,
    app: FastAPI,
    session_key: str,
    mode: str,
    driver_number: int | None,
    tick_interval_seconds: float | None,
) -> TickSource:
    """Choose a TickSource for this connection. The only mode-aware code here.

    SPEC 6.6: mode selects the implementation; "sample" is only valid under
    replay. On Day 4 this function gains a `mode == "live"` branch returning
    a LiveTickSource, and nothing else in this module changes.
    """
    settings: Settings = app.state.settings

    if mode == "live":
        raise TickSourceError(
            "Live mode is not implemented yet (planned for Day 4). "
            "Use ?mode=replay with a finished session_key, or session_key=sample.",
            code="live_mode_not_implemented",
        )

    if mode != "replay":
        raise TickSourceError(
            f"Unknown mode {mode!r}. Valid modes: 'replay' (and 'live', Day 4).",
            code="unknown_mode",
        )

    if session_key == SAMPLE_SESSION_KEY:
        return ReplayTickSource.from_sample(
            tick_interval_seconds=tick_interval_seconds,
            settings=settings,
        )

    return ReplayTickSource.from_openf1(
        client=app.state.openf1,
        session_key=session_key,
        driver_number=driver_number,
        tick_interval_seconds=tick_interval_seconds,
        settings=settings,
    )


def _clamp_interval(value: float | None, settings: Settings) -> float | None:
    """Keep a client-supplied pacing override inside sane bounds.

    ?tick_interval=0 is genuinely useful (dump the whole race instantly in a
    test). ?tick_interval=86400 would pin a connection open for a day, so the
    server decides the ceiling, not the client.
    """
    if value is None:
        return None
    return max(
        settings.min_tick_interval_seconds,
        min(value, settings.max_tick_interval_seconds),
    )


@app.websocket("/ws/race/{session_key}")
async def stream_race(
    websocket: WebSocket,
    session_key: str,
    mode: str = Query("replay", description="'replay' today; 'live' lands on Day 4."),
    driver_number: int | None = Query(None, description="Defaults to the car that ran furthest."),
    tick_interval: float | None = Query(None, description="Override seconds between ticks."),
) -> None:
    """Stream one race, lap by lap: start -> tick* -> end (or error).

    Structure of the handler, and why:

      accept -> build source -> open() -> send start -> stream -> send end

    We accept the connection *before* validating anything, because a client
    can only be told what went wrong over an open socket. Rejecting the
    handshake would give the browser an opaque failure with no detail; an
    accepted socket lets us send a real `error` envelope and then close
    cleanly. Errors are part of the protocol, not an absence of it.
    """
    await websocket.accept()

    settings: Settings = websocket.app.state.settings
    interval = _clamp_interval(tick_interval, settings)

    source: TickSource | None = None
    sent = 0

    try:
        source = _build_source(
            app=websocket.app,
            session_key=session_key,
            mode=mode,
            driver_number=driver_number,
            tick_interval_seconds=interval,
        )

        # All fallible setup happens here, before we claim the stream started.
        start = await source.open()
        await websocket.send_json(start.model_dump(mode="json"))

        # The heart of it. This loop has no idea what is behind `source`:
        # a fixture, a historical race, or (Day 4) a live session polling
        # OpenF1 every few seconds. Identical code serves all three.
        async for tick in source.ticks():
            await websocket.send_json(tick.model_dump(mode="json"))
            sent += 1

        # Falling out of the loop means the source is exhausted: replay
        # finished, or a live session ended.
        await websocket.send_json(
            EndMessage(
                session_key=session_key,
                total_ticks=sent,
                reason="completed",
            ).model_dump(mode="json")
        )

    except WebSocketDisconnect:
        # Entirely normal: the client closed the tab mid-race. Not an error,
        # and there is nobody left to send an error envelope to.
        logger.info(
            "Client disconnected from session_key=%s after %d ticks.", session_key, sent
        )

    except (TickSourceError, OpenF1Error) as exc:
        # Expected, explainable failures: unknown mode, bad session_key,
        # OpenF1 unreachable. Reported in-protocol so the frontend can show
        # something useful rather than "connection closed".
        detail = getattr(exc, "detail", str(exc))
        logger.warning("Stream failed for session_key=%s: %s", session_key, detail)
        await _send_error(websocket, detail=detail, code=exc.code)

    except Exception as exc:  # noqa: BLE001 - last line of defence
        # Unexpected. Log the full traceback server-side; send the client
        # something honest but non-leaky.
        logger.exception("Unexpected error streaming session_key=%s", session_key)
        await _send_error(
            websocket,
            detail=f"Unexpected server error while streaming: {type(exc).__name__}.",
            code="internal_error",
        )

    finally:
        # Release per-connection resources whatever happened. The shared
        # httpx client is owned by the app lifespan, so close() here is a
        # no-op for replay sources — it exists for whatever Day 4 holds open.
        if source is not None:
            await source.close()
        await _close_quietly(websocket)


async def _send_error(websocket: WebSocket, *, detail: str, code: str | None) -> None:
    """Send an `error` envelope, tolerating an already-dead socket.

    If the client vanished, the send raises — and there is nothing useful to
    do about it, so we swallow it rather than masking the original error with
    a second one.
    """
    try:
        await websocket.send_json(
            ErrorMessage(detail=detail, code=code).model_dump(mode="json")
        )
    except (WebSocketDisconnect, RuntimeError):
        logger.debug("Could not deliver error envelope; client already gone.")


async def _close_quietly(websocket: WebSocket) -> None:
    """Close the socket, ignoring the case where it is already closed."""
    try:
        await websocket.close()
    except (WebSocketDisconnect, RuntimeError):
        pass


# --- misc ----------------------------------------------------------------


@app.get("/")
async def root() -> JSONResponse:
    """A signpost, so hitting the root in a browser isn't just a 404."""
    return JSONResponse(
        {
            "service": "f1-pit-strategy",
            "endpoints": {
                "health": "GET /health",
                "sample_stints_debug": "GET /race/sample/stints",
                "stream": "WS /ws/race/{session_key}?mode=replay",
            },
            "try": "WS /ws/race/sample",
        }
    )
