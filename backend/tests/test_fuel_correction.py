"""Fuel-burn correction.

A car sheds fuel through a race and gets faster for reasons unrelated to
tyres. Within a stint, fuel load and tyre age are perfectly collinear, so
regression alone can never separate them -- the fuel term has to come from
outside the data. These tests pin down what that correction does and, just as
importantly, what it must not disturb.
"""

import pytest

from backend.config import Settings
from backend.decision_engine import DecisionEngine
from backend.models import TickMessage

TOL = 0.005


def tick(lap, age, compound, duration, stint=1):
    return TickMessage(
        lap=lap, driver_number=1, compound=compound, tyre_age=age,
        stint_number=stint, lap_duration_s=duration, is_pit_out_lap=False,
    )


def run(settings, points, total_laps=None, compound="SOFT"):
    engine = DecisionEngine(settings, total_laps=total_laps)
    decision = None
    for lap, age, duration in points:
        decision = engine.observe(tick(lap, age, compound, duration))
    return engine, decision


# --- the size of the correction -------------------------------------------


def test_correction_is_fuel_effect_times_burn_per_lap(settings):
    """0.03 s/kg x (110 kg / 57 laps) = 0.0579 s/lap."""
    engine = DecisionEngine(settings, total_laps=57)
    assert engine.fuel_correction_s_per_lap == pytest.approx(0.0579, abs=1e-4)


def test_a_shorter_race_gets_a_larger_correction(settings):
    """Same fuel over fewer laps means a faster burn."""
    short = DecisionEngine(settings, total_laps=44).fuel_correction_s_per_lap
    long = DecisionEngine(settings, total_laps=78).fuel_correction_s_per_lap
    assert short > long
    assert short == pytest.approx(0.0750, abs=1e-4)
    assert long == pytest.approx(0.0423, abs=1e-4)


def test_unknown_race_length_falls_back_to_the_assumed_distance(settings):
    """Live mode: OpenF1 reports no lap count for a session in progress."""
    unknown = DecisionEngine(settings, total_laps=None)
    assumed = DecisionEngine(settings, total_laps=settings.assumed_race_laps)
    assert unknown.fuel_correction_s_per_lap == assumed.fuel_correction_s_per_lap


def test_correction_can_be_turned_off(raw_settings):
    assert DecisionEngine(raw_settings, total_laps=57).fuel_correction_s_per_lap == 0.0


# --- what it does to the fit ----------------------------------------------


def test_a_flat_stint_is_revealed_as_real_degradation(settings):
    """The case that motivated this.

    Lap times perfectly flat across a stint does NOT mean the tyre is holding
    up: the car is also getting lighter. Removing the fuel effect shows the
    tyre is in fact losing time.
    """
    points = [(lap, lap - 1, 95.0) for lap in range(1, 21)]
    engine, decision = run(settings, points, total_laps=57)

    assert decision is not None
    assert decision.raw_degradation_s_per_lap == pytest.approx(0.0, abs=TOL)
    assert decision.current_compound_degradation_s_per_lap == pytest.approx(
        engine.fuel_correction_s_per_lap, abs=TOL
    )
    assert decision.degradation_significance == "positive"


def test_the_correction_shifts_the_slope_by_exactly_its_own_size(settings):
    """Within a stint the correction is a constant shift, not a reshaping."""
    points = [(lap, lap - 1, 95.0 + 0.08 * (lap - 1)) for lap in range(1, 21)]
    _, corrected = run(settings, points, total_laps=57)
    _, raw = run(Settings(fuel_correction_enabled=False), points, total_laps=57)

    assert corrected is not None and raw is not None
    shift = (
        corrected.current_compound_degradation_s_per_lap
        - raw.current_compound_degradation_s_per_lap
    )
    assert shift == pytest.approx(corrected.fuel_correction_s_per_lap, abs=TOL)
    assert corrected.raw_degradation_s_per_lap == pytest.approx(
        raw.current_compound_degradation_s_per_lap, abs=TOL
    )


def test_a_genuinely_improving_tyre_still_reads_negative(settings):
    """The correction must not turn every stint positive.

    If it did, it would be manufacturing degradation rather than removing a
    known bias.
    """
    # Falling far faster than fuel burn alone could explain.
    points = [(lap, lap - 1, 95.0 - 0.30 * (lap - 1)) for lap in range(1, 21)]
    _, decision = run(settings, points, total_laps=57)
    assert decision is not None
    assert decision.current_compound_degradation_s_per_lap < 0
    assert decision.degradation_significance == "negative"


# --- what it must NOT disturb ---------------------------------------------


def test_projected_times_remain_real_lap_times(settings):
    """The fit is fuel-free, but what is reported must be comparable to a real lap.

    If projections were fuel-free the numbers would be seconds adrift of
    anything observable, and the backtest's accuracy figures meaningless.
    """
    points = [(lap, lap - 1, 95.0 + 0.08 * (lap - 1)) for lap in range(1, 21)]
    _, decision = run(settings, points, total_laps=57)

    assert decision is not None
    # Lap 21 on this line is about 96.6s; a fuel-free projection would be
    # roughly a second higher.
    assert decision.projected_time_current_tyres_s == pytest.approx(96.6, abs=0.3)


def test_the_fuel_term_cancels_out_of_the_pit_stay_comparison(settings):
    """Both options run the same lap with the same fuel, so delta must not move.

    delta = slope * age - pit_cost, and only the slope should change. If the
    fuel term leaked into the comparison the verdict would depend on how full
    the tank is, which is nonsense.
    """
    points = [(lap, lap - 1, 95.0 + 0.08 * (lap - 1)) for lap in range(1, 21)]
    _, decision = run(settings, points, total_laps=57)

    assert decision is not None
    expected_delta = (
        decision.current_compound_degradation_s_per_lap * decision.tyre_age
        - decision.pit_lane_cost_s
    )
    assert decision.delta_s == pytest.approx(expected_delta, abs=0.01)


def test_break_even_uses_the_corrected_slope(settings):
    points = [(lap, lap - 1, 95.0 + 0.08 * (lap - 1)) for lap in range(1, 21)]
    _, decision = run(settings, points, total_laps=57)

    assert decision is not None
    assert decision.laps_to_break_even is not None
    expected = decision.pit_lane_cost_s / (
        decision.current_compound_degradation_s_per_lap * decision.tyre_age
    )
    assert decision.laps_to_break_even == pytest.approx(expected, abs=0.05)


def test_both_slopes_are_reported_so_the_correction_is_auditable(settings):
    """A silent adjustment is indistinguishable from a bug."""
    points = [(lap, lap - 1, 95.0 + 0.08 * (lap - 1)) for lap in range(1, 21)]
    _, decision = run(settings, points, total_laps=57)

    assert decision is not None
    assert decision.fuel_correction_s_per_lap > 0
    assert decision.current_compound_degradation_s_per_lap == pytest.approx(
        decision.raw_degradation_s_per_lap + decision.fuel_correction_s_per_lap,
        abs=TOL,
    )


def test_with_correction_disabled_the_two_slopes_agree(raw_settings):
    points = [(lap, lap - 1, 95.0 + 0.08 * (lap - 1)) for lap in range(1, 21)]
    _, decision = run(raw_settings, points, total_laps=57)

    assert decision is not None
    assert decision.fuel_correction_s_per_lap == 0.0
    assert decision.raw_degradation_s_per_lap == pytest.approx(
        decision.current_compound_degradation_s_per_lap, abs=1e-9
    )


# --- the fixture demonstrates the whole problem offline -------------------


async def test_the_sample_fixture_recovers_its_own_degradation(settings):
    """End to end on the offline fixture, with no network.

    The fixture models fuel burn, so this checks the correction against a
    known truth: the HARD stint's raw slope is negative -- the tyre appears
    to be getting faster -- while its real degradation is positive. That is
    the exact effect seen on real Baku data, reproducible offline.
    """
    from backend.replay import ReplayTickSource
    from backend.sample_data import COMPOUND_PROFILES

    source = ReplayTickSource.from_sample(tick_interval_seconds=0, settings=settings)
    start = await source.open()
    engine = DecisionEngine(settings, total_laps=start.total_laps)

    latest: dict[str, object] = {}
    async for t in source.ticks():
        decision = engine.observe(t)
        if decision is not None:
            latest[t.compound] = decision

    assert set(latest) == {"SOFT", "MEDIUM", "HARD"}
    for compound, decision in latest.items():
        true_slope = COMPOUND_PROFILES[compound][1]
        assert decision.current_compound_degradation_s_per_lap == pytest.approx(
            true_slope, abs=0.02
        ), f"{compound}: corrected slope missed the fixture's true degradation"

    # The point of the exercise: uncorrected, the hard tyre reads as improving.
    assert latest["HARD"].raw_degradation_s_per_lap < 0
    assert latest["HARD"].current_compound_degradation_s_per_lap > 0
