"use client";

import { useCallback, useEffect, useMemo, useRef, useReducer } from "react";
import {
  DecisionMessage,
  Driver,
  Mode,
  NoLiveSessionMessage,
  StartMessage,
  StreamMessage,
  TickMessage,
  isStreamMessage,
} from "./types";

// Capped so a page cannot look connected an hour after giving up; finite so a
// backend that is genuinely down is not hidden behind endless retries.
const RECONNECT_INITIAL_MS = 1000;
const RECONNECT_MAX_MS = 15000;
const RECONNECT_MAX_ATTEMPTS = 6;

function backoffFor(attempt: number): number {
  return Math.min(RECONNECT_INITIAL_MS * 2 ** (attempt - 1), RECONNECT_MAX_MS);
}

export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "reconnecting"
  | "streaming"
  | "ended"
  | "no_live_session"
  | "error";

export interface RaceStreamState {
  status: ConnectionStatus;
  /** From `start`: the server's answer, not the client's request. */
  start: StartMessage | null;
  ticks: TickMessage[];
  latestDecision: DecisionMessage | null;
  decisionCount: number;
  end: { total_ticks: number; total_decisions: number; reason: string } | null;
  error: { detail: string; code: string | null } | null;
  noLiveSession: NoLiveSessionMessage | null;
  reconnectAttempt: number;
  reconnectDelayMs: number;
  /** Decisions that arrived for a lap with no matching tick. Should stay 0. */
  orderingViolations: number;
}

const INITIAL_STATE: RaceStreamState = {
  status: "idle",
  start: null,
  ticks: [],
  latestDecision: null,
  decisionCount: 0,
  end: null,
  error: null,
  noLiveSession: null,
  reconnectAttempt: 0,
  reconnectDelayMs: 0,
  orderingViolations: 0,
};

type Action =
  | { kind: "connect_requested" }
  | { kind: "reconnect_scheduled"; attempt: number; delayMs: number }
  | { kind: "reconnect_attempt" }
  | { kind: "retries_exhausted" }
  | { kind: "socket_open" }
  | { kind: "message"; message: StreamMessage }
  | { kind: "user_disconnected" }
  | { kind: "transport_error"; detail: string }
  | { kind: "socket_closed" }
  | { kind: "reset" };

/**
 * All stream state in one reducer.
 *
 * `ticks` and `latestDecision` are fields of one object, so no render can
 * observe a decision without the ticks preceding it. Separate useState atoms
 * could not guarantee that as a property of the design.
 */
function reducer(state: RaceStreamState, action: Action): RaceStreamState {
  switch (action.kind) {
    case "connect_requested":
      return { ...INITIAL_STATE, status: "connecting" };

    case "reconnect_scheduled":
      return {
        ...state,
        status: "reconnecting",
        reconnectAttempt: action.attempt,
        reconnectDelayMs: action.delayMs,
      };

    case "reconnect_attempt":
      // Ticks are cleared: the server restarts the stream from the beginning.
      return {
        ...INITIAL_STATE,
        status: "connecting",
        reconnectAttempt: state.reconnectAttempt,
      };

    case "socket_open":
      // Not "streaming" yet: the server may still answer with an error.
      return state;

    case "message": {
      const message = action.message;
      switch (message.type) {
        case "start":
          return { ...state, status: "streaming", start: message };

        case "tick":
          return { ...state, ticks: [...state.ticks, message] };

        case "decision": {
          const hasTick = state.ticks.some((t) => t.lap === message.lap);
          if (!hasTick) {
            console.error(
              `[ordering] decision for lap ${message.lap} arrived with no ` +
                `tick for that lap. Latest tick: ` +
                `${state.ticks.at(-1)?.lap ?? "none"}.`,
            );
          }
          return {
            ...state,
            latestDecision: message,
            decisionCount: state.decisionCount + 1,
            orderingViolations: state.orderingViolations + (hasTick ? 0 : 1),
          };
        }

        case "end":
          return {
            ...state,
            status: "ended",
            end: {
              total_ticks: message.total_ticks,
              total_decisions: message.total_decisions,
              reason: message.reason,
            },
          };

        case "no_live_session":
          return { ...state, status: "no_live_session", noLiveSession: message };

        case "error":
          return {
            ...state,
            status: "error",
            error: { detail: message.detail, code: message.code },
          };
      }
      return state;
    }

    case "user_disconnected":
      // `ended`, not `error`: the user asked for it. The reason keeps it
      // distinguishable from a race that actually finished.
      if (state.status === "connecting" || state.status === "streaming") {
        return {
          ...state,
          status: "ended",
          end: {
            total_ticks: state.ticks.length,
            total_decisions: state.decisionCount,
            reason: "disconnected before the stream finished",
          },
        };
      }
      return state;

    case "transport_error":
      return {
        ...state,
        status: "error",
        error: { detail: action.detail, code: "transport_error" },
      };

    case "socket_closed":
      // A close after `end`, `error` or `no_live_session` is the normal path
      // and must not be rewritten. This only fires once retries are spent.
      if (state.status === "streaming" || state.status === "connecting") {
        return {
          ...state,
          status: "error",
          error: {
            detail: "The connection closed before the stream finished.",
            code: "connection_dropped",
          },
        };
      }
      return state;

    case "retries_exhausted":
      return {
        ...state,
        status: "error",
        reconnectAttempt: 0,
        error: {
          detail:
            `The connection dropped and ${RECONNECT_MAX_ATTEMPTS} reconnect ` +
            "attempts all failed. The backend may be down. Press Connect to try again.",
          code: "reconnect_failed",
        },
      };

    case "reset":
      return INITIAL_STATE;
  }
}

/**
 * Normalise a backend URL to a WebSocket origin.
 *
 * An HTTPS page cannot open a plain ws:// connection -- browsers block it as
 * mixed content with no callback -- so https:// must become wss://. Accepting
 * every form matters because an https:// URL is the obvious thing to paste
 * into a hosting dashboard.
 */
function toWebSocketOrigin(raw: string, pageIsSecure: boolean): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (trimmed.startsWith("wss://") || trimmed.startsWith("ws://")) return trimmed;
  if (trimmed.startsWith("https://")) return `wss://${trimmed.slice(8)}`;
  if (trimmed.startsWith("http://")) return `ws://${trimmed.slice(7)}`;
  return `${pageIsSecure ? "wss" : "ws"}://${trimmed}`;
}

export class BackendNotConfiguredError extends Error {}

/**
 * Resolve the backend origin, or say precisely what is missing.
 *
 * The localhost fallback applies only when the page itself is on localhost:
 * in production it would reach the viewer's own machine and fail with an
 * error that explains nothing.
 */
export function resolveBackendOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_BACKEND_WS_URL;
  const isBrowser = typeof window !== "undefined";
  const pageIsSecure = isBrowser && window.location.protocol === "https:";
  const pageIsLocal =
    isBrowser &&
    ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);

  if (configured && configured.trim()) {
    return toWebSocketOrigin(configured, pageIsSecure);
  }
  if (!isBrowser || pageIsLocal) {
    return "ws://127.0.0.1:8000";
  }
  throw new BackendNotConfiguredError(
    "NEXT_PUBLIC_BACKEND_WS_URL is not set. This deployment does not know " +
      "where the backend is. Set it to the backend's URL (for example " +
      "https://your-service.onrender.com) in the hosting dashboard and redeploy.",
  );
}

/**
 * The API key, offered as a WebSocket subprotocol.
 *
 * Browsers cannot set headers on a WebSocket handshake, and a query parameter
 * would be written to the server's access log on every connection. The
 * subprotocol header is the remaining channel, and is not logged.
 *
 * NOT A SECRET. Anything prefixed NEXT_PUBLIC_ is compiled into the browser
 * bundle and readable by anyone who opens devtools. This deters casual abuse
 * of a rate-limited backend and allows a key to be rotated; it does not
 * authenticate users. A public single-page app cannot hold a secret.
 */
export function authSubprotocols(): string[] {
  const key = process.env.NEXT_PUBLIC_API_KEY?.trim();
  return key ? [`f1key.${key}`] : [];
}

export function buildStreamUrl(
  sessionKey: string,
  mode: Mode,
  driverNumber: number,
  backend?: string,
): string {
  const base = (backend ?? resolveBackendOrigin()).replace(/\/$/, "");
  const params = new URLSearchParams({
    mode,
    driver_number: String(driverNumber),
  });
  return `${base}/ws/race/${encodeURIComponent(sessionKey)}?${params}`;
}

/** The backend's HTTP origin: the WebSocket origin with its scheme swapped back. */
export function resolveBackendHttpOrigin(): string {
  return resolveBackendOrigin()
    .replace(/^wss:\/\//, "https://")
    .replace(/^ws:\/\//, "http://");
}

/** Who is entered in a session, for the driver picker. */
export async function fetchDrivers(
  sessionKey: string,
  signal?: AbortSignal,
): Promise<Driver[]> {
  const url = `${resolveBackendHttpOrigin()}/race/${encodeURIComponent(sessionKey)}/drivers`;
  const response = await fetch(url, { signal });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Not JSON; the status line is the best explanation available.
    }
    throw new Error(`Could not load drivers for ${sessionKey}: ${detail}`);
  }
  return (await response.json()) as Driver[];
}

export function useRaceStream() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const socketRef = useRef<WebSocket | null>(null);
  const targetRef = useRef<{
    sessionKey: string;
    mode: Mode;
    driverNumber: number;
  } | null>(null);
  const attemptRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const shouldReconnectRef = useRef(false);
  const scheduleReconnectRef = useRef<() => void>(() => {});

  /** Close without touching state. Used where the state change is elsewhere. */
  const teardown = useCallback(() => {
    shouldReconnectRef.current = false;
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket) {
      // Detach first, so our own teardown cannot rewrite a finished stream.
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      socket.close();
    }
  }, []);

  const disconnect = useCallback(() => {
    // Stop reconnecting first: a user stopping must not be answered by a retry.
    targetRef.current = null;
    attemptRef.current = 0;
    teardown();
    dispatch({ kind: "user_disconnected" });
  }, [teardown]);

  /** Shared by the first connection and every reconnect, so both are the same path. */
  const openSocket = useCallback((sessionKey: string, mode: Mode, driverNumber: number) => {
    let socket: WebSocket;
    try {
      const protocols = authSubprotocols();
      socket = protocols.length
        ? new WebSocket(buildStreamUrl(sessionKey, mode, driverNumber), protocols)
        : new WebSocket(buildStreamUrl(sessionKey, mode, driverNumber));
    } catch (cause) {
      dispatch({
        kind: "transport_error",
        detail:
          cause instanceof BackendNotConfiguredError
            ? cause.message
            : `Could not open a WebSocket: ${String(cause)}`,
      });
      return;
    }
    socketRef.current = socket;
    // Set imperatively, not derived from render state: onclose can fire
    // before React re-renders.
    shouldReconnectRef.current = true;

    socket.onopen = () => dispatch({ kind: "socket_open" });

    socket.onmessage = (event: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(event.data);
      } catch {
        dispatch({
          kind: "transport_error",
          detail: `Server sent a frame that is not JSON: ${event.data.slice(0, 200)}`,
        });
        return;
      }
      if (!isStreamMessage(parsed)) {
        dispatch({
          kind: "transport_error",
          detail: `Unrecognised message type from server: ${JSON.stringify(parsed).slice(0, 200)}`,
        });
        return;
      }
      // A `start` proves the connection is healthy, so the backoff resets.
      if (parsed.type === "start") attemptRef.current = 0;
      // Final answers: retrying would ask the same question again.
      if (
        parsed.type === "end" ||
        parsed.type === "error" ||
        parsed.type === "no_live_session"
      ) {
        shouldReconnectRef.current = false;
      }
      dispatch({ kind: "message", message: parsed });
    };

    socket.onerror = () =>
      dispatch({
        kind: "transport_error",
        detail:
          "WebSocket transport error — the connection could not be " +
          `established to ${resolveBackendOrigin()}. If you are running ` +
          "locally, start the backend with: uv run uvicorn backend.main:app",
      });

    socket.onclose = () => {
      socketRef.current = null;
      if (shouldReconnectRef.current) {
        scheduleReconnectRef.current();
      } else {
        dispatch({ kind: "socket_closed" });
      }
    };
  }, []);

  // openSocket and scheduleReconnect are mutually recursive, so one is reached
  // through a ref. Synced in an effect, never assigned during render.
  const scheduleReconnect = useCallback(() => {
    const target = targetRef.current;
    if (!target) return;

    attemptRef.current += 1;
    if (attemptRef.current > RECONNECT_MAX_ATTEMPTS) {
      dispatch({ kind: "retries_exhausted" });
      return;
    }

    const delay = backoffFor(attemptRef.current);
    dispatch({
      kind: "reconnect_scheduled",
      attempt: attemptRef.current,
      delayMs: delay,
    });

    timerRef.current = window.setTimeout(() => {
      dispatch({ kind: "reconnect_attempt" });
      openSocket(target.sessionKey, target.mode, target.driverNumber);
    }, delay);
  }, [openSocket]);

  useEffect(() => {
    scheduleReconnectRef.current = scheduleReconnect;
  }, [scheduleReconnect]);

  const connect = useCallback(
    (sessionKey: string, mode: Mode, driverNumber: number) => {
      teardown();
      targetRef.current = { sessionKey, mode, driverNumber };
      attemptRef.current = 0;
      dispatch({ kind: "connect_requested" });
      openSocket(sessionKey, mode, driverNumber);
    },
    [teardown, openSocket],
  );

  useEffect(() => teardown, [teardown]);

  const reset = useCallback(() => {
    teardown();
    dispatch({ kind: "reset" });
  }, [teardown]);

  const latestTickLap = useMemo(
    () => state.ticks.at(-1)?.lap ?? null,
    [state.ticks],
  );

  return { state, connect, disconnect, reset, latestTickLap };
}
