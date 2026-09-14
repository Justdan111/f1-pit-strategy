/**
 * The WebSocket message protocol, mirrored from the backend.
 *
 * Every name here matches `backend/src/backend/models.py` exactly — the
 * interface names (`StartMessage`, `TickMessage`, ...) and every field name.
 * DAY3.md asks for this deliberately: with identical names there is no
 * translation layer to keep in sync by hand, and a backend rename shows up
 * here as a compile error rather than as `undefined` rendering silently as
 * an empty cell.
 *
 * Field names are NOT camelCased. Converting `lap_duration_s` to
 * `lapDurationS` would look more idiomatic and would be a mistake: it adds a
 * mapping layer whose only job is to be kept correct forever, and the first
 * time it is wrong the UI shows a blank instead of an error.
 */

/** What the server actually did — not what the client asked for. */
export type SourceKind = "sample" | "historical_replay" | "live";

export type Verdict = "pit_now" | "stay_out";

export interface StartMessage {
  type: "start";
  session_key: string;
  source: SourceKind;
  /** null in live mode: a race in progress has no known total yet. */
  total_laps: number | null;
  driver_number: number | null;
}

export interface TickMessage {
  type: "tick";
  lap: number;
  driver_number: number;
  compound: string;
  tyre_age: number;
  stint_number: number;
  /** null when the lap was never completed — render as "—", never as 0. */
  lap_duration_s: number | null;
  is_pit_out_lap: boolean;
}

export interface DecisionMessage {
  type: "decision";
  lap: number;
  driver_number: number;
  compound: string;
  tyre_age: number;
  verdict: Verdict;

  current_compound_degradation_s_per_lap: number;
  fit_intercept_s: number;
  fit_r_squared: number;
  samples_used: number;
  samples_seen: number;

  projected_time_current_tyres_s: number;
  projected_time_fresh_tyres_s: number;
  pit_lane_cost_s: number;
  delta_s: number;

  fresh_tyre_advantage_s_per_lap: number;
  /** null when degradation is not measurably positive. */
  laps_to_break_even: number | null;
  degradation_is_measurable: boolean;
  note: string | null;
}

export interface NoLiveSessionMessage {
  type: "no_live_session";
  detail: string;
  /** ISO-8601. When the server checked, so a stale page cannot look current. */
  checked_at: string;
  next_session_key: number | null;
  next_session_name: string | null;
  /** ISO-8601, or null when nothing is scheduled. */
  next_session_start: string | null;
}

export interface EndMessage {
  type: "end";
  session_key: string;
  total_ticks: number;
  total_decisions: number;
  reason: string;
}

export interface ErrorMessage {
  type: "error";
  detail: string;
  code: string | null;
}

/**
 * Discriminated union on `type`, exactly as the backend's `StreamMessage`.
 * TypeScript narrows a `switch (message.type)` automatically, so each branch
 * gets the right fields with no casting.
 */
export type StreamMessage =
  | StartMessage
  | TickMessage
  | DecisionMessage
  | NoLiveSessionMessage
  | EndMessage
  | ErrorMessage;

/** Modes the backend's `?mode=` query parameter accepts. */
export type Mode = "replay" | "live";

/**
 * Runtime guard for one parsed frame.
 *
 * The socket delivers strings. `JSON.parse` returns `any`, and asserting
 * `as StreamMessage` over it would be a lie that TypeScript happily believes
 * — the compiler cannot check data that arrives at runtime. This narrows
 * honestly, so an unrecognised frame is surfaced instead of crashing a
 * renderer three components away from the actual cause.
 */
export function isStreamMessage(value: unknown): value is StreamMessage {
  if (typeof value !== "object" || value === null) return false;
  const type = (value as { type?: unknown }).type;
  return (
    type === "start" ||
    type === "tick" ||
    type === "decision" ||
    type === "no_live_session" ||
    type === "end" ||
    type === "error"
  );
}
