"""The TickSource contract test.

The claim is not that both classes have the right method names, but that
nothing downstream can tell which implementation produced a tick. Both are
run over the same data and their output compared field by field.
"""

from datetime import timedelta

import pytest

from backend.decision_engine import DecisionEngine
from backend.live import LiveTickSource
from backend.models import TickMessage
from backend.replay import RaceData, ReplayTickSource
from backend.tick_source import TickSource

from .conftest import FakeOpenF1Client, utc

REQUIRED_TICK_FIELDS = {
    "type",
    "lap",
    "driver_number",
    "compound",
    "tyre_age",
    "stint_number",
    "lap_duration_s",
    "is_pit_out_lap",
}


def _replay_source(settings, stints, laps):
    async def loader() -> RaceData:
        return RaceData(stints=stints, laps=laps)

    return ReplayTickSource(
        session_key="9904",
        loader=loader,
        source="historical_replay",
        driver_number=1,
        tick_interval_seconds=0,
        settings=settings,
    )


def _live_source(settings, session, stints, laps, *, poll_calls=3):
    """LiveTickSource with time and network faked. The clock eventually closes the window."""
    client = FakeOpenF1Client(sessions=[session], stints=stints, laps=laps)
    times = iter_clock(session, settings, poll_calls)

    async def no_sleep(_seconds: float) -> None:
        return None

    return LiveTickSource(
        client=client,
        session_key=str(session.session_key),
        driver_number=1,
        settings=settings,
        clock=lambda: next(times),
        sleep=no_sleep,
    )


def iter_clock(session, settings, poll_calls):
    """Clock that sits mid-window, then jumps past the close."""
    mid = session.date_start + timedelta(minutes=30)
    for i in range(poll_calls * 4):
        yield mid
    while True:
        yield session.date_end + timedelta(days=1)


# --- both are TickSources -------------------------------------------------


@pytest.mark.parametrize("implementation", [ReplayTickSource, LiveTickSource])
def test_both_implement_the_interface(implementation):
    assert issubclass(implementation, TickSource)
    assert not implementation.__abstractmethods__, (
        f"{implementation.__name__} leaves abstract methods unimplemented: "
        f"{implementation.__abstractmethods__}"
    )


# --- the real claim: identical tick shape ---------------------------------


async def test_both_sources_emit_identical_tick_shapes(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """An equality check between implementations, not a shape check against a schema."""
    replay = _replay_source(settings, stints_one_driver, laps_one_driver)
    await replay.open()
    replay_ticks = [t async for t in replay.ticks()]

    live = _live_source(settings, baku_2025_race, stints_one_driver, laps_one_driver)
    await live.open()
    live_ticks = [t async for t in live.ticks()]

    assert replay_ticks, "replay produced no ticks; the test proves nothing"
    assert live_ticks, "live produced no ticks; the test proves nothing"

    # Same field names on both sides.
    for tick in replay_ticks + live_ticks:
        assert set(tick.model_dump().keys()) == REQUIRED_TICK_FIELDS

    # Live holds back the in-progress final lap, so it can legitimately be
    # one tick shorter. Compare the overlap, field for field.
    shared = min(len(replay_ticks), len(live_ticks))
    assert shared > 0
    for replay_tick, live_tick in zip(replay_ticks[:shared], live_ticks[:shared]):
        assert replay_tick.model_dump() == live_tick.model_dump()


async def test_both_sources_produce_tick_messages(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """Identical type, not merely identical duck-typed fields."""
    replay = _replay_source(settings, stints_one_driver, laps_one_driver)
    await replay.open()
    replay_ticks = [t async for t in replay.ticks()]

    live = _live_source(settings, baku_2025_race, stints_one_driver, laps_one_driver)
    await live.open()
    live_ticks = [t async for t in live.ticks()]

    assert replay_ticks and live_ticks
    assert all(isinstance(t, TickMessage) for t in replay_ticks)
    assert all(isinstance(t, TickMessage) for t in live_ticks)


async def test_start_messages_differ_only_where_they_must(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """total_laps is None in live mode; everything else must match."""
    replay = _replay_source(settings, stints_one_driver, laps_one_driver)
    replay_start = await replay.open()

    live = _live_source(settings, baku_2025_race, stints_one_driver, laps_one_driver)
    live_start = await live.open()

    assert set(replay_start.model_dump()) == set(live_start.model_dump())
    assert replay_start.source == "historical_replay"
    assert live_start.source == "live"
    assert replay_start.total_laps == 6
    assert live_start.total_laps is None


# --- the downstream consequence -------------------------------------------


async def test_decision_engine_is_indifferent_to_the_source(
    settings, baku_2025_race, stints_one_driver, laps_one_driver
):
    """The same engine, fed from each source, must reach the same conclusions."""
    replay = _replay_source(settings, stints_one_driver, laps_one_driver)
    await replay.open()
    replay_engine = DecisionEngine(settings)
    replay_decisions = [
        d
        async for d in _decisions(replay_engine, replay)
    ]

    live = _live_source(settings, baku_2025_race, stints_one_driver, laps_one_driver)
    await live.open()
    live_engine = DecisionEngine(settings)
    live_decisions = [d async for d in _decisions(live_engine, live)]

    shared = min(len(replay_decisions), len(live_decisions))
    assert shared > 0, "no decisions produced; the test proves nothing"
    for replay_decision, live_decision in zip(
        replay_decisions[:shared], live_decisions[:shared]
    ):
        assert replay_decision.model_dump() == live_decision.model_dump()


async def _decisions(engine, source):
    async for tick in source.ticks():
        decision = engine.observe(tick)
        if decision is not None:
            yield decision


async def test_engine_skips_cleanly_on_live_ticks_with_too_little_data(
    settings, baku_2025_race
):
    """Skip behaviour, exercised through LiveTickSource specifically."""
    from backend.models import Lap, Stint

    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=2,
              compound="HARD", tyre_age_at_start=0)
    ]
    laps = [
        Lap(driver_number=1, lap_number=1, lap_duration=95.0),
        Lap(driver_number=1, lap_number=2, lap_duration=95.2),
    ]
    live = _live_source(settings, baku_2025_race, stints, laps)
    await live.open()

    engine = DecisionEngine(settings)
    verdicts = [engine.observe(tick) async for tick in live.ticks()]

    # Fewer than min_samples_for_fit clean laps -> no decision, and crucially
    # no exception.
    assert all(v is None for v in verdicts)
