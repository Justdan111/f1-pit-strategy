"""Turning OpenF1 stints + laps into TickMessages. Shared by both TickSource implementations."""

import logging
from typing import Protocol

from .models import Driver, Lap, Stint, TickMessage
from .tick_source import NoDataError, TickSourceError

logger = logging.getLogger(__name__)


class MixedDriverDataError(TickSourceError):
    """Rows from more than one car reached the flattener.

    Laps are keyed by lap number alone, so two cars' data merged here would
    produce one plausible-looking stint history that belongs to neither car.
    That must be loud, never silent.
    """

    code = "mixed_driver_data"


class DriverDataClient(Protocol):
    """The slice of OpenF1Client the streaming sources need."""

    async def get_stints(
        self, session_key: str, driver_number: int | None = None
    ) -> list[Stint]: ...

    async def get_laps(
        self, session_key: str, driver_number: int | None = None
    ) -> list[Lap]: ...

    async def get_drivers(self, session_key: str) -> list[Driver]: ...


async def fetch_driver_race(
    client: DriverDataClient,
    session_key: str,
    driver_number: int,
) -> tuple[list[Stint], list[Lap]]:
    """One car's stints and laps, filtered at the API.

    The single fetch path for both replay and live, so the filter cannot be
    applied in one mode and forgotten in the other.
    """
    stints = await client.get_stints(session_key, driver_number)
    laps = await client.get_laps(session_key, driver_number)
    return stints, laps


async def ensure_driver_entered(
    client: DriverDataClient,
    session_key: str,
    driver_number: int,
) -> None:
    """Fail clearly if the car is not in this session, listing who is.

    An empty entry list is not treated as "nobody": OpenF1 can populate
    /v1/drivers late at the start of a live session, and refusing every car
    then would be worse than skipping the check.
    """
    drivers = await client.get_drivers(session_key)
    if not drivers:
        logger.warning(
            "OpenF1 returned no entry list for session_key=%s; cannot confirm "
            "driver %s is entered. Continuing.",
            session_key,
            driver_number,
        )
        return

    entered = sorted({d.driver_number for d in drivers})
    if driver_number not in entered:
        raise NoDataError(
            f"Driver {driver_number} is not entered in session {session_key!r}. "
            f"Drivers entered: {entered}",
            code="unknown_driver",
        )

# Beyond any real session, but small enough that a corrupt lap_end cannot exhaust memory.
MAX_PLAUSIBLE_STINT_LAPS = 500


def resolve_driver(
    stints: list[Stint],
    requested: int | None,
    session_key: str,
) -> int:
    """Pick which driver to backtest. Not used by the streaming sources, which
    require a driver and never pick one.

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

    Input from more than one car is refused, not filtered: it means a caller
    skipped the driver filter, and guessing which car was meant would hide that.
    """
    cars = {s.driver_number for s in stints} | {lap.driver_number for lap in laps}
    if len(cars) > 1:
        raise MixedDriverDataError(
            f"Refusing to build one tick stream from several cars: {sorted(cars)}. "
            "Stints and laps must be filtered to a single driver_number first."
        )

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
