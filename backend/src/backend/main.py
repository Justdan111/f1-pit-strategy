"""FastAPI application: health, a debug REST route, and the WebSocket stream."""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import extract_key, is_authorised, selected_subprotocol
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
from .storage import DecisionStore
from .tick_source import TickSource, TickSourceError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own one pooled HTTP client for the process, not one per request."""
    settings = get_settings()
    store: DecisionStore | None = None
    if settings.decision_log_enabled:
        store = DecisionStore(
            settings.decision_log_path, max_runs=settings.decision_log_max_runs
        )
        try:
            await store.open()
        except Exception:
            # An unusable log must not stop the service from streaming.
            logger.exception("Could not open the decision log; continuing without it.")
            store = None

    async with httpx.AsyncClient(headers={"Accept": "application/json"}) as http:
        app.state.settings = settings
        app.state.http = http
        app.state.openf1 = OpenF1Client(http, settings)
        app.state.store = store
        logger.info("Startup complete. OpenF1 base URL: %s", settings.openf1_base_url)
        yield

    if store is not None:
        await store.close()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="F1 Pit Strategy",
    version="1.0.0",
    summary="Live and replay pit/stay decisions streamed over a WebSocket.",
    lifespan=lifespan,
)


_settings = get_settings()
_origins = _settings.allowed_origin_list

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=bool(_origins),
    allow_methods=["GET"],
    allow_headers=["*"],
)

_api_keys = _settings.api_key_list

if not _origins:
    logger.warning(
        "F1_ALLOWED_ORIGINS is not set: all origins are allowed. Fine locally, "
        "not for a deployed service."
    )
if not _api_keys:
    logger.warning(
        "F1_API_KEYS is not set: the WebSocket accepts unauthenticated "
        "connections. Fine locally, not for a deployed service."
    )


def _origin_allowed(origin: str | None) -> bool:
    """Whether a WebSocket handshake from this Origin may proceed.

    WebSockets are not subject to CORS -- browsers send Origin but do not
    preflight, and no CORS header can refuse one -- so this check is separate.
    A non-browser client sends no Origin and is allowed: Origin is a browser
    guarantee, not authentication.
    """
    if not _origins:
        return True
    if origin is None:
        return True
    return origin.rstrip("/") in _origins


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe. Does no I/O, so it cannot report us unhealthy when OpenF1 is down."""
    settings: Settings = get_settings()
    return {
        "status": "ok",
        "service": "f1-pit-strategy",
        "modes_available": ["replay", "live"],
        "live_poll_interval_seconds": settings.live_poll_interval_seconds,
        "live_window_margin_minutes": settings.live_window_margin_minutes,
        "replay_tick_interval_seconds": settings.replay_tick_interval_seconds,
        "pit_lane_cost_seconds": settings.pit_lane_cost_seconds,
        "min_samples_for_fit": settings.min_samples_for_fit,
        # Echoed so a misconfiguration is visible from a curl rather than
        # discovered as a frontend that cannot connect. The keys themselves
        # are never echoed, only whether any are configured.
        "allowed_origins": settings.allowed_origin_list or ["*"],
        "websocket_auth_required": bool(settings.api_key_list),
    }


@app.get("/race/sample/stints")
async def get_sample_stints() -> list[Stint]:
    """Debug only: raw sample stints, for comparing against emitted ticks."""
    return sample_stints()


@app.get("/runs")
async def list_runs(limit: int = Query(50, ge=1, le=200)) -> dict[str, object]:
    """Recent recorded streams, newest first."""
    store: DecisionStore | None = app.state.store
    if store is None:
        return {"enabled": False, "runs": []}
    runs = await store.list_runs(limit)
    return {"enabled": True, "runs": [vars(r) for r in runs]}


@app.get("/runs/{run_id}/decisions")
async def get_run_decisions(run_id: int) -> dict[str, object]:
    """Every decision from one run, in lap order: a race to review after the fact."""
    store: DecisionStore | None = app.state.store
    if store is None:
        raise HTTPException(status_code=404, detail="The decision log is disabled.")
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}.")
    return {"run": vars(run), "decisions": await store.get_decisions(run_id)}


def _build_source(
    *,
    app: FastAPI,
    session_key: str,
    mode: str,
    driver_number: int | None,
    tick_interval_seconds: float | None,
) -> TickSource:
    """Choose a TickSource for this connection. The only mode-aware code here."""
    settings: Settings = app.state.settings

    if mode == "live":
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
    """Keep a client-supplied pacing override inside server-decided bounds."""
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
    driver_number: int | None = Query(
        None, description="Defaults to the car that ran furthest."
    ),
    tick_interval: float | None = Query(
        None, description="Override seconds between ticks."
    ),
) -> None:
    """Stream one race: start -> (tick, decision)* -> end, or error.

    The connection is accepted before anything is validated, so a rejection
    can be explained in-protocol rather than as an opaque handshake failure.
    """
    subprotocols: list[str] = websocket.scope.get("subprotocols") or []
    # Echoed on every accept, including refusals: a browser that offered a
    # subprotocol closes the connection unless the server names one back, and
    # a refusal the client cannot read is indistinguishable from a crash.
    subprotocol = selected_subprotocol(subprotocols)

    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin):
        await websocket.accept(subprotocol=subprotocol)
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

    presented = extract_key(websocket.headers.get("authorization"), subprotocols)
    if not is_authorised(presented, _api_keys):
        await websocket.accept(subprotocol=subprotocol)
        # The key itself is never logged, only whether one was offered.
        logger.warning(
            "Refused WebSocket: %s API key.",
            "invalid" if presented else "missing",
        )
        await _send_error(
            websocket,
            detail=(
                "A valid API key is required to open a stream. Send it as "
                "'Authorization: Bearer <key>', or from a browser as the "
                "WebSocket subprotocol 'f1key.<key>'."
            ),
            code="unauthorized",
        )
        await _close_quietly(websocket)
        return

    await websocket.accept(subprotocol=subprotocol)

    settings: Settings = websocket.app.state.settings
    interval = _clamp_interval(tick_interval, settings)

    source: TickSource | None = None
    sent = 0
    decisions_sent = 0
    run_id: int | None = None
    _close_reason = "incomplete"
    # One engine per connection: a fresh connection cannot inherit history.
    # Built after open(), because the fuel correction scales with race
    # distance and that is only known once the source has described itself.
    engine: DecisionEngine | None = None

    try:
        source = _build_source(
            app=websocket.app,
            session_key=session_key,
            mode=mode,
            driver_number=driver_number,
            tick_interval_seconds=interval,
        )

        start = await source.open()
        await websocket.send_json(start.model_dump(mode="json"))

        # total_laps is None in live mode, where the engine falls back to an
        # assumed race distance.
        engine = DecisionEngine(settings, total_laps=start.total_laps)

        store: DecisionStore | None = websocket.app.state.store
        if store is not None:
            run_id = await store.start_run(start)

        # This loop has no idea what is behind `source`.
        async for tick in source.ticks():
            await websocket.send_json(tick.model_dump(mode="json"))
            sent += 1

            decision = _safe_decision(engine, tick)
            if decision is not None:
                await websocket.send_json(decision.model_dump(mode="json"))
                decisions_sent += 1
                # Recorded after sending, so persistence can never delay the
                # client. Failures inside the store are swallowed there.
                if store is not None and run_id is not None:
                    await store.record_decision(run_id, decision)

        await websocket.send_json(
            EndMessage(
                session_key=session_key,
                total_ticks=sent,
                total_decisions=decisions_sent,
                reason="completed",
            ).model_dump(mode="json")
        )
        _close_reason = "completed"

    except WebSocketDisconnect:
        _close_reason = "client_disconnected"
        logger.info(
            "Client disconnected from session_key=%s after %d ticks, %d decisions.",
            session_key,
            sent,
            decisions_sent,
        )

    except NoLiveSessionError as exc:
        # Not an error: the request worked and the answer was "no race is
        # happening", which is live mode's normal state.
        _close_reason = "no_live_session"
        logger.info("No live session for session_key=%s: %s", session_key, exc.detail)
        await _send_no_live_session(websocket, exc)

    except (TickSourceError, OpenF1Error) as exc:
        _close_reason = f"error:{exc.code}"
        detail = getattr(exc, "detail", str(exc))
        logger.warning("Stream failed for session_key=%s: %s", session_key, detail)
        await _send_error(websocket, detail=detail, code=exc.code)

    except Exception as exc:  # noqa: BLE001 - last line of defence
        _close_reason = "internal_error"
        logger.exception("Unexpected error streaming session_key=%s", session_key)
        await _send_error(
            websocket,
            detail=f"Unexpected server error while streaming: {type(exc).__name__}.",
            code="internal_error",
        )

    finally:
        store = websocket.app.state.store
        if store is not None and run_id is not None:
            await store.finish_run(
                run_id,
                total_ticks=sent,
                total_decisions=decisions_sent,
                reason=_close_reason,
            )
        if source is not None:
            await source.close()
        await _close_quietly(websocket)


def _safe_decision(engine: DecisionEngine, tick: TickMessage) -> DecisionMessage | None:
    """Compute a decision, never letting it break the stream.

    None is expected: too few clean samples for this compound yet. An
    exception is a bug, and the tick stream is still valid without decisions.
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
    """Send an `error` envelope, tolerating an already-dead socket."""
    try:
        await websocket.send_json(
            ErrorMessage(detail=detail, code=code).model_dump(mode="json")
        )
    except (WebSocketDisconnect, RuntimeError):
        logger.debug("Could not deliver error envelope; client already gone.")


async def _close_quietly(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "f1-pit-strategy",
            "endpoints": {
                "health": "GET /health",
                "sample_stints_debug": "GET /race/sample/stints",
                "runs": "GET /runs",
                "run_decisions": "GET /runs/{id}/decisions",
                "stream": "WS /ws/race/{session_key}?mode=replay|live",
                "live": f"WS /ws/race/{LATEST_SESSION_KEY}?mode=live",
            },
        }
    )
