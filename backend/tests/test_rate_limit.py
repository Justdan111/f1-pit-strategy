"""Rate-limit compliance against OpenF1's ~3 req/s and 30 req/min free tier.

OpenF1 advertises no rate-limit headers, so compliance is enforced before the
request leaves. Everything runs on a virtual clock: real sleeps would make
this suite slow enough to end up skipped.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.config import Settings
from backend.live import LiveTickSource
from backend.openf1_client import OpenF1RateLimited, OpenF1Unavailable, RateLimiter
from backend.tick_source import TickSourceError

from .conftest import FakeOpenF1Client, utc


class VirtualClock:
    """Time that only moves when something sleeps. Serves both a float and a datetime."""

    def __init__(self, start: datetime) -> None:
        self.start = start
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed)

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0, "negative sleep means the interval maths is wrong"
        self.sleeps.append(seconds)
        self.elapsed += seconds


# --- the limiter ----------------------------------------------------------


async def test_limiter_spaces_requests_by_the_minimum_interval():
    clock = VirtualClock(utc("2026-09-24T09:00:00+00:00"))
    limiter = RateLimiter(0.4, clock=clock.monotonic, sleep=clock.sleep)

    stamps: list[float] = []
    for _ in range(10):
        await limiter.acquire()
        stamps.append(clock.monotonic())

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps, "no gaps measured"
    assert min(gaps) >= 0.4 - 1e-9, f"requests too close together: {gaps}"


async def test_limiter_does_not_delay_a_request_that_is_already_late():
    """If enough time has passed on its own, acquire() must not add a wait."""
    clock = VirtualClock(utc("2026-09-24T09:00:00+00:00"))
    limiter = RateLimiter(0.4, clock=clock.monotonic, sleep=clock.sleep)

    await limiter.acquire()
    clock.elapsed += 5.0  # the world moved on without us
    await limiter.acquire()

    assert clock.sleeps == [], "slept despite the interval already having passed"


async def test_limiter_keeps_the_client_under_three_requests_per_second():
    clock = VirtualClock(utc("2026-09-24T09:00:00+00:00"))
    settings = Settings()
    limiter = RateLimiter(
        settings.openf1_min_request_interval_seconds,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    for _ in range(30):
        await limiter.acquire()

    # 30 requests spaced by the configured minimum.
    observed_rate = 30 / clock.monotonic() if clock.monotonic() else float("inf")
    assert observed_rate <= 3.0, f"{observed_rate:.2f} req/s exceeds the 3 req/s tier"


# --- the poll loop --------------------------------------------------------


async def test_poll_loop_never_polls_faster_than_the_configured_interval(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """The poll loop must never beat its configured interval."""
    clock = VirtualClock(baku_2025_race.date_start + timedelta(minutes=10))
    client = FakeOpenF1Client(
        sessions=[baku_2025_race], stints=stints_one_driver, laps=laps_one_driver
    )

    source = LiveTickSource(
        client=client,
        session_key="9904",
        driver_number=1,
        settings=settings,
        clock=clock.now,
        sleep=clock.sleep,
    )
    await source.open()
    ticks = [t async for t in source.ticks()]

    assert ticks, "no ticks; the loop never ran and the test proves nothing"

    # Every sleep between polls is at least the configured interval. The idle
    # path also sleeps the same amount, so a minimum across all of them is
    # the right assertion.
    assert clock.sleeps, "the loop never slept — it would poll flat out"
    assert min(clock.sleeps) >= settings.live_poll_interval_seconds, (
        f"polled faster than {settings.live_poll_interval_seconds}s: {clock.sleeps}"
    )


async def test_poll_loop_request_rate_stays_inside_the_per_minute_budget(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """Requests per simulated minute must sit well under 30."""
    clock = VirtualClock(baku_2025_race.date_start + timedelta(minutes=10))
    client = FakeOpenF1Client(
        sessions=[baku_2025_race], stints=stints_one_driver, laps=laps_one_driver
    )
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=1, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    await source.open()
    [t async for t in source.ticks()]

    polling_calls = [c for c in client.calls if c[0] in ("get_stints", "get_laps")]
    minutes = max(clock.monotonic() / 60.0, 1e-9)
    per_minute = len(polling_calls) / minutes

    assert per_minute <= 30, f"{per_minute:.1f} req/min exceeds the 30/min tier"
    # Confirm real margin, not just bare compliance.
    assert per_minute <= 20, (
        f"{per_minute:.1f} req/min leaves too little margin under the 30/min limit"
    )


# --- failure handling -----------------------------------------------------


async def test_rate_limited_response_backs_off_instead_of_crashing(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """A 429 must back off and recover, not take the stream down."""
    clock = VirtualClock(baku_2025_race.date_start + timedelta(minutes=10))
    client = FakeOpenF1Client(
        sessions=[baku_2025_race], stints=stints_one_driver, laps=laps_one_driver
    )
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=1, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    await source.open()

    # Injected only now, so the failure lands in the POLL loop rather than in
    # session resolution — the loop is what is under test here.
    client.fail_with = OpenF1RateLimited("slow down", retry_after_seconds=12.0)
    client.fail_times = 2

    ticks = [t async for t in source.ticks()]

    assert ticks, "stream produced nothing after recovering from a 429"
    # Retry-After (12s) is honoured over our smaller default backoff (5s).
    assert any(s >= 12.0 for s in clock.sleeps), (
        f"Retry-After was ignored; sleeps were {clock.sleeps}"
    )


async def test_persistent_failure_eventually_reports_an_error(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """Backoff must give up rather than retry forever in silence."""
    clock = VirtualClock(baku_2025_race.date_start + timedelta(minutes=10))
    client = FakeOpenF1Client(
        sessions=[baku_2025_race], stints=stints_one_driver, laps=laps_one_driver
    )
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=1, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    await source.open()
    client.fail_with = OpenF1Unavailable("upstream is down")
    client.fail_times = 10_000

    with pytest.raises(TickSourceError, match="failed .* times in a row"):
        [t async for t in source.ticks()]


async def test_backoff_grows_and_is_capped(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    clock = VirtualClock(baku_2025_race.date_start + timedelta(minutes=10))
    client = FakeOpenF1Client(
        sessions=[baku_2025_race], stints=stints_one_driver, laps=laps_one_driver
    )
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=1, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    await source.open()
    client.fail_with = OpenF1Unavailable("down")
    client.fail_times = 10_000

    with pytest.raises(TickSourceError):
        [t async for t in source.ticks()]

    assert clock.sleeps == sorted(clock.sleeps), "backoff did not grow monotonically"
    assert max(clock.sleeps) <= settings.live_backoff_max_seconds
