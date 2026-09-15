"""Turning OpenF1 stints + laps into TickMessages. Shared by both TickSource implementations."""

import logging

from .models import Lap, Stint, TickMessage
from .tick_source import NoDataError

logger = logging.getLogger(__name__)

# Beyond any real session, but small enough that a corrupt lap_end cannot exhaust memory.
MAX_PLAUSIBLE_STINT_LAPS = 500


def resolve_driver(
    stints: list[Stint],
    requested: int | None,
    session_key: str,
) -> int:
    """Pick which driver to stream.

    With no driver named, take the car that completed the most laps, tie-broken
    by lowest number: deterministic, and biased towards a car still running.
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
    """Expand stint ranges into one tick per lap, merged with lap timing on lap number.

    Tyre age is `tyre_age_at_start + (lap - lap_start)`, not `lap - lap_start`:
    OpenF1 reports stints beginning on used sets.

    Bad records are skipped rather than fatal, since live data can be briefly
    inconsistent. A lap with no timing keeps lap_duration_s = None; the tick is
    still true, only the fit loses a point.
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

    # Sorted so the stream is chronological; a later stint wins a shared lap.
    return [by_lap[lap] for lap in sorted(by_lap)]
