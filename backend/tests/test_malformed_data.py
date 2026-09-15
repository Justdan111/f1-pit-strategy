"""Malformed and partial OpenF1 data must degrade, not crash.

Live data is mid-flight: a lap exists before it has a duration, a stint can
be briefly inconsistent, a retirement leaves records half-written. A stream
that dies on one bad row dies during the race.
"""

import httpx
import pytest

from backend.models import Lap, Stint
from backend.openf1_client import (
    OpenF1BadResponse,
    OpenF1Client,
    OpenF1RateLimited,
    OpenF1Unavailable,
    RateLimiter,
)
from backend.tick_builder import MAX_PLAUSIBLE_STINT_LAPS, flatten_to_ticks


def _client(handler, settings) -> OpenF1Client:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    # Zero-interval limiter: rate limiting is tested elsewhere, and real
    # sleeps here would only make this suite slow.
    return OpenF1Client(http, settings, rate_limiter=RateLimiter(0.0))


# --- the tick builder -----------------------------------------------------


def test_lap_with_no_timing_still_produces_a_tick():
    """The car ran that lap. Only the degradation fit loses a point."""
    stints = [Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=3,
                    compound="HARD", tyre_age_at_start=0)]
    laps = [Lap(driver_number=1, lap_number=1, lap_duration=95.0)]

    ticks = flatten_to_ticks(stints, laps)
    assert [t.lap for t in ticks] == [1, 2, 3]
    assert ticks[0].lap_duration_s == 95.0
    assert ticks[1].lap_duration_s is None
    assert ticks[2].lap_duration_s is None


def test_stint_with_inverted_lap_range_is_skipped_not_fatal():
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=10, lap_end=2,
              compound="HARD", tyre_age_at_start=0),
        Stint(driver_number=1, stint_number=2, lap_start=1, lap_end=3,
              compound="SOFT", tyre_age_at_start=0),
    ]
    ticks = flatten_to_ticks(stints, [])
    assert [t.lap for t in ticks] == [1, 2, 3]
    assert all(t.compound == "SOFT" for t in ticks)


def test_absurd_lap_end_is_skipped_rather_than_expanded():
    """A corrupt lap_end must not allocate a hundred thousand ticks."""
    stints = [Stint(driver_number=1, stint_number=1, lap_start=1,
                    lap_end=100_000, compound="HARD", tyre_age_at_start=0)]
    assert flatten_to_ticks(stints, []) == []
    # And the boundary is where it claims to be.
    ok = [Stint(driver_number=1, stint_number=1, lap_start=1,
                lap_end=MAX_PLAUSIBLE_STINT_LAPS, compound="HARD",
                tyre_age_at_start=0)]
    assert len(flatten_to_ticks(ok, [])) == MAX_PLAUSIBLE_STINT_LAPS


def test_empty_input_produces_no_ticks_and_no_exception():
    assert flatten_to_ticks([], []) == []


def test_laps_for_a_stint_that_does_not_exist_are_ignored():
    """Timing without matching tyre data cannot become a tick."""
    stints = [Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=2,
                    compound="HARD", tyre_age_at_start=0)]
    laps = [Lap(driver_number=1, lap_number=n, lap_duration=95.0) for n in range(1, 60)]
    ticks = flatten_to_ticks(stints, laps)
    assert [t.lap for t in ticks] == [1, 2]


# --- the HTTP client ------------------------------------------------------


async def test_unparseable_rows_are_dropped_and_good_ones_survive(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"driver_number": 1, "stint_number": 1, "lap_start": 1,
             "lap_end": 20, "compound": "HARD", "tyre_age_at_start": 0},
            {"driver_number": 2, "stint_number": 1, "lap_start": 1,
             "lap_end": None, "compound": "SOFT"},          # retired mid-stint
            {"nonsense": True},                              # not a stint at all
        ])

    stints = await _client(handler, settings).get_stints("9904")
    assert len(stints) == 1
    assert stints[0].driver_number == 1


async def test_unknown_extra_fields_do_not_break_parsing(settings):
    """OpenF1 adding a field must not break us."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "driver_number": 1, "stint_number": 1, "lap_start": 1, "lap_end": 5,
            "compound": "HARD", "tyre_age_at_start": 0,
            "some_new_field_added_next_season": 42,
        }])

    stints = await _client(handler, settings).get_stints("9904")
    assert len(stints) == 1


async def test_non_json_body_raises_a_specific_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(OpenF1BadResponse, match="non-JSON"):
        await _client(handler, settings).get_stints("9904")


async def test_json_object_instead_of_array_raises(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"detail": "unexpected"})

    with pytest.raises(OpenF1BadResponse, match="Expected a JSON array"):
        await _client(handler, settings).get_stints("9904")


async def test_404_no_results_is_an_empty_list_not_an_error(settings):
    """OpenF1 returns 404 for an empty match: an empty result, not a failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No results found."})

    assert await _client(handler, settings).get_stints("9904") == []


async def test_429_raises_rate_limited_with_retry_after(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"},
                              headers={"Retry-After": "17"})

    with pytest.raises(OpenF1RateLimited) as caught:
        await _client(handler, settings).get_stints("9904")
    assert caught.value.retry_after_seconds == 17.0


async def test_500_is_unavailable_not_bad_request(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    with pytest.raises(OpenF1Unavailable):
        await _client(handler, settings).get_stints("9904")


async def test_timeout_is_reported_as_unavailable(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(OpenF1Unavailable, match="timed out"):
        await _client(handler, settings).get_stints("9904")


async def test_session_row_missing_dates_is_dropped(settings):
    """Defaulting a missing date could report a race live when it is not."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"session_key": 1, "session_name": "Race",
             "date_start": "2026-09-26T11:00:00+00:00",
             "date_end": "2026-09-26T13:00:00+00:00"},
            {"session_key": 2, "session_name": "Broken"},
        ])

    sessions = await _client(handler, settings).get_sessions(year=2026)
    assert [s.session_key for s in sessions] == [1]
