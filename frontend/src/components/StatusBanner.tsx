import { ConnectionStatus, RaceStreamState } from "@/lib/useRaceStream";

/** One distinct, non-blank state per connection status. */
const COPY: Record<ConnectionStatus, { title: string; tone: string }> = {
  idle: { title: "Not connected", tone: "idle" },
  connecting: { title: "Connecting…", tone: "connecting" },
  // "Connecting" means nothing started; "Reconnecting" means we lost a stream.
  reconnecting: { title: "Reconnecting…", tone: "reconnecting" },
  streaming: { title: "Streaming", tone: "streaming" },
  ended: { title: "Stream ended", tone: "ended" },
  // Informational, not a failure: live mode is usually in this state.
  no_live_session: { title: "No live session", tone: "info" },
  error: { title: "Error", tone: "error" },
};

function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

export function StatusBanner({ state }: { state: RaceStreamState }) {
  const { title, tone } = COPY[state.status];

  return (
    <div className={`banner banner-${tone}`} role="status" aria-live="polite">
      <div className="banner-head">
        <span className="dot" aria-hidden />
        <strong>{title}</strong>
      </div>

      {state.status === "idle" && (
        <p>Enter a session key and press Connect. Try <code>sample</code>.</p>
      )}

      {state.status === "connecting" && (
        <p>
          Opening the WebSocket. The server has not confirmed the stream yet —
          it may still reject the request.
        </p>
      )}

      {state.status === "reconnecting" && (
        <p>
          The connection dropped. Retrying (attempt {state.reconnectAttempt} of{" "}
          6) in {Math.round(state.reconnectDelayMs / 1000)}s. The stream will
          restart from the beginning of the available data.
        </p>
      )}

      {state.status === "no_live_session" && state.noLiveSession && (
        <>
          <p>{state.noLiveSession.detail}</p>
          {state.noLiveSession.next_session_start && (
            <p>
              <strong>Next session:</strong>{" "}
              {state.noLiveSession.next_session_name ?? "unknown"} —{" "}
              {formatWhen(state.noLiveSession.next_session_start)}
            </p>
          )}
          <p className="muted">
            Checked at {formatWhen(state.noLiveSession.checked_at)}.
          </p>
        </>
      )}

      {state.status === "streaming" && state.start && (
        <p>
          Session <code>{state.start.session_key}</code>
          {state.start.driver_number !== null && (
            <> · car #{state.start.driver_number}</>
          )}
          {" · "}
          {state.start.total_laps === null
            ? "total laps unknown (live)"
            : `${state.start.total_laps} laps`}
        </p>
      )}

      {/* Finishing and being stopped are both `ended`, but must not read alike. */}
      {state.status === "ended" && state.end && (
        <p>
          {state.end.reason === "completed"
            ? "Finished normally."
            : `Stopped: ${state.end.reason}.`}{" "}
          {state.end.total_ticks} tick{state.end.total_ticks === 1 ? "" : "s"},{" "}
          {state.end.total_decisions} decision
          {state.end.total_decisions === 1 ? "" : "s"}.
        </p>
      )}

      {state.status === "error" && state.error && (
        <p>
          {state.error.detail}
          {state.error.code && (
            <>
              {" "}
              <code>{state.error.code}</code>
            </>
          )}
        </p>
      )}
    </div>
  );
}
