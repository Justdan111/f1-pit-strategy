"use client";

import { useCallback, useEffect, useMemo, useRef, useReducer } from "react";
import {
  DecisionMessage,
  Mode,
  StartMessage,
  StreamMessage,
  TickMessage,
  isStreamMessage,
} from "./types";

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
  | "streaming"
  | "ended"
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
  orderingViolations: 0,
};

type Action =
  | { kind: "connect_requested" }
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
      // Order matters here. The server sends `end` and then closes, so a
      // close arriving after `ended` is the normal path and must not be
      // rewritten as an error. Equally, a close while still `streaming` is a
      // genuine drop and must NOT be allowed to look like a clean finish.
      // Reconnecting is Day 4; saying so plainly is today's job.
      if (state.status === "streaming" || state.status === "connecting") {
        return {
          ...state,
          status: "error",
          error: {
            detail:
              "The connection closed before the stream finished. No reconnect " +
              "is attempted yet (planned for Day 4) — press Connect to retry.",
            code: "connection_dropped",
          },
        };
      }
      return state;

    case "reset":
      return INITIAL_STATE;
  }
}

const DEFAULT_BACKEND =
  process.env.NEXT_PUBLIC_BACKEND_WS_URL ?? "ws://127.0.0.1:8000";

export function buildStreamUrl(
  sessionKey: string,
  mode: Mode,
  backend: string = DEFAULT_BACKEND,
): string {
  // `mode` is always passed through, never hardcoded to "replay" — DAY3.md
  // asks for the connection layer to be mode-generic now so that Day 4 is a
  // toggle change rather than a restructuring of this file.
  const base = backend.replace(/\/$/, "");
  return `${base}/ws/race/${encodeURIComponent(sessionKey)}?mode=${mode}`;
}

export function useRaceStream() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const socketRef = useRef<WebSocket | null>(null);

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
    teardown();
    dispatch({ kind: "user_disconnected" });
  }, [teardown]);

  const connect = useCallback(
    (sessionKey: string, mode: Mode) => {
      teardown();
      dispatch({ kind: "connect_requested" });

      let socket: WebSocket;
      try {
        socket = new WebSocket(buildStreamUrl(sessionKey, mode));
      } catch (cause) {
        dispatch({
          kind: "transport_error",
          detail: `Could not open a WebSocket: ${String(cause)}`,
        });
        return;
      }
      socketRef.current = socket;

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
          // Fail loudly rather than ignoring it. An unknown `type` means the
          // frontend and backend protocols have diverged, which is worth
          // knowing immediately instead of discovering as a missing row.
          dispatch({
            kind: "transport_error",
            detail: `Unrecognised message type from server: ${JSON.stringify(parsed).slice(0, 200)}`,
          });
          return;
        }
        dispatch({ kind: "message", message: parsed });
      };

      // `onerror` gives no useful detail by design (the browser withholds it
      // to avoid leaking cross-origin information), so the message here says
      // what is actionable rather than pretending to diagnose.
      socket.onerror = () =>
        dispatch({
          kind: "transport_error",
          detail:
            "WebSocket transport error. Is the backend running at " +
            `${DEFAULT_BACKEND}? Start it with: uv run uvicorn backend.main:app`,
        });

      socket.onclose = () => dispatch({ kind: "socket_closed" });
    },
    [teardown],
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
