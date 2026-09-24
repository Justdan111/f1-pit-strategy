"use client";

import { useEffect, useState } from "react";
import { Driver, Mode } from "@/lib/types";
import { fetchDrivers } from "@/lib/useRaceStream";

// Long enough that typing "9904" makes one request, not four.
const DRIVER_LOOKUP_DEBOUNCE_MS = 400;

type DriverList =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "loaded"; drivers: Driver[] }
  | { status: "error"; detail: string };

/** "Verstappen (1) — Red Bull Racing". Team is shown as what it is: metadata on the car. */
function driverLabel(driver: Driver): string {
  const name = driver.last_name ?? driver.full_name ?? `Car ${driver.driver_number}`;
  const team = driver.team_name ? ` — ${driver.team_name}` : "";
  return `${name} (${driver.driver_number})${team}`;
}

/** Teammates adjacent, so choosing "a team" is choosing one of two rows. */
function byTeamThenNumber(a: Driver, b: Driver): number {
  const team = (a.team_name ?? "").localeCompare(b.team_name ?? "");
  return team !== 0 ? team : a.driver_number - b.driver_number;
}

/** session_key input, driver picker, mode selector, and connect/disconnect controls. */
export function ConnectForm({
  onConnect,
  onDisconnect,
  busy,
}: {
  onConnect: (sessionKey: string, mode: Mode, driverNumber: number) => void;
  onDisconnect: () => void;
  busy: boolean;
}) {
  const [sessionKey, setSessionKey] = useState("sample");
  const [mode, setMode] = useState<Mode>("replay");
  // Both keyed by the session they belong to, so a stale list or a choice
  // made for a different session is ignored rather than carried over into a
  // session that car may not be in.
  const [lookup, setLookup] = useState<{ key: string; result: DriverList } | null>(null);
  const [choice, setChoice] = useState<{ key: string; driverNumber: number } | null>(null);

  const key = sessionKey.trim();

  useEffect(() => {
    if (!key) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      fetchDrivers(key, controller.signal)
        .then((drivers) =>
          setLookup({
            key,
            result: { status: "loaded", drivers: [...drivers].sort(byTeamThenNumber) },
          }),
        )
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          setLookup({
            key,
            result: {
              status: "error",
              detail: cause instanceof Error ? cause.message : String(cause),
            },
          });
        });
    }, DRIVER_LOOKUP_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [key]);

  let driverList: DriverList = { status: "idle" };
  if (key) driverList = lookup?.key === key ? lookup.result : { status: "loading" };

  const drivers = driverList.status === "loaded" ? driverList.drivers : [];
  let driverNumber: number | null = choice?.key === key ? choice.driverNumber : null;
  // A one-car session (the sample) has nothing to choose.
  if (driverNumber === null && drivers.length === 1) driverNumber = drivers[0].driver_number;

  const selected = drivers.find((d) => d.driver_number === driverNumber) ?? null;
  const canConnect = key !== "" && driverNumber !== null;

  let placeholder = "choose a driver";
  if (driverList.status === "loading") placeholder = "loading drivers…";
  else if (driverList.status === "idle") placeholder = "enter a session_key first";
  else if (driverList.status === "error") placeholder = "could not load drivers";
  else if (drivers.length === 0) placeholder = "no drivers listed yet";

  return (
    <form
      className="connect"
      onSubmit={(event) => {
        event.preventDefault();
        if (key && driverNumber !== null) onConnect(key, mode, driverNumber);
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
        <span>driver</span>
        <span className="driver-pick">
          <span
            className="team-swatch"
            aria-hidden
            style={{
              background: selected?.team_colour
                ? `#${selected.team_colour}`
                : "transparent",
            }}
          />
          <select
            value={driverNumber ?? ""}
            onChange={(event) =>
              setChoice(
                event.target.value === ""
                  ? null
                  : { key, driverNumber: Number(event.target.value) },
              )
            }
            disabled={drivers.length === 0}
          >
            <option value="" disabled>
              {placeholder}
            </option>
            {drivers.map((driver) => (
              <option key={driver.driver_number} value={driver.driver_number}>
                {driverLabel(driver)}
              </option>
            ))}
          </select>
        </span>
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
        <button type="submit" disabled={!canConnect}>
          Connect
        </button>
        <button type="button" onClick={onDisconnect} disabled={!busy}>
          Disconnect
        </button>
      </div>

      {driverList.status === "error" && (
        <p className="hint hint-error">{driverList.detail}</p>
      )}

      <p className="hint">
        <strong>replay:</strong> <code>sample</code> streams the offline
        fixture; a numeric key such as <code>9904</code> replays a finished
        race from OpenF1.{" "}
        <strong>live:</strong> use <code>latest</code> to follow whatever
        session is running now. A stream follows one car; to watch both cars
        of a team, open a second tab — each stream decides on its own.
      </p>
    </form>
  );
}
