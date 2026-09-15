"""LiveTickSource: polls OpenF1 during a live session and emits newly-completed laps."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Awaitable, Callable

from .config import Settings, get_settings
from .models import Lap, Session, StartMessage, Stint, TickMessage
from .openf1_client import OpenF1Client, OpenF1Error, OpenF1RateLimited
from .tick_builder import flatten_to_ticks, resolve_driver
from .tick_source import TickSource, TickSourceError

logger = logging.getLogger(__name__)

# OpenF1's shortcut for the most recent or currently-running session.
LATEST_SESSION_KEY = "latest"


class NoLiveSessionError(TickSourceError):
    """No session is running. Expected, not a malfunction."""

    code = "no_live_session"

    def __init__(
        self,
        detail: str,
        *,
        next_session: Session | None = None,
        checked_at: datetime | None = None,
    ) -> None:
        super().__init__(detail, code=self.code)
        self.next_session = next_session
        self.checked_at = checked_at or datetime.now(timezone.utc)


# `now` is a parameter, not a call to datetime.now(), so the definition of
# "live" can be tested at any instant including the exact boundaries.


def live_window(
    session: Session,
    margin_minutes: int,
) -> tuple[datetime, datetime]:
    """The interval during which OpenF1 serves live data for a session."""
    margin = timedelta(minutes=margin_minutes)
    return session.date_start - margin, session.date_end + margin


def is_session_live(
    session: Session,
    now: datetime,
    margin_minutes: int,
) -> bool:
    """Is this session live at `now`? Boundaries are inclusive at both ends."""
    if now.tzinfo is None:
        raise ValueError(
            "is_session_live() requires a timezone-aware `now`; "
            "got a naive datetime, which cannot be compared to OpenF1's "
            "UTC-offset timestamps."
        )
    if session.is_cancelled:
        return False

    opens, closes = live_window(session, margin_minutes)
    return opens <= now <= closes


def next_session_after(
    sessions: list[Session],
    now: datetime,
) -> Session | None:
    """The soonest session that has not started yet, or None."""
    upcoming = [s for s in sessions if not s.is_cancelled and s.date_start > now]
    return min(upcoming, key=lambda s: s.date_start) if upcoming else None


class LiveTickSource(TickSource):
    """Polls OpenF1 while a session's live window is open."""

    def __init__(
        self,
        *,
        client: OpenF1Client,
        session_key: str = LATEST_SESSION_KEY,
        driver_number: int | None = None,
        settings: Settings | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._requested_session_key = session_key
        self._requested_driver = driver_number
        self._settings = settings or get_settings()

        # Injected so tests can control time.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep

        self._session: Session | None = None
        self._driver_number: int | None = None
        # Per connection, so a reconnect replays the race so far.
        self._emitted_laps: set[int] = set()
        self._opened = False

    @property
    def session_key(self) -> str:
        if self._session:
            return str(self._session.session_key)
        return self._requested_session_key

    @property
    def source(self) -> str:
        return "live"

    async def open(self) -> StartMessage:
        """Find a live session, or explain that there isn't one."""
        now = self._clock()
        session = await self._resolve_session(now)

        if not is_session_live(session, now, self._settings.live_window_margin_minutes):
            raise await self._no_live_session(session, now)

        self._session = session
        self._opened = True
        opens, closes = live_window(session, self._settings.live_window_margin_minutes)
        logger.info(
            "Live session detected: %s (session_key=%s), window %s to %s, "
            "polling every %.1fs",
            session.label,
            session.session_key,
            opens.isoformat(),
            closes.isoformat(),
            self._settings.live_poll_interval_seconds,
        )

        return StartMessage(
            session_key=str(session.session_key),
            source="live",
            # Always None: a race in progress has no known total.
            total_laps=None,
            driver_number=self._requested_driver,
        )

    async def ticks(self) -> AsyncIterator[TickMessage]:
        """Poll while the window is open, yielding only laps not yet sent."""
        if not self._opened or self._session is None:
            raise RuntimeError("open() must be awaited before ticks()")

        session = self._session
        _, closes = live_window(session, self._settings.live_window_margin_minutes)

        backoff = self._settings.live_backoff_initial_seconds
        consecutive_failures = 0
        last_new_lap_at = self._clock()

        while True:
            now = self._clock()

            if now > closes:
                logger.info(
                    "Live window closed for session_key=%s at %s; ending stream.",
                    session.session_key,
                    closes.isoformat(),
                )
                return

            try:
                new_ticks = await self._poll()
            except OpenF1Error as exc:
                consecutive_failures += 1
                if consecutive_failures >= self._settings.live_max_consecutive_failures:
                    raise TickSourceError(
                        f"OpenF1 failed {consecutive_failures} times in a row while "
                        f"streaming session_key={session.session_key}: {exc}",
                        code="live_polling_failed",
                    ) from exc

                wait = backoff
                if isinstance(exc, OpenF1RateLimited) and exc.retry_after_seconds:
                    # Honour the server's instruction over our own guess.
                    wait = max(wait, exc.retry_after_seconds)

                logger.warning(
                    "Poll %d/%d failed (%s). Backing off %.1fs.",
                    consecutive_failures,
                    self._settings.live_max_consecutive_failures,
                    exc,
                    wait,
                )
                await self._sleep(wait)
                backoff = min(backoff * 2, self._settings.live_backoff_max_seconds)
                continue

            # Transient failures must not accumulate across healthy polling.
            consecutive_failures = 0
            backoff = self._settings.live_backoff_initial_seconds

            for tick in new_ticks:
                self._emitted_laps.add(tick.lap)
                yield tick

            if new_ticks:
                last_new_lap_at = self._clock()
            elif (
                self._clock() - last_new_lap_at
            ).total_seconds() > self._settings.live_idle_timeout_seconds:
                logger.info(
                    "No new laps for %.0fs on session_key=%s; ending stream.",
                    self._settings.live_idle_timeout_seconds,
                    session.session_key,
                )
                return

            await self._sleep(self._settings.live_poll_interval_seconds)

    async def _resolve_session(self, now: datetime) -> Session:
        sessions = await self._client.get_sessions(
            session_key=self._requested_session_key
        )
        if not sessions:
            raise NoLiveSessionError(
                "OpenF1 returned no session for "
                f"session_key={self._requested_session_key!r}.",
                checked_at=now,
            )
        return sessions[0]

    async def _no_live_session(
        self, session: Session, now: datetime
    ) -> NoLiveSessionError:
        """Build the 'nothing live' answer, with the next session when known.

        A failed lookup of what is next must not turn the expected case into
        an error, so it degrades to a bare answer.
        """
        opens, closes = live_window(session, self._settings.live_window_margin_minutes)
        upcoming: Session | None = None
        try:
            candidates = await self._client.get_sessions(year=now.year)
            upcoming = next_session_after(candidates, now)
            if upcoming is None:
                candidates = await self._client.get_sessions(year=now.year + 1)
                upcoming = next_session_after(candidates, now)
        except OpenF1Error as exc:
            logger.warning("Could not look up the next session: %s", exc)

        if now < opens:
            why = (
                f"The most recent session ({session.label}) has not started yet; "
                f"live data opens at {opens.isoformat()}."
            )
        else:
            why = (
                f"The most recent session ({session.label}) is over; "
                f"live data closed at {closes.isoformat()}."
            )

        detail = f"No live session right now. {why}"
        if upcoming is not None:
            detail += f" Next up: {upcoming.label} at {upcoming.date_start.isoformat()}."

        return NoLiveSessionError(detail, next_session=upcoming, checked_at=now)

    async def _poll(self) -> list[TickMessage]:
        """One poll: fetch, merge, and return only laps not already emitted."""
        session_key = str(self._session.session_key)  # type: ignore[union-attr]

        stints: list[Stint] = await self._client.get_stints(session_key)
        if not stints:
            # Normal early on: the session is live but no lap is complete.
            return []

        if self._driver_number is None:
            # Resolved once and then fixed, so the stream cannot switch cars.
            self._driver_number = resolve_driver(
                stints, self._requested_driver, session_key
            )
            logger.info("Live stream following driver %s", self._driver_number)

        laps: list[Lap] = await self._client.get_laps(session_key, self._driver_number)

        driver_stints = [s for s in stints if s.driver_number == self._driver_number]
        all_ticks = flatten_to_ticks(driver_stints, laps)
        return self._select_new(all_ticks)

    def _select_new(self, ticks: list[TickMessage]) -> list[TickMessage]:
        """Which laps to send now.

        Never re-send an emitted lap, and hold back the newest lap while it has
        no lap time. A lap appears in /v1/laps when the car STARTS it, with a
        null duration; emitting immediately would send a tick whose lap time is
        permanently null, since it would never be sent again. Released once a
        duration arrives or a higher lap proves the earlier one is over.
        """
        if not ticks:
            return []

        highest_lap = max(t.lap for t in ticks)
        ready: list[TickMessage] = []

        for tick in ticks:
            if tick.lap in self._emitted_laps:
                continue
            still_running = tick.lap == highest_lap and tick.lap_duration_s is None
            if still_running:
                continue
            ready.append(tick)

        return sorted(ready, key=lambda t: t.lap)
