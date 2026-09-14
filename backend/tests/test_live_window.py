"""Live-session detection.

DAY4.md is explicit: do NOT loosen "is this session live" so historical data
passes, because that validates the wrong thing and hides a real bug if the
window arithmetic is off. Instead, hold the definition fixed and move the
CLOCK — testing real session metadata at chosen instants, including the exact
30-minute boundaries.

That is only possible because `is_session_live` takes `now` as a parameter
instead of calling datetime.now() itself. If it read the clock internally,
the only way to test a boundary would be to wait for a race.
"""

from datetime import timedelta

import pytest

from backend.live import (
    LATEST_SESSION_KEY,
    LiveTickSource,
    NoLiveSessionError,
    is_session_live,
    live_window,
    next_session_after,
)

from .conftest import FakeOpenF1Client, utc

MARGIN = 30


# --- the window itself ----------------------------------------------------


def test_window_is_thirty_minutes_either_side(baku_2025_race):
    """Real session 11:00-13:00 UTC must give a 10:30-13:30 window."""
    opens, closes = live_window(baku_2025_race, MARGIN)
    assert opens == utc("2025-09-21T10:30:00+00:00")
    assert closes == utc("2025-09-21T13:30:00+00:00")


@pytest.mark.parametrize(
    "when, expected, why",
    [
        ("2025-09-21T09:00:00+00:00", False, "two hours before the window opens"),
        ("2025-09-21T10:29:59+00:00", False, "one second before the window opens"),
        ("2025-09-21T10:30:00+00:00", True, "EXACTLY 30 min before start: live"),
        ("2025-09-21T10:30:01+00:00", True, "just inside the opening edge"),
        ("2025-09-21T11:00:00+00:00", True, "lights out"),
        ("2025-09-21T12:00:00+00:00", True, "mid-race"),
        ("2025-09-21T13:00:00+00:00", True, "chequered flag"),
        ("2025-09-21T13:29:59+00:00", True, "just inside the closing edge"),
        ("2025-09-21T13:30:00+00:00", True, "EXACTLY 30 min after end: still live"),
        ("2025-09-21T13:30:01+00:00", False, "one second after the window closes"),
        ("2025-09-21T15:00:00+00:00", False, "well after"),
        ("2026-09-14T12:00:00+00:00", False, "a year later — today"),
    ],
)
def test_live_window_boundaries(baku_2025_race, when, expected, why):
    """The boundary cases DAY4.md asks for, at one-second resolution.

    Both edges are inclusive. An off-by-one here is exactly the bug that
    would otherwise first appear during a real session, which is the one
    moment it cannot be debugged calmly.
    """
    assert is_session_live(baku_2025_race, utc(when), MARGIN) is expected, why


def test_a_real_past_session_is_not_live_today(baku_2025_race):
    """The guard against the temptation DAY4.md warns about.

    If someone later widens the margin to make replay data pass as live, this
    fails. It is deliberately phrased in terms of the real Baku 2025 race and
    a plausible "today", because that is precisely the shortcut that would be
    taken to get a green test before the 24th.
    """
    assert not is_session_live(baku_2025_race, utc("2026-09-14T12:00:00+00:00"), MARGIN)


def test_upcoming_azerbaijan_2026_is_not_live_yet(baku_2026_practice_1):
    """The actual target session is correctly NOT live before the 24th."""
    assert not is_session_live(
        baku_2026_practice_1, utc("2026-09-14T12:00:00+00:00"), MARGIN
    )


def test_upcoming_azerbaijan_2026_goes_live_on_the_day(baku_2026_practice_1):
    """...and IS live inside its real window on 2026-09-24.

    FP1 runs 08:30-09:30 UTC, so live data opens at 08:00.
    """
    assert is_session_live(
        baku_2026_practice_1, utc("2026-09-24T08:00:00+00:00"), MARGIN
    )
    assert is_session_live(
        baku_2026_practice_1, utc("2026-09-24T09:00:00+00:00"), MARGIN
    )
    assert not is_session_live(
        baku_2026_practice_1, utc("2026-09-24T10:00:01+00:00"), MARGIN
    )


def test_cancelled_session_is_never_live(baku_2025_race):
    """A cancelled session is not live even mid-window."""
    cancelled = baku_2025_race.model_copy(update={"is_cancelled": True})
    assert not is_session_live(cancelled, utc("2025-09-21T12:00:00+00:00"), MARGIN)


def test_naive_datetime_is_rejected_with_a_readable_error(baku_2025_race):
    """A naive `now` must fail loudly, not compare wrongly."""
    naive = utc("2025-09-21T12:00:00+00:00").replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        is_session_live(baku_2025_race, naive, MARGIN)


# --- next-session lookup --------------------------------------------------


def test_next_session_picks_the_soonest_future_one(
    baku_2025_race, baku_2026_practice_1
):
    now = utc("2026-09-14T12:00:00+00:00")
    later = baku_2026_practice_1.model_copy(
        update={
            "session_key": 11377,
            "session_name": "Race",
            "date_start": utc("2026-09-26T11:00:00+00:00"),
            "date_end": utc("2026-09-26T13:00:00+00:00"),
        }
    )
    picked = next_session_after([later, baku_2025_race, baku_2026_practice_1], now)
    assert picked is not None
    assert picked.session_key == 11370  # FP1 on the 24th, not the race on the 26th


def test_next_session_is_none_when_nothing_is_scheduled(baku_2025_race):
    assert next_session_after([baku_2025_race], utc("2026-09-14T12:00:00+00:00")) is None


# --- LiveTickSource.open() ------------------------------------------------


async def test_open_reports_no_live_session_with_the_next_one(
    settings, baku_2025_race, baku_2026_practice_1
):
    """The state that will be true every time this is run before the 24th.

    It must be a NoLiveSessionError carrying the next session — not a generic
    failure — and the message must be specific enough to act on.
    """
    client = FakeOpenF1Client(sessions=[baku_2025_race, baku_2026_practice_1])
    source = LiveTickSource(
        client=client,
        session_key=LATEST_SESSION_KEY,
        settings=settings,
        clock=lambda: utc("2026-09-14T12:00:00+00:00"),
    )

    with pytest.raises(NoLiveSessionError) as caught:
        await source.open()

    error = caught.value
    assert error.code == "no_live_session"
    assert "No live session right now" in error.detail
    assert error.next_session is not None
    assert error.next_session.session_key == 11370
    assert "2026-09-24T08:30" in error.detail


async def test_open_succeeds_inside_a_real_window(settings, baku_2025_race):
    """Same code path, clock moved inside the real Baku 2025 window.

    Note what is NOT relaxed to make this pass: the margin is the production
    30 minutes and the session is the genuine one. Only `now` changes.
    """
    client = FakeOpenF1Client(sessions=[baku_2025_race])
    source = LiveTickSource(
        client=client,
        session_key="9904",
        driver_number=1,
        settings=settings,
        clock=lambda: utc("2025-09-21T12:00:00+00:00"),
    )

    start = await source.open()
    assert start.source == "live"
    assert start.session_key == "9904"
    # Always None in live mode: a race in progress has no known total.
    assert start.total_laps is None


async def test_open_when_openf1_knows_no_such_session(settings):
    client = FakeOpenF1Client(sessions=[])
    source = LiveTickSource(
        client=client, session_key="does-not-exist", settings=settings,
        clock=lambda: utc("2026-09-14T12:00:00+00:00"),
    )
    with pytest.raises(NoLiveSessionError, match="no session"):
        await source.open()


async def test_next_session_lookup_failure_still_answers(settings, baku_2025_race):
    """A failed 'what's next' lookup must not turn the expected case into an error."""
    from backend.openf1_client import OpenF1Unavailable

    class FlakyLookup(FakeOpenF1Client):
        async def get_sessions(self, **kwargs):
            self.calls.append(("get_sessions", (), kwargs))
            if "year" in kwargs:
                raise OpenF1Unavailable("upstream down")
            return list(self.sessions)

    client = FlakyLookup(sessions=[baku_2025_race])
    source = LiveTickSource(
        client=client, session_key=LATEST_SESSION_KEY, settings=settings,
        clock=lambda: utc("2026-09-14T12:00:00+00:00"),
    )
    with pytest.raises(NoLiveSessionError) as caught:
        await source.open()
    assert caught.value.next_session is None
    assert "No live session right now" in caught.value.detail
