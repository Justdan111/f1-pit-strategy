"""Confidence bounds on the fitted degradation slope.

The point of these is to separate two things a point estimate cannot: a tyre
that genuinely is not slowing, and data too sparse or noisy to tell.
"""

import random

import pytest

from backend.decision_engine import DecisionEngine
from backend.models import TickMessage
from backend.statistics_helpers import t_critical_95

TOL = 0.005


def tick(lap, age, compound, duration, out=False):
    return TickMessage(
        lap=lap, driver_number=1, compound=compound, tyre_age=age,
        stint_number=1, lap_duration_s=duration, is_pit_out_lap=out,
    )


def feed(settings, points, compound="SOFT"):
    """Feed (age, duration) pairs and return the final fit."""
    engine = DecisionEngine(settings)
    for i, (age, duration) in enumerate(points):
        engine.observe(tick(i + 1, age, compound, duration))
    return engine._fit(compound)  # noqa: SLF001


# --- the t table ----------------------------------------------------------


def test_t_values_match_the_textbook():
    assert t_critical_95(1) == pytest.approx(12.706)
    assert t_critical_95(10) == pytest.approx(2.228)
    assert t_critical_95(30) == pytest.approx(2.042)


def test_t_approaches_the_normal_for_large_samples():
    assert t_critical_95(10_000) == pytest.approx(1.960)


def test_t_shrinks_monotonically_with_degrees_of_freedom():
    values = [t_critical_95(df) for df in range(1, 40)]
    assert values == sorted(values, reverse=True)


def test_t_needs_at_least_one_degree_of_freedom():
    with pytest.raises(ValueError):
        t_critical_95(0)


# --- the interval ---------------------------------------------------------


def test_a_perfect_fit_has_a_zero_width_interval(raw_settings):
    """No residuals means no uncertainty about the slope."""
    fit = feed(raw_settings, [(n, 90.0 + 0.1 * n) for n in range(10)])
    assert fit is not None
    assert fit.slope_std_error == pytest.approx(0.0, abs=1e-9)
    assert fit.slope_ci_low == pytest.approx(fit.slope_s_per_lap, abs=TOL)
    assert fit.slope_ci_high == pytest.approx(fit.slope_s_per_lap, abs=TOL)
    assert fit.significance == "positive"


def test_the_interval_brackets_the_point_estimate(settings):
    rng = random.Random(7)
    fit = feed(
        settings,
        [(n, 90.0 + 0.1 * n + rng.uniform(-0.3, 0.3)) for n in range(15)],
    )
    assert fit is not None
    assert fit.slope_ci_low < fit.slope_s_per_lap < fit.slope_ci_high


def test_more_samples_narrow_the_interval(settings):
    """The whole reason to report it: uncertainty should fall as data arrives."""
    rng = random.Random(11)
    noise = [rng.uniform(-0.3, 0.3) for _ in range(40)]

    few = feed(settings, [(n, 90.0 + 0.1 * n + noise[n]) for n in range(6)])
    many = feed(settings, [(n, 90.0 + 0.1 * n + noise[n]) for n in range(40)])

    assert few is not None and many is not None
    few_width = few.slope_ci_high - few.slope_ci_low
    many_width = many.slope_ci_high - many.slope_ci_low
    assert many_width < few_width, "interval did not narrow with more data"


def test_noisier_data_widens_the_interval(settings):
    rng = random.Random(3)
    quiet = feed(
        settings, [(n, 90.0 + 0.1 * n + rng.uniform(-0.05, 0.05)) for n in range(20)]
    )
    rng = random.Random(3)
    noisy = feed(
        settings, [(n, 90.0 + 0.1 * n + rng.uniform(-1.5, 1.5)) for n in range(20)]
    )
    assert quiet is not None and noisy is not None
    assert (noisy.slope_ci_high - noisy.slope_ci_low) > (
        quiet.slope_ci_high - quiet.slope_ci_low
    )


# --- the three states -----------------------------------------------------


def test_clear_degradation_is_reported_positive(settings):
    rng = random.Random(5)
    fit = feed(
        settings,
        [(n, 90.0 + 0.15 * n + rng.uniform(-0.1, 0.1)) for n in range(20)],
    )
    assert fit is not None
    assert fit.significance == "positive"
    assert fit.slope_ci_low > 0


def test_clear_negative_slope_is_reported_negative(raw_settings):
    """Fuel burn outweighing wear, confidently."""
    rng = random.Random(5)
    fit = feed(
        raw_settings,
        [(n, 105.0 - 0.1 * n + rng.uniform(-0.1, 0.1)) for n in range(20)],
    )
    assert fit is not None
    assert fit.significance == "negative"
    assert fit.slope_ci_high < 0


def test_noise_around_a_flat_line_is_unclear_not_a_verdict(raw_settings):
    """The case this feature exists for: no trend, but plenty of scatter."""
    rng = random.Random(13)
    fit = feed(raw_settings, [(n, 95.0 + rng.uniform(-1.0, 1.0)) for n in range(20)])
    assert fit is not None
    assert fit.significance == "unclear"
    assert fit.slope_ci_low <= 0 <= fit.slope_ci_high


def test_three_samples_are_not_enough_to_be_confident(raw_settings):
    """A real slope on the minimum sample count must still read as unclear.

    With 3 points there is 1 degree of freedom and t is 12.7, so unless the
    fit is near-perfect the interval swamps the estimate. Reporting those
    three laps as a measurement would be the exact overconfidence this is
    meant to prevent.
    """
    fit = feed(raw_settings, [(0, 90.0), (1, 90.3), (2, 90.1)])
    assert fit is not None
    assert fit.samples_used == 3
    assert fit.significance == "unclear"


# --- what reaches the client ----------------------------------------------


def test_decision_carries_the_interval_and_a_break_even_range(settings):
    engine = DecisionEngine(settings)
    rng = random.Random(21)
    decision = None
    for n in range(20):
        decision = engine.observe(
            tick(n + 1, n, "SOFT", 90.0 + 0.12 * n + rng.uniform(-0.1, 0.1))
        )

    assert decision is not None
    assert decision.degradation_significance == "positive"
    assert (
        decision.slope_ci_low_s_per_lap
        < decision.current_compound_degradation_s_per_lap
        < decision.slope_ci_high_s_per_lap
    )
    # A steeper slope pays a stop back sooner, so the low end of the range
    # comes from the high end of the slope.
    assert decision.laps_to_break_even_low is not None
    assert decision.laps_to_break_even_high is not None
    assert (
        decision.laps_to_break_even_low
        <= decision.laps_to_break_even
        <= decision.laps_to_break_even_high
    )


def test_an_unclear_slope_reports_an_unbounded_payback(raw_settings):
    """If the tyre might not be slowing, the stop might never pay back."""
    engine = DecisionEngine(raw_settings)
    rng = random.Random(4)
    decision = None
    for n in range(20):
        decision = engine.observe(
            tick(n + 1, n, "SOFT", 95.0 + 0.01 * n + rng.uniform(-1.0, 1.0))
        )

    assert decision is not None
    assert decision.degradation_significance == "unclear"
    assert decision.laps_to_break_even_high is None, (
        "reported a finite worst-case payback on a slope that spans zero"
    )


def test_an_unclear_slope_says_so_in_the_note(raw_settings):
    engine = DecisionEngine(raw_settings)
    rng = random.Random(4)
    decision = None
    for n in range(20):
        decision = engine.observe(
            tick(n + 1, n, "SOFT", 95.0 + 0.01 * n + rng.uniform(-1.0, 1.0))
        )
    assert decision is not None
    assert decision.note is not None
    assert "not distinguishable from zero" in decision.note


def test_the_verdict_itself_is_unchanged_by_the_new_fields(settings):
    """Confidence bounds inform; they must not silently move the decision."""
    engine = DecisionEngine(settings)
    decision = None
    for n in range(12):
        decision = engine.observe(tick(n + 1, n, "SOFT", 90.0 + 0.1 * n))
    assert decision is not None
    # Same one-lap rule as before: delta_s > 0 means pit.
    assert decision.verdict == ("pit_now" if decision.delta_s > 0 else "stay_out")
