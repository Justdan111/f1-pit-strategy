"""An offline, fake-but-plausible race, used when no session_key is given.

Why this exists as a first-class thing rather than a test fixture:

- It makes the whole stack runnable with no network at all. You can develop
  on a plane, and CI never depends on somebody else's API being up.
- It is deterministic. When a tick looks wrong, you know whether the bug is
  in our flattening logic or in the data, because this data never changes.
- It exercises the interesting case. A single-stint race would hide the one
  piece of real logic in ReplayTickSource: resetting tyre age at a pit stop
  while lap number keeps climbing.

The shape: a 58-lap race for car #1, three stints, a two-stop strategy.
Stint 3 deliberately starts on a *used* set (tyre_age_at_start=2) because
OpenF1 really does report that — a set scrubbed in qualifying is not new —
and code that assumes every stint starts at age 0 will be wrong on real data.

These numbers are invented. They are not a real Grand Prix, and nothing here
should be read as real F1 data.
"""

from .models import Stint

SAMPLE_SESSION_KEY = "sample"
SAMPLE_DRIVER_NUMBER = 1


def sample_stints() -> list[Stint]:
    """Return a fresh copy of the sample stints.

    Built on every call rather than exposed as a module-level list, so a
    caller that mutates the result can't corrupt the fixture for everyone
    else in the process.
    """
    return [
        Stint(
            driver_number=SAMPLE_DRIVER_NUMBER,
            stint_number=1,
            lap_start=1,
            lap_end=18,
            compound="MEDIUM",
            tyre_age_at_start=0,
        ),
        Stint(
            driver_number=SAMPLE_DRIVER_NUMBER,
            stint_number=2,
            lap_start=19,
            lap_end=40,
            compound="HARD",
            tyre_age_at_start=0,
        ),
        Stint(
            driver_number=SAMPLE_DRIVER_NUMBER,
            stint_number=3,
            lap_start=41,
            lap_end=58,
            compound="SOFT",
            # A used set: scrubbed in qualifying, so it starts two laps old.
            tyre_age_at_start=2,
        ),
    ]

