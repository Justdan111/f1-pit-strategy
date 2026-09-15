"use client";

import { useState } from "react";
import { Mode } from "@/lib/types";

/** session_key input, mode selector, and connect/disconnect controls. */
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
          <option value="live">live</option>
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
        <strong>replay:</strong> <code>sample</code> streams the offline
        fixture; a numeric key such as <code>9904</code> replays a finished
        race from OpenF1.{" "}
        <strong>live:</strong> use <code>latest</code> to follow whatever
        session is running now. Outside a race weekend that correctly reports
        no live session — the next one is the Azerbaijan GP, 24–26 September.
      </p>
    </form>
  );
}
