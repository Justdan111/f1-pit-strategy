"use client";

import { ConnectForm } from "@/components/ConnectForm";
import { DecisionPanel } from "@/components/DecisionPanel";
import { SourceBadge } from "@/components/SourceBadge";
import { StatusBanner } from "@/components/StatusBanner";
import { Timeline } from "@/components/Timeline";
import { useRaceStream } from "@/lib/useRaceStream";

/** One live view. State lives in one hook and is passed down as props. */
export default function Page() {
  const { state, connect, disconnect, latestTickLap } = useRaceStream();
  const busy = state.status === "connecting" || state.status === "streaming";

  return (
    <main>
      <header className="page-head">
        <h1>F1 Pit Strategy</h1>
        {/* Only once the server has said what it is actually doing. */}
        {state.start && <SourceBadge source={state.start.source} />}
      </header>

      <ConnectForm onConnect={connect} onDisconnect={disconnect} busy={busy} />

      <StatusBanner state={state} />

      {/* If this ever renders, the panel and timeline disagree: a real bug. */}
      {state.orderingViolations > 0 && (
        <div className="banner banner-error">
          <div className="banner-head">
            <span className="dot" aria-hidden />
            <strong>Message ordering violation</strong>
          </div>
          <p>
            {state.orderingViolations} decision
            {state.orderingViolations === 1 ? "" : "s"} arrived for a lap with
            no matching tick. The decision panel and the timeline are out of
            sync — see the browser console for the offending laps.
          </p>
        </div>
      )}

      <div className="columns">
        <section>
          <h2>
            Timeline{" "}
            <span className="count">
              {state.ticks.length}
              {state.start?.total_laps != null && ` / ${state.start.total_laps}`}
            </span>
          </h2>
          <Timeline ticks={state.ticks} highlightLap={latestTickLap} />
        </section>

        <section>
          <DecisionPanel
            decision={state.latestDecision}
            latestTickLap={latestTickLap}
            streaming={state.status === "streaming"}
          />
        </section>
      </div>
    </main>
  );
}
