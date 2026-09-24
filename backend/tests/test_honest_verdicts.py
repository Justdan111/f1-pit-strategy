"""The engine must never recommend a stop it cannot back with real signal.

Both failure modes were found by replaying Baku 2025 (session 9904): every
pit_now it produced was an artefact of one or the other.
"""

import random

from backend.config import Settings
from backend.decision_engine import DecisionEngine
from backend.models import TickMessage

# Leclerc (#16), Baku 2025, stint 1 on MEDIUM, which started on a used set at
# age 3. Laps 1-4 are the standing start and safety car. Lap 19 is the in-lap:
# +3.67s over the pace of laps 14-18, under the old 5.0s filter.
LECLERC_STINT_1 = {
    1: 143.517, 2: 161.332, 3: 158.427, 4: 183.79, 5: 111.174, 6: 107.727,
    7: 107.309, 8: 106.834, 9: 106.773, 10: 107.388, 11: 106.801, 12: 106.47,
    13: 106.442, 14: 106.132, 15: 106.843, 16: 106.56, 17: 106.016,
    18: 106.025, 19: 109.688,
}
LECLERC_IN_LAP = 19
BAKU_2025_LAPS = 51


def tick(lap: int, age: int, duration: float, compound: str = "MEDIUM") -> TickMessage:
    return TickMessage(
        lap=lap, driver_number=16, compound=compound, tyre_age=age,
        stint_number=1, lap_duration_s=duration,
    )


def replay_leclerc(settings: Settings):
    engine = DecisionEngine(settings, total_laps=BAKU_2025_LAPS)
    return {
        lap: engine.observe(tick(lap, lap + 2, duration))
        for lap, duration in LECLERC_STINT_1.items()
    }


# --- fix 2: in-laps stay out of the fit ------------------------------------


def test_leclerc_in_lap_is_excluded_from_the_fit(settings):
    decisions = replay_leclerc(settings)
    before, in_lap = decisions[LECLERC_IN_LAP - 1], decisions[LECLERC_IN_LAP]

    # Seen, but not used: the fit is exactly what it was a lap earlier.
    assert in_lap.samples_seen == before.samples_seen + 1
    assert in_lap.samples_used == before.samples_used
    assert in_lap.current_compound_degradation_s_per_lap <= 0
    assert in_lap.verdict == "stay_out"


def test_the_old_threshold_let_the_in_lap_bend_the_slope(settings):
    """Encodes the bug itself, so the test above cannot pass vacuously."""
    loose = settings.model_copy(update={"max_lap_time_excess_for_fit_s": 5.0})
    decisions = replay_leclerc(loose)
    before, in_lap = decisions[LECLERC_IN_LAP - 1], decisions[LECLERC_IN_LAP]

    assert in_lap.samples_used == before.samples_used + 1
    assert before.current_compound_degradation_s_per_lap < 0
    assert in_lap.current_compound_degradation_s_per_lap > 0


def test_an_in_lap_just_over_the_new_threshold_is_excluded(raw_settings):
    """The gentlest in-lap at Baku 2025 was +2.92s (car #4, lap 37)."""
    engine = DecisionEngine(raw_settings, total_laps=51)
    for age in range(1, 15):
        engine.observe(tick(age, age, 105.0 + 0.02 * age))
    in_lap = engine.observe(tick(15, 15, 105.0 + 0.02 * 14 + 2.92))
    assert in_lap.samples_used == 14


def test_ordinary_lap_noise_is_kept(raw_settings):
    """Green laps sat at a p95 of +1.77s over local pace; those must stay in."""
    engine = DecisionEngine(raw_settings, total_laps=51)
    d = None
    for age in range(1, 16):
        bump = 1.7 if age % 5 == 0 else 0.0
        d = engine.observe(tick(age, age, 105.0 + 0.02 * age + bump))
    assert d.samples_used == d.samples_seen == 15


# --- fix 1: no pit_now without significant degradation ----------------------


def noisy_positive(settings: Settings, *, seed: int, laps: int):
    """A positive trend drowned in noise, with a long race left to repay a stop."""
    rng = random.Random(seed)
    engine = DecisionEngine(settings, total_laps=70)
    d = None
    for age in range(1, laps + 1):
        d = engine.observe(tick(age, age, 100.0 + 0.1 * age + rng.uniform(-2.0, 2.0)))
    return d


def test_a_stop_that_pays_on_the_point_estimate_alone_is_stay_out(raw_settings):
    # Three samples: the Baku car #12 case, an interval of about +/-10 s/lap.
    d = noisy_positive(raw_settings, seed=3, laps=3)
    assert d.net_gain_s > 0, "precondition: the point estimate says stop"
    assert d.degradation_significance != "positive"
    assert d.verdict == "stay_out"
    assert "insufficient signal" in d.note


def test_no_pit_now_ever_rests_on_an_interval_that_reaches_zero(raw_settings):
    for seed in range(200):
        for laps in (3, 5, 8):
            d = noisy_positive(raw_settings, seed=seed, laps=laps)
            if d is not None and d.verdict == "pit_now":
                assert d.slope_ci_low_s_per_lap > 0, (seed, laps)


def test_clear_degradation_still_says_pit(raw_settings):
    """The gate must not turn the engine into one that never recommends a stop."""
    engine = DecisionEngine(raw_settings, total_laps=70)
    d = None
    for age in range(1, 16):
        d = engine.observe(tick(age, age, 100.0 + 0.15 * age))
    assert d.degradation_significance == "positive"
    assert d.verdict == "pit_now"
