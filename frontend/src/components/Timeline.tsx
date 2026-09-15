import { TickMessage } from "@/lib/types";

/**
 * Ticks, newest first. Never truncated: trimming would let the decision panel
 * reference a lap no longer visible. A null lap time renders as a dash.
 */
export function Timeline({
  ticks,
  highlightLap,
}: {
  ticks: TickMessage[];
  highlightLap: number | null;
}) {
  if (ticks.length === 0) {
    return (
      <div className="empty">
        No laps yet. Ticks appear here as the server sends them.
      </div>
    );
  }

  const rows = [...ticks].reverse();

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>lap</th>
            <th>compound</th>
            <th>tyre age</th>
            <th>lap time</th>
            <th>stint</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((tick) => (
            <tr
              key={tick.lap}
              className={tick.lap === highlightLap ? "row-current" : undefined}
            >
              <td className="num">{tick.lap}</td>
              <td>
                <span className={`compound compound-${tick.compound}`}>
                  {tick.compound}
                </span>
                {tick.is_pit_out_lap && (
                  <span className="tag" title="Out-lap: excluded from the degradation fit">
                    out-lap
                  </span>
                )}
              </td>
              <td className="num">{tick.tyre_age}</td>
              <td className="num">
                {tick.lap_duration_s === null
                  ? "—"
                  : tick.lap_duration_s.toFixed(3)}
              </td>
              <td className="num">{tick.stint_number}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
