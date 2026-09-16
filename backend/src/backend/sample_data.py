"""Offline fixture: a fake but plausible 58-lap race, used when no session_key is given."""

import random

from .models import Lap, Stint

SAMPLE_SESSION_KEY = "sample"
SAMPLE_DRIVER_NUMBER = 1

# compound: (base lap time on a fresh set, degradation in seconds per lap)
COMPOUND_PROFILES: dict[str, tuple[float, float]] = {
    "SOFT": (89.0, 0.110),
    "MEDIUM": (89.8, 0.070),
    "HARD": (90.7, 0.042),
}

# Contamination matching real data (Baku 2025, car #1), so fit hygiene is exercised offline.
STANDING_START_PENALTY_S = 25.0
IN_LAP_PENALTY_S = 5.5
OUT_LAP_PENALTY_S = 18.0

LAP_TIME_NOISE_S = 0.15
NOISE_SEED = 20260914

# Fuel burn, so the fixture behaves like real data rather than like an
# idealised tyre model. Matches the engine's assumed physics for a 58-lap
# race: 0.03 s/kg x 110 kg / 58 laps. With it present, the engine's fuel
# correction recovers the true degradation slopes above; without it, the
# correction would have nothing to remove and would overstate them.
SAMPLE_RACE_LAPS = 58
FUEL_EFFECT_S_PER_LAP = 0.03 * 110.0 / SAMPLE_RACE_LAPS


def sample_stints() -> list[Stint]:
    """Three stints, two stops. Built fresh each call so callers cannot mutate the fixture."""
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
            # A used set, as OpenF1 really reports: scrubbed in qualifying.
            tyre_age_at_start=2,
        ),
    ]


def sample_laps() -> list[Lap]:
    """Lap times matching sample_stints().

    Returned as `Lap` models so fixture and real data merge through one code
    path. Includes fuel burn, so the engine's correction has something real to
    remove and recovers the stated degradation slopes.
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
            duration = base + degradation * tyre_age
            # The car gets lighter and faster as the race goes on, which is a
            # function of lap number rather than tyre age.
            duration -= FUEL_EFFECT_S_PER_LAP * (lap_number - 1)
            duration += rng.uniform(-LAP_TIME_NOISE_S, LAP_TIME_NOISE_S)

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
