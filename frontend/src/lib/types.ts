/**
 * The WebSocket message protocol, mirrored from backend/models.py.
 *
 * Names match the backend exactly, including snake_case fields, so there is
 * no mapping layer to keep in sync and a backend rename surfaces as a
 * compile error rather than as an empty cell.
 */

/** What the server actually did, not what the client asked for. */
export type SourceKind = "sample" | "historical_replay" | "live";

export type Verdict = "pit_now" | "stay_out";

/** Whether the fitted slope is distinguishable from zero at 95% confidence. */
export type DegradationSignificance = "positive" | "unclear" | "negative";

export type Mode = "replay" | "live";

export interface StartMessage {
  type: "start";
  session_key: string;
  source: SourceKind;
  /** null in live mode: a race in progress has no known total. */
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
  /** null when the lap was never completed. Render as a dash, never as 0. */
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
  /** Which question the verdict answered. `next_lap_only` is not actionable. */
  verdict_basis: "race_remaining" | "next_lap_only";
  /** Laps left in the race. null when the distance is unknown. */
  laps_remaining: number | null;
  /** Seconds saved over the rest of the race by stopping now. */
  net_gain_s: number | null;

  /** Tyre degradation with fuel burn removed. */
  current_compound_degradation_s_per_lap: number;
  /** The uncorrected slope: degradation minus fuel effect. */
  raw_degradation_s_per_lap: number;
  /** Seconds per lap added back. Zero when correction is disabled. */
  fuel_correction_s_per_lap: number;
  fit_intercept_s: number;
  fit_r_squared: number;
  samples_used: number;
  samples_seen: number;

  slope_std_error_s_per_lap: number;
  slope_ci_low_s_per_lap: number;
  slope_ci_high_s_per_lap: number;
  degradation_significance: DegradationSignificance;

  projected_time_current_tyres_s: number;
  projected_time_fresh_tyres_s: number;
  pit_lane_cost_s: number;
  delta_s: number;

  fresh_tyre_advantage_s_per_lap: number;
  /** null when degradation is not measurably positive. */
  laps_to_break_even: number | null;
  /** The same figure at the ends of the slope's interval. `high` is null when
   *  the interval reaches zero: the payback period is then unbounded. */
  laps_to_break_even_low: number | null;
  laps_to_break_even_high: number | null;
  degradation_is_measurable: boolean;
  note: string | null;
}

export interface NoLiveSessionMessage {
  type: "no_live_session";
  detail: string;
  checked_at: string;
  next_session_key: number | null;
  next_session_name: string | null;
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

export type StreamMessage =
  | StartMessage
  | TickMessage
  | DecisionMessage
  | NoLiveSessionMessage
  | EndMessage
  | ErrorMessage;

/** Runtime guard: the compiler cannot check data that arrives at runtime. */
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
