"use client";

import { useState } from "react";
import { Mode } from "@/lib/types";

/**
 * session_key text input + mode selector + Connect/Disconnect.
 *
 * A plain text input, per DAY3.md — a race browser against /v1/sessions is
 * explicitly out of scope.
 *
 * The `live` option exists but is disabled and labelled. DAY3.md is direct
 * about why: LiveTickSource is not built until Day 4, and an enabled toggle
 * would promise something the backend cannot do. Disabled-and-explained is
 * honest; hidden would be too, but it would also hide that live mode is the
 * point of the whole project.
 */
export function ConnectForm({
  onConnect,
  onDisconnect,
  busy,
}: {
  onConnect: (sessionKey: string, mode: Mode) => void;
  onDisconnect: () => void;
  busy: boolean;
}) {
  const [sessionKey, setSessionKey] = useState("sample");
  const [mode, setMode] = useState<Mode>("replay");

  return (
    <form
      className="connect"
      onSubmit={(event) => {
        event.preventDefault();
        const key = sessionKey.trim();
        if (key) onConnect(key, mode);
      }}
    >
      <label>
        <span>session_key</span>
        <input
          value={sessionKey}
          onChange={(event) => setSessionKey(event.target.value)}
          placeholder="sample, or a numeric key like 9904"
          spellCheck={false}
          autoComplete="off"
        />
      </label>

      <label>
        <span>mode</span>
        <select
          value={mode}
          onChange={(event) => setMode(event.target.value as Mode)}
        >
          <option value="replay">replay</option>
          <option value="live" disabled>
            live — not yet available (Day 4)
          </option>
        </select>
      </label>

      <div className="connect-actions">
        <button type="submit" disabled={!sessionKey.trim()}>
          Connect
        </button>
        <button type="button" onClick={onDisconnect} disabled={!busy}>
          Disconnect
        </button>
      </div>

      <p className="hint">
        <code>sample</code> streams the offline fixture. A numeric key such as{" "}
        <code>9904</code> replays a finished race from OpenF1.
      </p>
    </form>
  );
}
