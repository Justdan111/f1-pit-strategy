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

import random

from .models import Lap, Stint

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


# --- Synthetic lap times (Day 2) --------------------------------------------
#
# The Day 1 fixture had stint metadata but no lap times, so it could not
# exercise a degradation curve at all. These numbers fix that.
#
# Per compound: a base lap time on a brand-new set, and a degradation slope
# in seconds lost per lap of tyre age. Softer compound = faster when new,
# degrades quicker. That ordering is the whole point — it is what makes the
# engine's per-compound fits distinguishable from each other.
#
# DELIBERATE SIMPLIFICATION: no fuel-burn effect. Real lap times get faster
# through a stint as the car sheds fuel (roughly -0.03 s/lap), which on real
# Baku data was enough to make a HARD tyre's measured degradation come out
# NEGATIVE. That is realistic, and it is exactly why the fixture excludes it:
# the fixture's job is to be a clean, hand-checkable test of the degradation
# maths, not a simulation of every confound. The real confound is documented
# where it belongs — in the decision engine, which has to survive it.

COMPOUND_PROFILES: dict[str, tuple[float, float]] = {
    # compound: (base lap time on a fresh set in seconds, degradation s/lap)
    "SOFT": (89.0, 0.110),
    "MEDIUM": (89.8, 0.070),
    "HARD": (90.7, 0.042),
}

# Contamination, reproducing what real data actually contains. Measured on
# Baku 2025 (session_key 9904, car #1): standing start +25.7s, in-lap +5.4s,
# out-lap +18.1s. Including these offline means the decision engine's fit
# hygiene is exercised by the fixture, with no network needed.
STANDING_START_PENALTY_S = 25.0
IN_LAP_PENALTY_S = 5.5
OUT_LAP_PENALTY_S = 18.0

# Small deterministic scatter, so the fit is not suspiciously perfect and
# r-squared means something. Seeded: the fixture must produce byte-identical
# numbers on every run, or "it changed" stops being evidence of a bug.
LAP_TIME_NOISE_S = 0.15
NOISE_SEED = 20260914


def sample_laps() -> list[Lap]:
    """Lap times for the sample race, one per lap, matching sample_stints().

    Returned as `Lap` models — the same type the OpenF1 client returns — so
    ReplayTickSource merges fixture data and real data through one identical
    code path. If the fixture returned some bespoke shape, the merge logic
    used in development would not be the merge logic used in production.
    """
    rng = random.Random(NOISE_SEED)
    stints = sample_stints()
    laps: list[Lap] = []

    for stint in stints:
        base, degradation = COMPOUND_PROFILES[stint.compound]
        is_first_stint = stint.stint_number == 1
        is_last_stint = stint.stint_number == len(stints)

        for lap_number in range(stint.lap_start, stint.lap_end + 1):
            tyre_age = stint.tyre_age_at_start + (lap_number - stint.lap_start)

            # The clean, physical part: lap time rises linearly with tyre age.
            duration = base + degradation * tyre_age
            duration += rng.uniform(-LAP_TIME_NOISE_S, LAP_TIME_NOISE_S)

            # Then the contamination a real feed would carry.
            is_out_lap = lap_number == stint.lap_start and not is_first_stint
            is_in_lap = lap_number == stint.lap_end and not is_last_stint

            if is_first_stint and lap_number == stint.lap_start:
                duration += STANDING_START_PENALTY_S
            if is_out_lap:
                duration += OUT_LAP_PENALTY_S
            if is_in_lap:
                duration += IN_LAP_PENALTY_S

            laps.append(
                Lap(
                    driver_number=stint.driver_number,
                    lap_number=lap_number,
                    lap_duration=round(duration, 3),
                    is_pit_out_lap=is_out_lap,
                )
            )

    return laps
