"use client";

import { useCallback, useEffect, useMemo, useRef, useReducer } from "react";
import {
  DecisionMessage,
  Mode,
  NoLiveSessionMessage,
  StartMessage,
  StreamMessage,
  TickMessage,
  isStreamMessage,
} from "./types";

/**
 * Reconnect backoff (Day 4).
 *
 * Doubling from 1s, capped at 15s, for at most 6 attempts. Capped because an
 * unbounded backoff eventually means a page that looks connected but gave up
 * an hour ago; finite because retrying forever hides a backend that is
 * genuinely down. When the attempts run out the UI says so and hands control
 * back to the user.
 */
const RECONNECT_INITIAL_MS = 1000;
const RECONNECT_MAX_MS = 15000;
const RECONNECT_MAX_ATTEMPTS = 6;

function backoffFor(attempt: number): number {
  return Math.min(RECONNECT_INITIAL_MS * 2 ** (attempt - 1), RECONNECT_MAX_MS);
}

/**
 * Connection lifecycle.
 *
 * DAY3.md names four: connecting / streaming / ended / error. `idle` is a
 * fifth, for "the page is open and nothing has been attempted yet" — without
 * it, the initial render would have to pretend to be one of the other four,
 * and "connecting" before anyone pressed Connect is exactly the kind of state
 * that looks fine while being untrue.
 */
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
  /** From the `start` message. The server's answer, not the client's request. */
  start: StartMessage | null;
  ticks: TickMessage[];
  latestDecision: DecisionMessage | null;
  /** Counted so a user-initiated stop can report real totals, not the
      server's `end` totals (which never arrive in that case). */
  decisionCount: number;
  /** Set on `end`. Carries the server's own totals for cross-checking. */
  end: { total_ticks: number; total_decisions: number; reason: string } | null;
  error: { detail: string; code: string | null } | null;
  /** Set when the server reports no session is live. Not an error. */
  noLiveSession: NoLiveSessionMessage | null;
  /** Which reconnect attempt is in flight (0 when not reconnecting). */
  reconnectAttempt: number;
  /** Delay before the in-flight reconnect attempt, for an honest countdown. */
  reconnectDelayMs: number;
  /**
   * Count of decisions that arrived for a lap with no matching tick.
   *
   * DAY3.md flags panel/timeline disagreement as a real ordering bug rather
   * than a display quirk. Rather than eyeballing it, the invariant is
   * checked on every decision and any breach is counted and surfaced in the
   * UI. A silent assumption that "message order is fine because nothing
   * crashed" is precisely what this is here to replace.
   */
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
 * One reducer holding every piece of stream state.
 *
 * Why a reducer rather than several `useState` calls: a single frame can
 * imply several coordinated changes (a `start` sets status AND source AND
 * total laps; an `end` sets status AND totals). Splitting those across
 * independent setters spreads one transition over several places and makes
 * "which of these is allowed to happen without the others" an unwritten
 * rule. Here every transition is one pure function, readable top to bottom.
 *
 * It also gives the ordering guarantee for free. `ticks` and `latestDecision`
 * live in ONE state object, so no render can ever observe a decision without
 * the ticks that preceded it — they are literally the same value. Two
 * separate `useState` atoms could not promise that as a property of the
 * design; it would merely happen to be true.
 */
function reducer(state: RaceStreamState, action: Action): RaceStreamState {
  switch (action.kind) {
    case "connect_requested":
      // A fresh connection starts from a clean slate. Leaving the previous
      // race's ticks on screen under a new session_key would be a display
      // that lies about what it is showing.
      return { ...INITIAL_STATE, status: "connecting" };

    case "reconnect_scheduled":
      // The drop is now visible as its own state rather than as a generic
      // error. DAY4.md wants `reconnecting` distinguishable from the initial
      // `connecting`: one means "we have not started", the other means "we
      // were streaming and lost it", and they need different copy.
      return {
        ...state,
        status: "reconnecting",
        reconnectAttempt: action.attempt,
        reconnectDelayMs: action.delayMs,
      };

    case "reconnect_attempt":
      // Ticks are cleared because the server restarts the stream from the
      // beginning on a new connection — keeping the old ones would produce
      // duplicates and break the timeline/decision ordering invariant.
      return {
        ...INITIAL_STATE,
        status: "connecting",
        reconnectAttempt: state.reconnectAttempt,
      };

    case "socket_open":
      // Deliberately NOT "streaming" yet. The socket being open only means
      // the handshake succeeded; the server may still answer with an `error`
      // envelope (unknown session_key, live mode unavailable). "streaming"
      // is claimed when the server actually says it has started.
      return state;

    case "message": {
      const message = action.message;
      switch (message.type) {
        case "start":
          return { ...state, status: "streaming", start: message };

        case "tick":
          return { ...state, ticks: [...state.ticks, message] };

        case "decision": {
          // The invariant, checked rather than assumed.
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
          // Expected, not a failure: live mode spends most of its life here.
          // Given its own status so the UI can present it as information
          // rather than as a red error banner (SPEC section 10).
          return { ...state, status: "no_live_session", noLiveSession: message };

        case "error":
          // An `error` envelope is the server answering in-protocol, not the
          // transport failing. It is reported as such, with the server's own
          // detail text rather than a generic message of our own.
          return {
            ...state,
            status: "error",
            error: { detail: message.detail, code: message.code },
          };
      }
      return state;
    }

    case "user_disconnected":
      // Pressing Disconnect must change what the UI says. Leaving the banner
      // on "Streaming" after the socket is closed is the exact failure mode
      // DAY3.md rules out: a state that looks fine while being untrue.
      //
      // This is `ended`, not `error` — the user asked for it, nothing broke.
      // The reason string keeps it distinguishable from a race that actually
      // finished, so the two never read alike.
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
      // Order matters. The server sends `end` and then closes, so a close
      // arriving after `ended` (or after `error`, or `no_live_session`) is
      // the normal path and must not be rewritten. A close while still
      // `streaming` is a genuine drop.
      //
      // As of Day 4 the hook schedules a reconnect on a drop, so this only
      // produces a terminal error once the retries are exhausted — see
      // `retries_exhausted`.
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
 * Where the backend lives, as a WebSocket origin.
 *
 * `NEXT_PUBLIC_BACKEND_WS_URL` may be given in whichever form is natural —
 * `https://…`, `http://…`, `wss://…`, `ws://…`, or a bare host. Accepting
 * all of them and normalising here is deliberate: the value gets typed into
 * a Vercel dashboard box, and the obvious thing to paste is the backend's
 * `https://` URL. Requiring `wss://` would make the natural input silently
 * wrong.
 *
 * THE SCHEME MATTERS (flagged on Day 4). A page served over HTTPS cannot
 * open a plain `ws://` connection — browsers block it as mixed content, with
 * a console error and no callback. Production must be `wss://`, and that is
 * derived from the configured origin rather than hardcoded, so the same
 * build works on http://localhost and on https://…vercel.app.
 */
function toWebSocketOrigin(raw: string, pageIsSecure: boolean): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (trimmed.startsWith("wss://") || trimmed.startsWith("ws://")) return trimmed;
  if (trimmed.startsWith("https://")) return `wss://${trimmed.slice(8)}`;
  if (trimmed.startsWith("http://")) return `ws://${trimmed.slice(7)}`;
  // A bare host takes the page's own security level: an HTTPS page must not
  // downgrade to ws://, and a local HTTP page cannot use wss:// without TLS.
  return `${pageIsSecure ? "wss" : "ws"}://${trimmed}`;
}

export class BackendNotConfiguredError extends Error {}

/**
 * Resolve the backend origin, or explain precisely what is missing.
 *
 * Falling back to localhost when the variable is unset would be actively
 * unhelpful in production: the deployed page would try to reach the
 * viewer's own machine and fail with a generic connection error that says
 * nothing about the actual cause. So the fallback applies only when the page
 * is itself served from localhost.
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

export function buildStreamUrl(
  sessionKey: string,
  mode: Mode,
  backend?: string,
): string {
  // `mode` is always passed through, never hardcoded to "replay" — the
  // connection layer stayed mode-generic from Day 3, which is why enabling
  // live mode on Day 4 needed no change here.
  const base = (backend ?? resolveBackendOrigin()).replace(/\/$/, "");
  return `${base}/ws/race/${encodeURIComponent(sessionKey)}?mode=${mode}`;
}

export function useRaceStream() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const socketRef = useRef<WebSocket | null>(null);
  const targetRef = useRef<{ sessionKey: string; mode: Mode } | null>(null);
  const attemptRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const shouldReconnectRef = useRef(false);
  const scheduleReconnectRef = useRef<() => void>(() => {});

  /**
   * Close the socket without touching state.
   *
   * Handlers are detached before closing so our own teardown cannot fire
   * `socket_closed` and rewrite a finished stream into an error. Used where
   * the state change is somebody else's job: starting a new connection
   * (which resets anyway) and unmounting (where there is no UI left to
   * update).
   */
  const teardown = useCallback(() => {
    shouldReconnectRef.current = false;
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      socket.close();
    }
  }, []);

  /** The user pressed Disconnect: close, and say so. */
  const disconnect = useCallback(() => {
    // Stop reconnecting first: the user asking to stop must not be answered
    // by a retry a second later.
    targetRef.current = null;
    attemptRef.current = 0;
    teardown();
    dispatch({ kind: "user_disconnected" });
  }, [teardown]);

  /**
   * Open a socket and attach handlers. Shared by the first connection and by
   * every reconnect attempt, so both take exactly the same code path — a
   * reconnect that differs from a connect is a reconnect that is not tested
   * by connecting.
   */
  const openSocket = useCallback(
    (sessionKey: string, mode: Mode) => {
      let socket: WebSocket;
      try {
        socket = new WebSocket(buildStreamUrl(sessionKey, mode));
      } catch (cause) {
        // A missing NEXT_PUBLIC_BACKEND_WS_URL lands here. Reported with its
        // own message so a deployment misconfiguration reads as exactly that
        // rather than as a mysterious connection failure.
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
      // Armed here, cleared the moment the server gives a final answer. Set
      // imperatively rather than derived from `state.status`, because
      // `onclose` can fire before React has re-rendered — a ref updated
      // during render would then still say "streaming" and trigger a
      // reconnect after a perfectly clean finish.
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
        // Without this, a stream that drops once an hour would eventually be
        // treated as though it had failed six times in a row.
        if (parsed.type === "start") attemptRef.current = 0;
        // Final answers. Retrying any of these would just ask the backend
        // the same question again and get the same reply.
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
        // Note: no reconnect suppression here. `onerror` is always followed
        // by `onclose`, and a transport error on a live stream is exactly
        // the case worth retrying.
        dispatch({
          kind: "transport_error",
          detail:
            "WebSocket transport error — the connection could not be " +
            `established to ${resolveBackendOrigin()}. If you are running ` +
            "locally, start the backend with: uv run uvicorn backend.main:app",
        });

      socket.onclose = () => {
        socketRef.current = null;
        // Reconnect ONLY on an unexpected drop. A clean `end`, a server
        // `error`, and `no_live_session` are all final answers — retrying
        // them would hammer the backend to be told the same thing again.
        if (shouldReconnectRef.current) {
          scheduleReconnectRef.current();
        } else {
          dispatch({ kind: "socket_closed" });
        }
      };
    },
    [],
  );

  /**
   * Schedule the next reconnect attempt.
   *
   * `openSocket` and this function are mutually recursive — a closed socket
   * schedules a retry, and the retry opens a socket — so one of them has to
   * be reached indirectly. `openSocket` calls this through a ref, which is
   * kept in sync in an effect below rather than assigned during render:
   * mutating a ref while rendering can desync it from what a concurrent
   * render observes, which is why React's lint rules forbid it.
   */
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
      openSocket(target.sessionKey, target.mode);
    }, delay);
  }, [openSocket]);

  useEffect(() => {
    scheduleReconnectRef.current = scheduleReconnect;
  }, [scheduleReconnect]);

  const connect = useCallback(
    (sessionKey: string, mode: Mode) => {
      teardown();
      targetRef.current = { sessionKey, mode };
      attemptRef.current = 0;
      dispatch({ kind: "connect_requested" });
      openSocket(sessionKey, mode);
    },
    [teardown, openSocket],
  );

  // Close the socket if the component unmounts mid-stream, so a navigation
  // away does not leave the server pacing ticks into a dead connection.
  // `teardown`, not `disconnect`: dispatching into an unmounted component
  // would be a no-op at best and a warning at worst.
  useEffect(() => teardown, [teardown]);

  const reset = useCallback(() => {
    teardown();
    dispatch({ kind: "reset" });
  }, [teardown]);

  /**
   * The highest lap number seen in the timeline. Exposed so the UI can state
   * the timeline/panel relationship explicitly rather than implying it.
   */
  const latestTickLap = useMemo(
    () => state.ticks.at(-1)?.lap ?? null,
    [state.ticks],
  );

  return { state, connect, disconnect, reset, latestTickLap };
}
