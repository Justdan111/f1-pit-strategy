import { DecisionMessage } from "@/lib/types";

/**
 * The latest `decision` message, in full.
 *
 * Two things this panel refuses to do:
 *
 * 1. Render a blank/skeleton panel before the first decision. Day 2's engine
 *    needs at least three clean samples of a compound before it can fit
 *    anything, so "no decision yet" is a normal, expected state with a real
 *    reason — and DAY3.md asks for it to be stated, not left looking broken.
 *
 * 2. Show the verdict alone. SPEC section 10 is "explainability over
 *    accuracy": every number behind the call is displayed so a reader can
 *    recompute it and disagree. A verdict with no arithmetic is exactly the
 *    thing that looks authoritative while being unverifiable.
 */

function Row({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="metric" title={hint}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function DecisionPanel({
  decision,
  latestTickLap,
  streaming,
}: {
  decision: DecisionMessage | null;
  latestTickLap: number | null;
  streaming: boolean;
}) {
  if (!decision) {
    return (
      <div className="panel panel-waiting">
        <h2>Decision</h2>
        <p className="waiting-title">
          {streaming ? "Gathering data…" : "No decision yet"}
        </p>
        <p>
          The engine fits a degradation curve per compound and needs at least{" "}
          <strong>3 clean laps</strong> of the current compound before it can
          say anything. Out-laps and anomalous laps (safety car, standing
          start) are excluded from that count, so the first decision of a
          stint usually arrives on its third or fourth lap.
        </p>
        {latestTickLap !== null && (
          <p className="muted">Latest lap received: {latestTickLap}.</p>
        )}
      </div>
    );
  }

  const pit = decision.verdict === "pit_now";
  const sign = (n: number) => (n > 0 ? `+${n.toFixed(3)}` : n.toFixed(3));

  return (
    <div className={`panel panel-${decision.verdict}`}>
      <h2>Decision</h2>

      <div className="verdict">
        <span className="verdict-word">
          {pit ? "PIT NOW" : "STAY OUT"}
        </span>
        <span className="verdict-ctx">
          lap {decision.lap} · {decision.compound} · tyre age{" "}
          {decision.tyre_age}
        </span>
      </div>

      {/* The one-lap comparison DAY2.md specifies. */}
      <h3>One-lap comparison</h3>
      <dl className="metrics">
        <Row
          label="stay out (next lap)"
          value={`${decision.projected_time_current_tyres_s.toFixed(3)} s`}
          hint="Projected next-lap time on the current set, one lap older."
        />
        <Row
          label="fresh set (next lap)"
          value={`${decision.projected_time_fresh_tyres_s.toFixed(3)} s`}
          hint="Curve read at tyre age 1, not 0 — lap one on a new set is not its best."
        />
        <Row
          label="pit lane cost"
          value={`${decision.pit_lane_cost_s.toFixed(1)} s`}
          hint="Fixed constant. Circuit-dependent in reality; a documented simplification."
        />
        <Row
          label="delta"
          value={`${sign(decision.delta_s)} s`}
          hint="Time saved by pitting over the next lap. Positive means pit_now."
        />
      </dl>

      {/* The numbers the call actually turns on. */}
      <h3>What the call turns on</h3>
      <dl className="metrics">
        <Row
          label="degradation"
          value={`${sign(decision.current_compound_degradation_s_per_lap)} s/lap`}
          hint="Fitted slope of lap time against tyre age for this compound."
        />
        <Row
          label="fresh-tyre advantage"
          value={`${sign(decision.fresh_tyre_advantage_s_per_lap)} s/lap`}
          hint="slope x current tyre age — constant on every future lap."
        />
        <Row
          label="laps to break even"
          value={
            decision.laps_to_break_even === null
              ? "—"
              : `${decision.laps_to_break_even.toFixed(2)}`
          }
          hint="pit cost / advantage. The number a strategist actually acts on."
        />
      </dl>

      {!decision.degradation_is_measurable && (
        <p className="warn">
          <strong>No measurable degradation.</strong> The fitted slope is not
          positive, so there is no break-even point to report — the verdict
          below is still computed, but do not read it as a confident
          recommendation.
        </p>
      )}

      {decision.note && <p className="note">{decision.note}</p>}

      <h3>Fit quality</h3>
      <dl className="metrics">
        <Row label="r²" value={decision.fit_r_squared.toFixed(4)} />
        <Row label="intercept" value={`${decision.fit_intercept_s.toFixed(3)} s`} />
        <Row
          label="samples"
          value={`${decision.samples_used} used / ${decision.samples_seen} seen`}
          hint="They differ because contaminated laps are excluded from the fit."
        />
      </dl>

      {latestTickLap !== null && decision.lap !== latestTickLap && (
        <p className="muted">
          Showing the decision for lap {decision.lap}; latest lap received is{" "}
          {latestTickLap}. (Normal at the start of a stint, when the engine has
          too few samples of the new compound to decide yet.)
        </p>
      )}
    </div>
  );
}
