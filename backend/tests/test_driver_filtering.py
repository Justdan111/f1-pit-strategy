"""One stream, one car: replay filters by driver the same way live does.

Proven against a fake that behaves like OpenF1's driver_number query param.
The equivalent check against the real API is run by hand before a live test;
see docs/SPEC.md.
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models import Lap, Stint
from backend.replay import ReplayTickSource
from backend.tick_builder import MixedDriverDataError, flatten_to_ticks
from backend.tick_source import NoDataError

from .conftest import FakeOpenF1Client


def _grid() -> FakeOpenF1Client:
    """Two cars on different strategies over the same laps, like Baku 2025."""
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=4,
              compound="HARD", tyre_age_at_start=0),
        Stint(driver_number=1, stint_number=2, lap_start=5, lap_end=6,
              compound="MEDIUM", tyre_age_at_start=4),
        Stint(driver_number=16, stint_number=1, lap_start=1, lap_end=2,
              compound="MEDIUM", tyre_age_at_start=3),
        Stint(driver_number=16, stint_number=2, lap_start=3, lap_end=6,
              compound="HARD", tyre_age_at_start=0),
    ]
    laps = [
        Lap(driver_number=d, lap_number=n, lap_duration=90.0 + d + n / 10)
        for d in (1, 16)
        for n in range(1, 7)
    ]
    return FakeOpenF1Client(stints=stints, laps=laps)


async def _replay(client, driver_number, settings):
    source = ReplayTickSource.from_openf1(
        client=client, session_key="9904", driver_number=driver_number,
        tick_interval_seconds=0, settings=settings,
    )
    start = await source.open()
    return start, [t async for t in source.ticks()]


async def test_each_driver_gets_only_their_own_stints(settings):
    client = _grid()
    start_1, ver = await _replay(client, 1, settings)
    start_16, lec = await _replay(client, 16, settings)

    assert start_1.driver_number == 1 and start_16.driver_number == 16
    assert {t.driver_number for t in ver} == {1}
    assert {t.driver_number for t in lec} == {16}
    assert [t.lap for t in ver] == [1, 2, 3, 4, 5, 6]
    assert [(t.compound, t.tyre_age) for t in ver] == [
        ("HARD", 0), ("HARD", 1), ("HARD", 2), ("HARD", 3),
        ("MEDIUM", 4), ("MEDIUM", 5),
    ]
    assert [(t.compound, t.tyre_age) for t in lec] == [
        ("MEDIUM", 3), ("MEDIUM", 4),
        ("HARD", 0), ("HARD", 1), ("HARD", 2), ("HARD", 3),
    ]
    # Lap times come from the same car as the tyre data.
    assert all(t.lap_duration_s == pytest.approx(91.0 + t.lap / 10) for t in ver)
    assert all(t.lap_duration_s == pytest.approx(106.0 + t.lap / 10) for t in lec)


async def test_replay_filters_at_the_api(settings):
    client = _grid()
    await _replay(client, 16, settings)
    data_calls = [c for c in client.calls if c[0] in ("get_stints", "get_laps")]
    assert data_calls
    assert all(args[1] == 16 for _, args, _ in data_calls)


async def test_replay_refuses_a_driver_not_entered(settings):
    with pytest.raises(NoDataError) as caught:
        await _replay(_grid(), 44, settings)
    assert caught.value.code == "unknown_driver"
    assert "[1, 16]" in caught.value.detail


async def test_sample_refuses_any_driver_but_its_own(settings):
    source = ReplayTickSource.from_sample(
        driver_number=16, tick_interval_seconds=0, settings=settings
    )
    with pytest.raises(NoDataError) as caught:
        await source.open()
    assert caught.value.code == "unknown_driver"


def test_flattener_refuses_mixed_cars():
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=3),
        Stint(driver_number=16, stint_number=1, lap_start=1, lap_end=3),
    ]
    with pytest.raises(MixedDriverDataError):
        flatten_to_ticks(stints, [])


def test_flattener_refuses_laps_from_another_car():
    stints = [Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=3)]
    laps = [Lap(driver_number=16, lap_number=1, lap_duration=95.0)]
    with pytest.raises(MixedDriverDataError):
        flatten_to_ticks(stints, laps)


def test_websocket_without_driver_number_is_refused_in_protocol():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/race/sample?mode=replay") as ws:
            message = ws.receive_json()
    assert message["type"] == "error"
    assert message["code"] == "driver_number_required"


def test_websocket_with_driver_number_streams_that_car():
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/race/sample?mode=replay&driver_number=1&tick_interval=0"
        ) as ws:
            start = ws.receive_json()
            tick = ws.receive_json()
    assert start["type"] == "start" and start["driver_number"] == 1
    assert tick["type"] == "tick" and tick["driver_number"] == 1


def test_sample_drivers_route():
    with TestClient(app) as client:
        response = client.get("/race/sample/drivers")
    assert response.status_code == 200
    assert [d["driver_number"] for d in response.json()] == [1]
