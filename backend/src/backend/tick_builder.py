"""Turning OpenF1's stints + laps into TickMessages.

Extracted from ReplayTickSource on Day 4 so LiveTickSource uses the SAME
code, not merely equivalent code.

DAY4.md requires live ticks to be "the exact same shape" as replay ticks, so
that the decision engine and frontend need no changes. Two separate
implementations could satisfy that on the day they were written and drift the
first time either is touched. One shared function cannot drift: there is only
one place where a TickMessage is constructed from race data, and the contract
test asserts both sources route through it.
"""

import logging

from .models import Lap, Stint, TickMessage
from .tick_source import NoDataError

logger = logging.getLogger(__name__)

# No Grand Prix has ever exceeded ~200 laps (the 1950s Indy 500 counted for
# the championship at 200). 500 is far beyond any real session while still
# small enough to stop a corrupt lap_end from exhausting memory.
MAX_PLAUSIBLE_STINT_LAPS = 500


def resolve_driver(
    stints: list[Stint],
    requested: int | None,
    session_key: str,
) -> int:
    """Pick which driver to stream.

    A session's stints cover the whole grid; a tick stream is about one car.
    If a driver was named, use it (and say clearly if they are not present).
    Otherwise take the car that has completed the most laps, tie-broken by
    lowest number — deterministic, and biased towards a car that is actually
    running rather than one that retired early.

    Shared between replay and live. In live mode "most laps so far" is
    re-evaluated only once, on the first poll that returns data, so the
    stream does not switch cars mid-race.
    """
    available = {s.driver_number for s in stints}

    if requested is not None:
        if requested not in available:
            raise NoDataError(
                f"No stint data for driver {requested} in session "
                f"{session_key!r}. Drivers present: {sorted(available)}"
            )
        return requested

    if not available:
        raise NoDataError(f"No drivers found in session {session_key!r}.")

    furthest: dict[int, int] = {}
    for stint in stints:
        furthest[stint.driver_number] = max(
            furthest.get(stint.driver_number, 0), stint.lap_end
        )
    return max(furthest.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def flatten_to_ticks(stints: list[Stint], laps: list[Lap]) -> list[TickMessage]:
    """Expand stint ranges into one tick per lap, merged with lap timing.

    Stints are ranges ("laps 19-40 on HARDs, starting at age 0"); ticks are
    points ("lap 27, HARD, age 8"). This is where that expansion happens, and
    where tyre age is computed:

        tyre_age = tyre_age_at_start + (lap - lap_start)

    NOT simply (lap - lap_start). OpenF1 reports stints that begin on used
    sets — Baku 2025 car #1 stint 2 starts at age 4 — so the naive version is
    wrong on real data while passing every test written against a naive
    fixture. Day 2's degradation curve depends on this being right.

    The merge: timing comes from a different endpoint (/v1/laps) than tyre
    data (/v1/stints), with no shared row identity, so they are joined on lap
    number. Callers narrow both sides to one driver first.

    Robustness, which matters more in live mode than replay (DAY4.md asks for
    malformed or partial data to degrade rather than crash):

    - A lap with no timing entry keeps lap_duration_s = None rather than
      being dropped. The tick is still true — the car ran that lap on that
      tyre — only the degradation fit loses a data point.
    - A stint whose lap_end precedes its lap_start is skipped and logged.
      Live data is mid-flight and can be briefly inconsistent; one nonsense
      stint must not take the stream down.
    - A stint spanning an implausible number of laps is skipped rather than
      expanded. Without this, a single corrupt lap_end of 100000 would build
      a hundred thousand ticks and exhaust memory — a malformed-data bug that
      presents as an outage.
    """
    timing: dict[int, Lap] = {lap.lap_number: lap for lap in laps}
    by_lap: dict[int, TickMessage] = {}

    for stint in sorted(stints, key=lambda s: (s.stint_number, s.lap_start)):
        if stint.lap_end < stint.lap_start:
            logger.warning(
                "Skipping stint %s: lap_end (%d) precedes lap_start (%d)",
                stint.stint_number,
                stint.lap_end,
                stint.lap_start,
            )
            continue

        if stint.lap_start < 0 or stint.lap_count > MAX_PLAUSIBLE_STINT_LAPS:
            logger.warning(
                "Skipping implausible stint %s spanning laps %d-%d (%d laps)",
                stint.stint_number,
                stint.lap_start,
                stint.lap_end,
                stint.lap_count,
            )
            continue

        for lap in range(stint.lap_start, stint.lap_end + 1):
            timed = timing.get(lap)
            by_lap[lap] = TickMessage(
                lap=lap,
                driver_number=stint.driver_number,
                compound=stint.compound,
                tyre_age=stint.tyre_age_at_start + (lap - stint.lap_start),
                stint_number=stint.stint_number,
                lap_duration_s=timed.lap_duration if timed else None,
                is_pit_out_lap=timed.is_pit_out_lap if timed else False,
            )

    # Sorted so the stream is strictly chronological regardless of the order
    # stints arrived in. Later stints overwrite earlier ones on a shared lap,
    # which matches reality: after a stop, that lap ran on the new set.
    return [by_lap[lap] for lap in sorted(by_lap)]

