import { DecisionMessage, DegradationSignificance } from "@/lib/types";

/** How much to trust the fitted slope, and why. */
const SIGNIFICANCE: Record<
  DegradationSignificance,
  { label: string; hint: string }
> = {
  positive: {
    label: "measured",
    hint: "The slope's 95% interval is entirely above zero: the tyre is confidently degrading.",
  },
  unclear: {
    label: "not conclusive",
    hint: "The slope's 95% interval spans zero. Too few or too noisy samples to tell degradation from none — which is not the same as no degradation.",
  },
  negative: {
    label: "getting faster",
    hint: "The slope's 95% interval is entirely below zero. Usually fuel burn outweighing tyre wear; this engine does not correct for it.",
  },
};

/**
 * The latest decision in full. Never renders a blank panel before the first
 * one, and never shows the verdict without the numbers behind it.
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
        <span
          className={`signif signif-${decision.degradation_significance}`}
          title={SIGNIFICANCE[decision.degradation_significance].hint}
        >
          {SIGNIFICANCE[decision.degradation_significance].label}
        </span>
      </div>

      {/* The comparison the verdict is actually based on. */}
      <h3>Over the rest of the race</h3>
      <dl className="metrics">
        <Row
          label="laps remaining"
          value={
            decision.laps_remaining === null
              ? "unknown"
              : `${decision.laps_remaining}`
          }
          hint="Race distance minus the current lap. Unknown in live mode unless supplied, because OpenF1 reports no lap count for a session in progress."
        />
        <Row
          label="net gain if you pit now"
          value={
            decision.net_gain_s === null
              ? "—"
              : `${sign(decision.net_gain_s)} s`
          }
          hint="advantage per lap x laps remaining - pit cost. Positive means stopping now saves time over the rest of the race. This is what the verdict is based on."
        />
      </dl>

      {decision.verdict_basis === "next_lap_only" && (
        <p className="warn">
          <strong>Verdict not actionable.</strong> The race distance is unknown,
          so this compares only the <em>next lap</em> — and a pit stop can never
          be repaid in one lap, so that comparison can almost never favour
          stopping. Read <strong>laps to break even</strong> below, or pass{" "}
          <code>?total_laps=</code>.
        </p>
      )}

      {/* Kept for transparency: this is the one-lap view, which is why a
          verdict based on it alone was useless. */}
      <h3>The next lap alone</h3>
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
          label="delta (one lap)"
          value={`${sign(decision.delta_s)} s`}
          hint="Time saved by pitting over the NEXT LAP only. Essentially always negative: a 22s stop cannot be repaid in one lap. Shown for transparency; the verdict does not use it when the race distance is known."
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
        {decision.fuel_correction_s_per_lap > 0 && (
          <Row
            label="before fuel correction"
            value={`${sign(decision.raw_degradation_s_per_lap)} s/lap`}
            hint={`Raw slope of lap time against tyre age, which measures degradation minus fuel burn. ${decision.fuel_correction_s_per_lap.toFixed(4)}s/lap was added back to remove the fuel effect. Shown so the adjustment is auditable rather than invisible.`}
          />
        )}
        <Row
          label="95% interval"
          value={`${sign(decision.slope_ci_low_s_per_lap)} … ${sign(
            decision.slope_ci_high_s_per_lap,
          )}`}
          hint={SIGNIFICANCE[decision.degradation_significance].hint}
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
        <Row
          label="break-even range"
          value={
            decision.laps_to_break_even_low === null
              ? "—"
              : `${decision.laps_to_break_even_low.toFixed(1)} … ${
                  decision.laps_to_break_even_high === null
                    ? "∞"
                    : decision.laps_to_break_even_high.toFixed(1)
                }`
          }
          hint="The payback period at the ends of the slope's interval. Unbounded when the interval reaches zero: a tyre that might not be slowing might never repay a stop."
        />
      </dl>

      {/* One warning, driven by the confidence interval. "Cannot tell" and
          "confidently not degrading" are different findings and must not
          share a message. */}
      {decision.degradation_significance === "unclear" && (
        <p className="warn">
          <strong>Not statistically conclusive.</strong> The slope&apos;s 95%
          interval spans zero on {decision.samples_used} samples, so the data
          cannot yet distinguish degradation from none. That is not the same
          as the tyre not degrading
          {decision.laps_to_break_even !== null
            ? " — treat the break-even figure as provisional."
            : "."}
        </p>
      )}

      {decision.degradation_significance === "negative" && (
        <p className="warn">
          <strong>Lap times are falling, confidently.</strong> The slope&apos;s
          95% interval sits entirely below zero, so this is a real effect
          rather than noise — usually fuel burn outweighing tyre wear as the
          car lightens. This engine does not correct for fuel, so there is no
          break-even point to report.
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
