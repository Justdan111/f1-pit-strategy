import { SourceKind } from "@/lib/types";

/** Shows `start.source` literally: what the server did, not what was requested. */
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
