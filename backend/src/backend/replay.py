"""ReplayTickSource — the Day 1 implementation of TickSource.

It takes stint data (a compact, per-stint summary of a race) and turns it
into a per-lap stream, paced out over wall-clock time.

The two jobs, in order:

1. FLATTEN (in open()). Stints are ranges: "laps 19-40 on HARDs, starting
   age 0". Ticks are points: "lap 27, HARD, age 8". Expanding ranges into
   points is where tyre age gets computed, and it's the only real logic in
   this file.

2. PACE (in ticks()). Yield those points one at a time with a delay between
   them, so downstream code sees a stream that arrives over time rather than
   a list that arrives at once.

Why pacing is fake, and why that's fine
---------------------------------------
PROJECT.md and SPEC 7.3 put real-time-accurate replay pacing explicitly out
of scope. We sleep a fixed interval; we do not sleep the actual lap time. The
purpose is not to simulate a race faithfully — it's to make the transport
behave the way live mode will. A handler that works against a source which
yields slowly, unpredictably, and possibly forever is a handler that will
still work on Day 4. A handler written against a list would not be.

Why the data is loaded through an injected callable
---------------------------------------------------
`ReplayTickSource` never imports the OpenF1 client or the sample fixture. It
is handed a `loader`: an async callable returning stints. That's why the same
class serves both "replay a real historical race" and "replay the offline
fixture" without a single `if sample:` branch, and why a test can replay
whatever stints it likes without a network or a fixture file.
"""

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
from .tick_builder import flatten_to_ticks, resolve_driver
from .tick_source import NoDataError, TickSource

logger = logging.getLogger(__name__)


@dataclass
class RaceData:
    """Everything needed to build a tick stream for one session.

    Two separate OpenF1 endpoints, carried together: stints say which tyre was
    on the car, laps say how fast it went. Day 1 only needed stints; Day 2's
    degradation curve needs both, so the loader now returns a pair rather than
    a bare list.

    Bundling them in one object (rather than adding a second loader) keeps a
    single injection point, which is what let the sample fixture and the live
    API stay behind the same seam in the first place.
    """

    stints: list[Stint]
    laps: list[Lap] = field(default_factory=list)


# "Give me the data for this stream." Async because the real one does HTTP.
RaceDataLoader = Callable[[], Awaitable[RaceData]]


class ReplayTickSource(TickSource):
    """Replays finished stint data as a paced, per-lap tick stream."""

    def __init__(
        self,
        *,
        session_key: str,
        loader: RaceDataLoader,
        source: SourceKind,
        driver_number: int | None = None,
        tick_interval_seconds: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()

        self._session_key = session_key
        self._loader = loader
        self._source: SourceKind = source
        self._requested_driver = driver_number
        self._tick_interval = (
            settings.replay_tick_interval_seconds
            if tick_interval_seconds is None
            else tick_interval_seconds
        )

        # Populated by open(). Kept private so nothing reads them before then.
        self._ticks: list[TickMessage] = []
        self._driver_number: int | None = None
        self._opened = False

    # --- factories -------------------------------------------------------
    # Two ways to build one, differing only in where the stints come from.
    # Note they also set `source` correctly, which is the thing most likely
    # to be got wrong by hand: a replayed historical race is NOT "live".

    @classmethod
    def from_sample(
        cls,
        *,
        tick_interval_seconds: float | None = None,
        settings: Settings | None = None,
    ) -> "ReplayTickSource":
        """Replay the offline fixture. No network involved."""

        async def loader() -> RaceData:
            return RaceData(stints=sample_stints(), laps=sample_laps())

        return cls(
            session_key=SAMPLE_SESSION_KEY,
            loader=loader,
            source="sample",
            driver_number=SAMPLE_DRIVER_NUMBER,
            tick_interval_seconds=tick_interval_seconds,
            settings=settings,
        )

    @classmethod
    def from_openf1(
        cls,
        *,
        client: OpenF1Client,
        session_key: str,
        driver_number: int | None = None,
        tick_interval_seconds: float | None = None,
        settings: Settings | None = None,
    ) -> "ReplayTickSource":
        """Replay a finished race fetched from OpenF1."""

        async def loader() -> RaceData:
            # Stints are deliberately NOT filtered by driver at the API.
            # Fetching the whole session (one request either way, a few KB
            # bigger) means _resolve_driver can see the full grid, so asking
            # for a driver who wasn't in the session produces "driver 99 isn't
            # here, these are: [...]" instead of an indistinguishable "no data
            # for this session_key". Filtering is this class's job.
            #
            # Laps ARE filtered by driver when we know which one we want: a
            # full session's laps is ~20x the payload and we would throw all
            # but one driver's away. When no driver was requested we cannot
            # filter yet (the driver isn't chosen until we've seen the
            # stints), so we fetch the lot and narrow in _flatten.
            stints = await client.get_stints(session_key)
            laps = await client.get_laps(session_key, driver_number)
            return RaceData(stints=stints, laps=laps)

        return cls(
            session_key=session_key,
            loader=loader,
            source="historical_replay",
            driver_number=driver_number,
            tick_interval_seconds=tick_interval_seconds,
            settings=settings,
        )

    # --- TickSource contract ---------------------------------------------

    @property
    def session_key(self) -> str:
        return self._session_key

    @property
    def source(self) -> str:
        return self._source

    async def open(self) -> StartMessage:
        """Load the stints, flatten them to ticks, and describe the stream.

        All the fallible work happens here, before a single tick is sent, so
        a failure can be reported as an `error` envelope instead of killing a
        stream that had already claimed to start.
        """
        race = await self._loader()
        stints = race.stints

        if not stints:
            raise NoDataError(
                f"OpenF1 returned no stint data for session_key={self._session_key!r}. "
                "Check the session_key is correct and that the session has run."
            )

        driver_number = resolve_driver(stints, self._requested_driver, self._session_key)
        driver_stints = [s for s in stints if s.driver_number == driver_number]
        driver_laps = [lap for lap in race.laps if lap.driver_number == driver_number]

        self._ticks = flatten_to_ticks(driver_stints, driver_laps)
        if not self._ticks:
            raise NoDataError(
                f"Stint data for driver {driver_number} in session "
                f"{self._session_key!r} contained no usable laps."
            )

        self._driver_number = driver_number
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
            # Not fatal — the stream is still valid and Day 1's behaviour is
            # unchanged. But every decision will be skipped, so say why once
            # here rather than leaving someone to wonder at the silence.
            logger.warning(
                "No lap times merged for session_key=%s driver=%s. The stream "
                "will emit ticks but no decisions.",
                self._session_key,
                driver_number,
            )

        return StartMessage(
            session_key=self._session_key,
            source=self._source,
            # Replay knows the total up front because the race has finished.
            # Live mode will send None here — the race hasn't ended yet.
            total_laps=len(self._ticks),
            driver_number=driver_number,
        )

    async def ticks(self) -> AsyncIterator[TickMessage]:
        """Yield the flattened ticks, one at a time, paced.

        The sleep is *between* ticks, not before the first: connecting should
        produce data immediately, not after a beat of silence.

        `await asyncio.sleep(...)` rather than `time.sleep(...)` is the whole
        ballgame. It suspends this coroutine and hands the event loop back, so
        one server can pace hundreds of replays concurrently on one thread.
        time.sleep() would block the loop and freeze every other connection.
        """
        if not self._opened:
            raise RuntimeError("open() must be awaited before ticks()")

        for index, tick in enumerate(self._ticks):
            if index > 0 and self._tick_interval > 0:
                await asyncio.sleep(self._tick_interval)
            yield tick

