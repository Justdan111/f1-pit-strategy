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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .decision_engine import DecisionEngine
from .live import LATEST_SESSION_KEY, LiveTickSource, NoLiveSessionError
from .models import (
    DecisionMessage,
    EndMessage,
    ErrorMessage,
    NoLiveSessionMessage,
    Stint,
    TickMessage,
)
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


# --- CORS ----------------------------------------------------------------
#
# Applies to the HTTP routes. Note what it does NOT cover: WebSocket
# handshakes are not subject to CORS at all — browsers send an Origin header
# but do not preflight, and no CORS header can refuse one. Restricting who
# may open a stream therefore has to be done explicitly in the handler, which
# is what `_origin_allowed` below does.
_settings = get_settings()
_origins = _settings.allowed_origin_list

app.add_middleware(
    CORSMiddleware,
    # An empty configuration means local development, where the frontend is
    # on :3000 and the backend on :8000 — different origins, so without this
    # even local use would fail. Deployment sets the variable explicitly.
    allow_origins=_origins or ["*"],
    allow_credentials=bool(_origins),
    allow_methods=["GET"],
    allow_headers=["*"],
)

if not _origins:
    logger.warning(
        "F1_ALLOWED_ORIGINS is not set: all origins are allowed. Fine locally, "
        "not for a deployed service."
    )


def _origin_allowed(origin: str | None) -> bool:
    """Whether a WebSocket handshake from this Origin may proceed.

    Unrestricted when no origins are configured (local development). A
    non-browser client such as scripts/ws_client.py sends no Origin header at
    all and is allowed through — Origin is a browser guarantee, not an
    authentication mechanism, and pretending otherwise would give a false
    sense of protection while breaking the CLI tools.
    """
    if not _origins:
        return True
    if origin is None:
        return True
    return origin.rstrip("/") in _origins


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
        "day": 5,
        "modes_available": ["replay", "live"],
        "live_poll_interval_seconds": settings.live_poll_interval_seconds,
        "live_window_margin_minutes": settings.live_window_margin_minutes,
        "replay_tick_interval_seconds": settings.replay_tick_interval_seconds,
        "pit_lane_cost_seconds": settings.pit_lane_cost_seconds,
        "min_samples_for_fit": settings.min_samples_for_fit,
        # Echoed so a CORS misconfiguration is visible from a curl against
        # the deployed service, rather than only as a frontend that cannot
        # connect (DAY5.md asks for exactly this not to be a late discovery).
        "allowed_origins": settings.allowed_origin_list or ["*"],
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
        # The Day 1 prediction, realised: one branch here, and nothing
        # downstream of TickSource changed to accommodate it.
        if session_key == SAMPLE_SESSION_KEY:
            raise TickSourceError(
                "session_key=sample is a fixture and can only be replayed. "
                "Use ?mode=live with a real session_key, or 'latest'.",
                code="sample_is_replay_only",
            )
        return LiveTickSource(
            client=app.state.openf1,
            session_key=session_key,
            driver_number=driver_number,
            settings=settings,
        )

    if mode != "replay":
        raise TickSourceError(
            f"Unknown mode {mode!r}. Valid modes: 'replay', 'live'.",
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
    mode: str = Query("replay", description="'replay' or 'live'."),
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
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin):
        # Accept first, then refuse in-protocol. A rejected handshake gives
        # the browser an opaque failure; this way the reason is readable.
        await websocket.accept()
        logger.warning("Refused WebSocket from disallowed origin: %r", origin)
        await _send_error(
            websocket,
            detail=(
                f"Origin {origin!r} is not allowed to open a stream. "
                f"Configured origins: {_origins}."
            ),
            code="origin_not_allowed",
        )
        await _close_quietly(websocket)
        return

    await websocket.accept()

    settings: Settings = websocket.app.state.settings
    interval = _clamp_interval(tick_interval, settings)

    source: TickSource | None = None
    sent = 0
    decisions_sent = 0

    # One engine per connection, constructed here and never shared. That is
    # the whole of the "incremental, only data seen so far" requirement made
    # structural: a fresh connection cannot inherit another connection's
    # history, because there is nothing to inherit it from.
    engine = DecisionEngine(settings)

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

            decision = _safe_decision(engine, tick)
            if decision is not None:
                await websocket.send_json(decision.model_dump(mode="json"))
                decisions_sent += 1

        # Falling out of the loop means the source is exhausted: replay
        # finished, or a live session ended.
        await websocket.send_json(
            EndMessage(
                session_key=session_key,
                total_ticks=sent,
                total_decisions=decisions_sent,
                reason="completed",
            ).model_dump(mode="json")
        )

    except WebSocketDisconnect:
        # Entirely normal: the client closed the tab mid-race. Not an error,
        # and there is nobody left to send an error envelope to.
        logger.info(
            "Client disconnected from session_key=%s after %d ticks, %d decisions.",
            session_key,
            sent,
            decisions_sent,
        )

    except NoLiveSessionError as exc:
        # NOT an error. The request worked, the API answered, and the answer
        # was "no race is happening" — which is what live mode will report
        # almost every day of the year (SPEC section 10). Sent as its own
        # message type so the dashboard can show it as information rather
        # than as a red failure, then closed cleanly.
        logger.info("No live session for session_key=%s: %s", session_key, exc.detail)
        await _send_no_live_session(websocket, exc)

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


def _safe_decision(engine: DecisionEngine, tick: TickMessage) -> DecisionMessage | None:
    """Compute a decision for one tick, never letting it break the stream.

    Two distinct "no decision" cases, and they are not the same thing:

    - engine.observe() returns None. Expected and normal: fewer than the
      minimum clean samples for this compound, or every sample at one tyre
      age. Nothing has gone wrong; there is simply nothing honest to say yet.
      DAY2.md asks for exactly this — skip, don't crash.

    - engine.observe() raises. A bug. The tick stream is still valid and the
      client is still entitled to it, so the stream continues without
      decisions rather than dying. Logged with a full traceback, because a
      silently decision-free stream is precisely the "looks fine, isn't"
      failure this day is supposed to guard against.
    """
    try:
        return engine.observe(tick)
    except Exception:
        logger.exception(
            "Decision engine failed on lap %s (compound=%s, tyre_age=%s). "
            "Continuing the tick stream without decisions.",
            tick.lap,
            tick.compound,
            tick.tyre_age,
        )
        return None


async def _send_no_live_session(
    websocket: WebSocket, exc: NoLiveSessionError
) -> None:
    """Deliver the `no_live_session` envelope, tolerating a dead socket."""
    nxt = exc.next_session
    message = NoLiveSessionMessage(
        detail=exc.detail,
        checked_at=exc.checked_at,
        next_session_key=nxt.session_key if nxt else None,
        next_session_name=nxt.label if nxt else None,
        next_session_start=nxt.date_start if nxt else None,
    )
    try:
        await websocket.send_json(message.model_dump(mode="json"))
    except (WebSocketDisconnect, RuntimeError):
        logger.debug("Could not deliver no_live_session; client already gone.")


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
                "stream": "WS /ws/race/{session_key}?mode=replay|live",
                "live": f"WS /ws/race/{LATEST_SESSION_KEY}?mode=live",
            },
            "try": "WS /ws/race/sample",
        }
    )
