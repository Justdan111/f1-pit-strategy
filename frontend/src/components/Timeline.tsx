import { TickMessage } from "@/lib/types";

/**
 * The running list of ticks, newest first.
 *
 * Newest-first rather than append-to-bottom: the interesting lap is always
 * the one that just arrived, and this way it is at a fixed position instead
 * of scrolling away. The list is NOT truncated — trimming to the last N rows
 * would mean the decision panel could reference a lap no longer visible,
 * which is the exact timeline/panel disagreement DAY3.md warns about.
 *
 * `lap_duration_s` is nullable upstream, so a missing value renders as an
 * explicit dash. Rendering it as 0.000 would be inventing a lap time.
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
