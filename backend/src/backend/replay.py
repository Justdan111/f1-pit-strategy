"""ReplayTickSource: turns finished stint data into a paced, per-lap tick stream."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from .config import Settings, get_settings
from .models import Lap, SourceKind, StartMessage, Stint, TickMessage
from .openf1_client import OpenF1Client
from .sample_data import (
    SAMPLE_DRIVER_NUMBER,
    SAMPLE_SESSION_KEY,
    sample_laps,
    sample_stints,
)
from .tick_builder import ensure_driver_entered, fetch_driver_race, flatten_to_ticks
from .tick_source import NoDataError, TickSource

logger = logging.getLogger(__name__)


@dataclass
class RaceData:
    """One car's race. Stints say which tyre was on it; laps say how fast it went."""

    stints: list[Stint]
    laps: list[Lap] = field(default_factory=list)


RaceDataLoader = Callable[[], Awaitable[RaceData]]


class ReplayTickSource(TickSource):
    """Replays finished stint data as a paced tick stream.

    Pacing is a documented simplification, not a claim about real lap timing.
    Its purpose is to make the transport behave the way live mode will.

    Data arrives through an injected loader, so the same class serves the
    offline fixture and a real historical race with no mode branch. The
    loader returns one car's data only; the driver is always named, never
    picked.
    """

    def __init__(
        self,
        *,
        session_key: str,
        loader: RaceDataLoader,
        source: SourceKind,
        driver_number: int,
        tick_interval_seconds: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()

        self._session_key = session_key
        self._loader = loader
        self._source: SourceKind = source
        self._driver_number = driver_number
        self._tick_interval = (
            settings.replay_tick_interval_seconds
            if tick_interval_seconds is None
            else tick_interval_seconds
        )

        self._ticks: list[TickMessage] = []
        self._opened = False

    @classmethod
    def from_sample(
        cls,
        *,
        driver_number: int = SAMPLE_DRIVER_NUMBER,
        tick_interval_seconds: float | None = None,
        settings: Settings | None = None,
    ) -> "ReplayTickSource":
        """Replay the offline fixture. No network involved. It has one car."""

        async def loader() -> RaceData:
            if driver_number != SAMPLE_DRIVER_NUMBER:
                raise NoDataError(
                    f"Driver {driver_number} is not entered in session "
                    f"{SAMPLE_SESSION_KEY!r}. Drivers entered: [{SAMPLE_DRIVER_NUMBER}]",
                    code="unknown_driver",
                )
            return RaceData(stints=sample_stints(), laps=sample_laps())

        return cls(
            session_key=SAMPLE_SESSION_KEY,
            loader=loader,
            source="sample",
            driver_number=driver_number,
            tick_interval_seconds=tick_interval_seconds,
            settings=settings,
        )

    @classmethod
    def from_openf1(
        cls,
        *,
        client: OpenF1Client,
        session_key: str,
        driver_number: int,
        tick_interval_seconds: float | None = None,
        settings: Settings | None = None,
    ) -> "ReplayTickSource":
        """Replay one car's finished race fetched from OpenF1."""

        async def loader() -> RaceData:
            await ensure_driver_entered(client, session_key, driver_number)
            stints, laps = await fetch_driver_race(client, session_key, driver_number)
            return RaceData(stints=stints, laps=laps)

        return cls(
            session_key=session_key,
            loader=loader,
            source="historical_replay",
            driver_number=driver_number,
            tick_interval_seconds=tick_interval_seconds,
            settings=settings,
        )

    @property
    def session_key(self) -> str:
        return self._session_key

    @property
    def source(self) -> str:
        return self._source

    async def open(self) -> StartMessage:
        """Load, flatten, and describe the stream. All fallible work happens here."""
        race = await self._loader()
        driver_number = self._driver_number

        if not race.stints:
            raise NoDataError(
                f"OpenF1 returned no stint data for driver {driver_number} in "
                f"session_key={self._session_key!r}. Check the session has run "
                "and that this car took part in it."
            )

        # Raises if the loader let another car's rows through.
        self._ticks = flatten_to_ticks(race.stints, race.laps)
        if not self._ticks:
            raise NoDataError(
                f"Stint data for driver {driver_number} in session "
                f"{self._session_key!r} contained no usable laps."
            )

        self._opened = True

        timed = sum(1 for t in self._ticks if t.lap_duration_s is not None)
        logger.info(
            "Replay ready: session_key=%s source=%s driver=%s laps=%d "
            "(%d with lap times) interval=%.3fs",
            self._session_key,
            self._source,
            driver_number,
            len(self._ticks),
            timed,
            self._tick_interval,
        )
        if timed == 0:
            logger.warning(
                "No lap times merged for session_key=%s driver=%s. The stream "
                "will emit ticks but no decisions.",
                self._session_key,
                driver_number,
            )

        return StartMessage(
            session_key=self._session_key,
            source=self._source,
            total_laps=len(self._ticks),
            driver_number=driver_number,
        )

    async def ticks(self) -> AsyncIterator[TickMessage]:
        """Yield ticks one at a time, paced.

        `asyncio.sleep` rather than `time.sleep`: it suspends this coroutine
        and returns the event loop, so one server paces many streams at once.
        The sleep is between ticks, not before the first.
        """
        if not self._opened:
            raise RuntimeError("open() must be awaited before ticks()")

        for index, tick in enumerate(self._ticks):
            if index > 0 and self._tick_interval > 0:
                await asyncio.sleep(self._tick_interval)
            yield tick
