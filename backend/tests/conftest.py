"""Shared fixtures. No network: every test runs against in-memory fakes.

The data is real, though -- session timings and stint shapes copied from
actual OpenF1 responses -- so tests meet the shapes production will.
"""

from datetime import datetime, timezone

import pytest

from backend.config import Settings
from backend.models import Lap, Session, Stint


@pytest.fixture
def settings() -> Settings:
    """Explicit, not get_settings(): a stray F1_* variable must not change assertions."""
    return Settings()


def utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# --- real session metadata, copied from live OpenF1 responses -------------

@pytest.fixture
def baku_2025_race() -> Session:
    """Real finished session: 11:00-13:00 UTC, so a live window of 10:30-13:30."""
    return Session(
        session_key=9904,
        session_name="Race",
        session_type="Race",
        date_start=utc("2025-09-21T11:00:00+00:00"),
        date_end=utc("2025-09-21T13:00:00+00:00"),
        is_cancelled=False,
        location="Baku",
        country_name="Azerbaijan",
        year=2025,
    )


@pytest.fixture
def baku_2026_practice_1() -> Session:
    """The real session this project is aimed at: 2026-09-24, 08:30-09:30 UTC."""
    return Session(
        session_key=11370,
        session_name="Practice 1",
        session_type="Practice",
        date_start=utc("2026-09-24T08:30:00+00:00"),
        date_end=utc("2026-09-24T09:30:00+00:00"),
        is_cancelled=False,
        location="Baku",
        country_name="Azerbaijan",
        year=2026,
    )


@pytest.fixture
def stints_one_driver() -> list[Stint]:
    """Stint 2 starts on a used set (age 4), as the real data does."""
    return [
        Stint(
            driver_number=1, stint_number=1, lap_start=1, lap_end=3,
            compound="HARD", tyre_age_at_start=0,
        ),
        Stint(
            driver_number=1, stint_number=2, lap_start=4, lap_end=6,
            compound="MEDIUM", tyre_age_at_start=4,
        ),
    ]


@pytest.fixture
def laps_one_driver() -> list[Lap]:
    return [
        Lap(driver_number=1, lap_number=1, lap_duration=95.0, is_pit_out_lap=False),
        Lap(driver_number=1, lap_number=2, lap_duration=94.5, is_pit_out_lap=False),
        Lap(driver_number=1, lap_number=3, lap_duration=96.0, is_pit_out_lap=False),
        Lap(driver_number=1, lap_number=4, lap_duration=99.0, is_pit_out_lap=True),
        Lap(driver_number=1, lap_number=5, lap_duration=93.0, is_pit_out_lap=False),
        Lap(driver_number=1, lap_number=6, lap_duration=93.4, is_pit_out_lap=False),
    ]


class FakeOpenF1Client:
    """Stands in for OpenF1Client. Records calls so tests can assert request counts."""

    def __init__(
        self,
        *,
        sessions: list[Session] | None = None,
        stints: list[Stint] | None = None,
        laps: list[Lap] | None = None,
    ) -> None:
        self.sessions = sessions or []
        self.stints = stints or []
        self.laps = laps or []
        self.calls: list[tuple[str, tuple, dict]] = []
        # Set to an exception to make the next N calls fail.
        self.fail_with: Exception | None = None
        self.fail_times: int = 0

    def _maybe_fail(self) -> None:
        if self.fail_with is not None and self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with

    async def get_sessions(self, **kwargs):
        self.calls.append(("get_sessions", (), kwargs))
        self._maybe_fail()
        return list(self.sessions)

    async def get_stints(self, session_key, driver_number=None):
        self.calls.append(("get_stints", (session_key, driver_number), {}))
        self._maybe_fail()
        return list(self.stints)

    async def get_laps(self, session_key, driver_number=None):
        self.calls.append(("get_laps", (session_key, driver_number), {}))
        self._maybe_fail()
        return list(self.laps)


@pytest.fixture
def fake_client_factory():
    return FakeOpenF1Client
