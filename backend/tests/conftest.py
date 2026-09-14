"""Shared test fixtures.

The guiding rule for this suite: NO NETWORK. Every test runs against
in-memory fakes, so the suite is deterministic, fast, and does not fail
because somebody else's API is down or rate-limits CI.

The data the fakes return is real, though — session timings and stint shapes
copied from actual OpenF1 responses (verified 2026-09-14), so the tests
exercise the shapes production will meet rather than idealised ones.
"""

from datetime import datetime, timezone

import pytest

from backend.config import Settings
from backend.models import Lap, Session, Stint


@pytest.fixture
def settings() -> Settings:
    """Default settings, constructed explicitly.

    Not get_settings(), which is an lru_cache'd singleton reading the
    environment — a developer's stray F1_* variable must not change what the
    tests assert.
    """
    return Settings()


def utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# --- real session metadata, copied from live OpenF1 responses -------------

@pytest.fixture
def baku_2025_race() -> Session:
    """Azerbaijan GP 2025 race. A real, finished session.

    Verified against GET /v1/sessions?session_key=9904 on 2026-09-14:
    11:00:00+00:00 to 13:00:00+00:00. With a 30-minute margin its live window
    is therefore 10:30 to 13:30 — the numbers the boundary tests assert.
    """
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
    """Azerbaijan 2026 FP1 — the real session this project is aimed at.

    Verified against GET /v1/sessions?year=2026&country_name=Azerbaijan:
    session_key 11370, 2026-09-24T08:30 to 09:30 UTC.
    """
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
    """Two stints for car #1, shaped like the real Baku 2025 response.

    Stint 2 starts at tyre_age_at_start=4 because the real data does — a used
    set. Fixtures that always start at 0 hide the tyre-age bug.
    """
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
    """Stands in for OpenF1Client without touching the network.

    Records every call so tests can assert on request COUNT as well as on
    results — which is how the rate-limit test proves the poll loop is not
    quietly making extra requests.
    """

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
