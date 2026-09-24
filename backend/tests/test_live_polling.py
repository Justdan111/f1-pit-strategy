"""LiveTickSource polling behaviour.

Driven by a fake client whose data changes between polls. This proves the
logic -- laps emitted once, in order, held back while still running -- but
not that OpenF1 serves data this way during a real session.
"""

from datetime import timedelta

import pytest

from backend.live import LiveTickSource
from backend.models import Driver, Lap, Stint
from backend.tick_builder import MixedDriverDataError
from backend.tick_source import NoDataError

from .conftest import FakeOpenF1Client


class GrowingClient(FakeOpenF1Client):
    """A session that gains laps between polls, like a real one would."""

    def __init__(self, sessions, lap_schedule):
        super().__init__(sessions=sessions)
        self._schedule = lap_schedule
        self._poll = -1

    async def get_stints(self, session_key, driver_number=None):
        self.calls.append(("get_stints", (session_key, driver_number), {}))
        self._poll += 1
        stints, _ = self._schedule[min(self._poll, len(self._schedule) - 1)]
        return list(stints)

    async def get_laps(self, session_key, driver_number=None):
        self.calls.append(("get_laps", (session_key, driver_number), {}))
        _, laps = self._schedule[min(self._poll, len(self._schedule) - 1)]
        return list(laps)


def stint(lap_end, compound="HARD", age=0, number=1, lap_start=1):
    return Stint(driver_number=1, stint_number=number, lap_start=lap_start,
                 lap_end=lap_end, compound=compound, tyre_age_at_start=age)


def lap(n, duration):
    return Lap(driver_number=1, lap_number=n, lap_duration=duration)


class Clock:
    def __init__(self, start):
        self.start = start
        self.elapsed = 0.0

    def now(self):
        return self.start + timedelta(seconds=self.elapsed)

    async def sleep(self, seconds):
        self.elapsed += seconds


def build(session, schedule, settings, *, max_seconds=120):
    clock = Clock(session.date_start + timedelta(minutes=5))
    client = GrowingClient([session], schedule)
    source = LiveTickSource(
        client=client, session_key=str(session.session_key), driver_number=1,
        settings=settings, clock=clock.now, sleep=clock.sleep,
    )
    return source, client, clock


async def test_each_lap_is_emitted_exactly_once_across_polls(
    settings, baku_2025_race
):
    """The core requirement: no re-sending."""
    schedule = [
        ([stint(2)], [lap(1, 95.0), lap(2, 94.8)]),
        ([stint(3)], [lap(1, 95.0), lap(2, 94.8), lap(3, 94.6)]),
        ([stint(4)], [lap(1, 95.0), lap(2, 94.8), lap(3, 94.6), lap(4, 94.4)]),
        ([stint(5)], [lap(1, 95.0), lap(2, 94.8), lap(3, 94.6), lap(4, 94.4),
                      lap(5, 94.2)]),
    ]
    source, _, _ = build(baku_2025_race, schedule, settings)
    await source.open()
    ticks = [t async for t in source.ticks()]

    laps = [t.lap for t in ticks]
    assert laps == sorted(laps), "ticks arrived out of lap order"
    assert len(laps) == len(set(laps)), f"a lap was emitted twice: {laps}"
    assert set(laps) == {1, 2, 3, 4, 5}


async def test_a_poll_with_nothing_new_emits_nothing(settings, baku_2025_race):
    """Identical data on every poll must produce each lap once, not repeatedly."""
    frozen = ([stint(3)], [lap(1, 95.0), lap(2, 94.8), lap(3, 94.6)])
    source, client, _ = build(baku_2025_race, [frozen] * 8, settings)
    await source.open()
    ticks = [t async for t in source.ticks()]

    assert [t.lap for t in ticks] == [1, 2, 3]
    # It really did keep polling — the emptiness is the source's doing, not
    # the loop having stopped early.
    poll_calls = [c for c in client.calls if c[0] == "get_stints"]
    assert len(poll_calls) > 3


async def test_lap_in_progress_is_held_until_it_has_a_time(
    settings, baku_2025_race
):
    """A lap appears when the car STARTS it. Emitting early would permanently
    lose its lap time, since it would never be re-sent."""
    schedule = [
        # Lap 3 has started but not finished.
        ([stint(3)], [lap(1, 95.0), lap(2, 94.8),
                      Lap(driver_number=1, lap_number=3, lap_duration=None)]),
        # Now it has finished.
        ([stint(3)], [lap(1, 95.0), lap(2, 94.8), lap(3, 94.6)]),
    ]
    source, _, _ = build(baku_2025_race, schedule, settings)
    await source.open()
    ticks = [t async for t in source.ticks()]

    by_lap = {t.lap: t for t in ticks}
    assert set(by_lap) == {1, 2, 3}
    assert by_lap[3].lap_duration_s == 94.6, (
        "lap 3 was emitted while still in progress and never corrected"
    )


async def test_finished_lap_with_genuinely_missing_timing_is_still_emitted(
    settings, baku_2025_race
):
    """Once a higher lap exists, a missing duration is real absence, not pending."""
    schedule = [
        ([stint(4)], [lap(1, 95.0), lap(2, 94.8),
                      Lap(driver_number=1, lap_number=3, lap_duration=None),
                      lap(4, 94.4)]),
    ]
    source, _, _ = build(baku_2025_race, schedule, settings)
    await source.open()
    ticks = [t async for t in source.ticks()]

    by_lap = {t.lap: t for t in ticks}
    assert set(by_lap) == {1, 2, 3, 4}
    assert by_lap[3].lap_duration_s is None


async def test_no_stints_yet_is_not_an_error(settings, baku_2025_race):
    """A session can be live before any car has completed a lap."""
    source, _, _ = build(baku_2025_race, [([], [])], settings)
    await source.open()
    ticks = [t async for t in source.ticks()]
    assert ticks == []


async def test_stream_ends_on_idle_timeout_when_laps_stop_arriving(
    settings, baku_2025_race
):
    """First of two termination paths: give up after the idle timeout."""
    frozen = ([stint(3)], [lap(1, 95.0), lap(2, 94.8), lap(3, 94.6)])
    source, _, clock = build(baku_2025_race, [frozen] * 5000, settings)
    await source.open()
    ticks = [t async for t in source.ticks()]

    assert ticks, "produced nothing"
    idle_for = clock.elapsed
    assert idle_for >= settings.live_idle_timeout_seconds
    # Ended on idleness, well before the window would have closed.
    assert clock.now() < baku_2025_race.date_end + timedelta(minutes=30)


class EndlessClient(FakeOpenF1Client):
    """Adds a completed lap on every poll, so the stream is never idle."""

    def __init__(self, sessions):
        super().__init__(sessions=sessions)
        self._laps = 0

    async def get_stints(self, session_key, driver_number=None):
        self.calls.append(("get_stints", (session_key, driver_number), {}))
        self._laps += 1
        return [stint(self._laps)]

    async def get_laps(self, session_key, driver_number=None):
        self.calls.append(("get_laps", (session_key, driver_number), {}))
        return [lap(n, 95.0) for n in range(1, self._laps + 1)]


async def test_stream_ends_when_the_live_window_closes(settings, baku_2025_race):
    """Second termination path: laps keep arriving, so only the window can stop it."""
    # Five minutes from the close, so the simulated lap count stays realistic.
    closes = baku_2025_race.date_end + timedelta(minutes=30)
    clock = Clock(closes - timedelta(minutes=5))
    client = EndlessClient([baku_2025_race])
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=1, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    await source.open()
    ticks = [t async for t in source.ticks()]

    assert ticks, "produced nothing"
    assert clock.now() > closes, "loop stopped before the window closed"
    # Laps kept arriving throughout, so idleness was never the reason: five
    # minutes is far short of the 15-minute idle timeout.
    assert clock.elapsed < settings.live_idle_timeout_seconds
    assert len(ticks) > 10


async def test_polls_ask_the_api_for_one_driver_only(settings, baku_2025_race):
    """Filtering at the API, not after: the grid never reaches the flattener."""
    source, client, _ = build(
        baku_2025_race, [([stint(3)], [lap(n, 95.0) for n in range(1, 4)])], settings
    )
    await source.open()
    [t async for t in source.ticks()]

    data_calls = [c for c in client.calls if c[0] in ("get_stints", "get_laps")]
    assert data_calls
    assert all(args[1] == 1 for _, args, _ in data_calls)


async def test_car_still_in_the_garage_is_not_an_error(settings, baku_2025_race):
    """Practice: other cars are running, ours has no stint yet. Keep polling."""
    grid = [
        Stint(driver_number=44, stint_number=1, lap_start=1, lap_end=9,
              compound="SOFT", tyre_age_at_start=0),
    ]
    later = grid + [
        Stint(driver_number=16, stint_number=1, lap_start=1, lap_end=3,
              compound="MEDIUM", tyre_age_at_start=2),
    ]
    laps_44 = [Lap(driver_number=44, lap_number=n, lap_duration=94.0) for n in range(1, 10)]
    laps_16 = [Lap(driver_number=16, lap_number=n, lap_duration=96.0) for n in range(1, 4)]

    class GarageClient(FakeOpenF1Client):
        polls = 0

        async def get_stints(self, session_key, driver_number=None):
            self.calls.append(("get_stints", (session_key, driver_number), {}))
            self.polls += 1
            rows = grid if self.polls <= 2 else later
            return [s for s in rows if s.driver_number == driver_number]

        async def get_laps(self, session_key, driver_number=None):
            self.calls.append(("get_laps", (session_key, driver_number), {}))
            rows = laps_44 + (laps_16 if self.polls > 2 else [])
            return [r for r in rows if r.driver_number == driver_number]

    clock = Clock(baku_2025_race.date_start + timedelta(minutes=5))
    client = GarageClient(
        sessions=[baku_2025_race],
        drivers=[Driver(driver_number=16), Driver(driver_number=44)],
    )
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=16, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    await source.open()
    ticks = [t async for t in source.ticks()]

    assert [t.lap for t in ticks] == [1, 2, 3]
    assert {t.driver_number for t in ticks} == {16}
    assert [t.tyre_age for t in ticks] == [2, 3, 4]


async def test_open_refuses_a_driver_not_entered(settings, baku_2025_race):
    client = FakeOpenF1Client(
        sessions=[baku_2025_race],
        drivers=[Driver(driver_number=1), Driver(driver_number=16)],
    )
    clock = Clock(baku_2025_race.date_start + timedelta(minutes=5))
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=99, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    with pytest.raises(NoDataError) as caught:
        await source.open()
    assert caught.value.code == "unknown_driver"
    assert "[1, 16]" in caught.value.detail


async def test_a_leak_from_another_car_fails_loudly(settings, baku_2025_race):
    """If the API ever ignored the filter, the stream must stop, not blend cars."""

    class LeakyClient(FakeOpenF1Client):
        async def get_stints(self, session_key, driver_number=None):
            self.calls.append(("get_stints", (session_key, driver_number), {}))
            return list(self.stints)  # ignores driver_number

    client = LeakyClient(
        sessions=[baku_2025_race],
        stints=[
            Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=3,
                  compound="HARD", tyre_age_at_start=0),
            Stint(driver_number=44, stint_number=1, lap_start=1, lap_end=9,
                  compound="SOFT", tyre_age_at_start=0),
        ],
        laps=[lap(n, 95.0) for n in range(1, 4)],
    )
    clock = Clock(baku_2025_race.date_start + timedelta(minutes=5))
    source = LiveTickSource(
        client=client, session_key="9904", driver_number=1, settings=settings,
        clock=clock.now, sleep=clock.sleep,
    )
    await source.open()
    with pytest.raises(MixedDriverDataError):
        [t async for t in source.ticks()]
