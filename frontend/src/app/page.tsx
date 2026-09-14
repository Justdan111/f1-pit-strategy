"use client";

import { ConnectForm } from "@/components/ConnectForm";
import { DecisionPanel } from "@/components/DecisionPanel";
import { SourceBadge } from "@/components/SourceBadge";
import { StatusBanner } from "@/components/StatusBanner";
import { Timeline } from "@/components/Timeline";
import { useRaceStream } from "@/lib/useRaceStream";

/**
 * The single page. DAY3.md: one live view, no landing page, no nav.
 *
 * All stream state lives in one `useRaceStream` hook and is passed down as
 * props. No context, no store library — with exactly one consumer tree and
 * one source of truth, either would be indirection with nothing to gain, and
 * both would make the ordering guarantee harder to reason about rather than
 * easier.
 */
export default function Page() {
  const { state, connect, disconnect, latestTickLap } = useRaceStream();
  const busy = state.status === "connecting" || state.status === "streaming";

  return (
    <main>
      <header className="page-head">
        <h1>F1 Pit Strategy</h1>
        {/* The badge only appears once the server has told us what it is
            actually doing. Rendering it from the form's selection before
            `start` arrives would show a claim we cannot yet support. */}
        {state.start && <SourceBadge source={state.start.source} />}
      </header>

      <ConnectForm onConnect={connect} onDisconnect={disconnect} busy={busy} />

      <StatusBanner state={state} />

      {/* The ordering invariant, surfaced. If this ever renders, the panel
          and the timeline disagree and that is a real bug, not a display
          quirk (DAY3.md). Silence here is the check passing. */}
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
