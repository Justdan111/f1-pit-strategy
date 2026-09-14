"use client";

import { useState } from "react";
import { Mode } from "@/lib/types";

/**
 * session_key text input + mode selector + Connect/Disconnect.
 *
 * A plain text input, per DAY3.md — a race browser against /v1/sessions is
 * explicitly out of scope.
 *
 * The `live` option is enabled as of Day 4, now that LiveTickSource exists.
 * It was deliberately disabled before that, because an enabled toggle would
 * have promised something the backend could not do.
 *
 * Enabling it required no change to the connection layer: Day 3 passed
 * `mode` through generically rather than hardcoding "replay", so this is the
 * one-line change it was designed to be.
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
