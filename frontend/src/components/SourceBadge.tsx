import { SourceKind } from "@/lib/types";

/**
 * Shows what the server reported in `start.source` — literally.
 *
 * DAY3.md is specific about this: the badge reflects what the backend
 * actually did, not what the connect form requested. Those are different
 * facts and they could diverge (a future backend might silently fall back
 * from live to replay). Rendering the request instead of the answer would be
 * a dashboard that quietly lies about whether you are watching a real race.
 *
 * The raw value is always shown alongside the human label for the same
 * reason — a reader can check the badge against the raw `start` frame.
 */
const LABELS: Record<SourceKind, { label: string; hint: string }> = {
  sample: {
    label: "SAMPLE",
    hint: "Offline fixture — synthetic data, no network",
  },
  historical_replay: {
    label: "REPLAY",
    hint: "A finished race, paced out to feel live",
  },
  live: {
    label: "LIVE",
    hint: "A session happening right now",
  },
};

export function SourceBadge({ source }: { source: SourceKind }) {
  const { label, hint } = LABELS[source];
  return (
    <span className={`badge badge-${source}`} title={hint}>
      <strong>{label}</strong>
      <code>{source}</code>
    </span>
  );
}
