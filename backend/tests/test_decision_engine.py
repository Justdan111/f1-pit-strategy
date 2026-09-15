"""Decision engine scenarios.

Every expected value was computed by hand from the stated line and tyre age
and is hardcoded, so these encode an independent derivation rather than
whatever the code produced. Constants: pit_cost 22.0s, fresh-tyre reference
age 1.0, minimum 3 clean samples.
"""

import pytest

from backend.decision_engine import DecisionEngine
from backend.models import TickMessage

TOL = 0.005


def tick(lap, age, compound, duration, out=False):
    return TickMessage(
        lap=lap, driver_number=1, compound=compound, tyre_age=age,
        stint_number=1, lap_duration_s=duration, is_pit_out_lap=out,
    )


def decide(settings, samples, final):
    engine = DecisionEngine(settings)
    for sample in samples:
        engine.observe(sample)
    return engine.observe(final)


# --- A: nearly-new MEDIUM, lap_time = 90.0 + 0.08 * age, current age 4 ----
#   advantage = 0.08 * 4          = 0.32 s/lap
#   stay      = 90.0 + 0.08 * 5   = 90.40
#   fresh     = 90.0 + 0.08 * 1   = 90.08
#   delta     = 90.40 - 112.08    = -21.68  -> stay_out
#   breakeven = 22 / 0.32         = 68.75 laps

def test_nearly_new_tyre_says_stay_out(settings):
    decision = decide(
        settings,
        [tick(n + 1, n, "MEDIUM", 90.0 + 0.08 * n) for n in range(0, 4)],
        tick(5, 4, "MEDIUM", 90.0 + 0.08 * 4),
    )
    assert decision is not None
    assert decision.verdict == "stay_out"
    assert decision.current_compound_degradation_s_per_lap == pytest.approx(0.08, abs=TOL)
    assert decision.fresh_tyre_advantage_s_per_lap == pytest.approx(0.32, abs=TOL)
    assert decision.projected_time_current_tyres_s == pytest.approx(90.40, abs=TOL)
    assert decision.projected_time_fresh_tyres_s == pytest.approx(90.08, abs=TOL)
    assert decision.delta_s == pytest.approx(-21.68, abs=TOL)
    assert decision.laps_to_break_even == pytest.approx(68.75, abs=TOL)
    assert decision.degradation_is_measurable
    assert decision.fit_r_squared == pytest.approx(1.0, abs=TOL)


# --- B: worn SOFT, 89.0 + 0.11 * age, current age 19 ----------------------
#   advantage = 0.11 * 19 = 2.09 s/lap ; breakeven = 22 / 2.09 = 10.53 laps

def test_worn_tyre_still_says_stay_out_but_break_even_is_near(settings):
    decision = decide(
        settings,
        [tick(40 + n, n, "SOFT", 89.0 + 0.11 * n) for n in (2, 6, 10, 14, 18)],
        tick(58, 19, "SOFT", 89.0 + 0.11 * 19),
    )
    assert decision is not None
    assert decision.verdict == "stay_out"
    assert decision.fresh_tyre_advantage_s_per_lap == pytest.approx(2.09, abs=TOL)
    assert decision.delta_s == pytest.approx(-19.91, abs=TOL)
    assert decision.laps_to_break_even == pytest.approx(10.53, abs=TOL)


# --- C: a tyre falling apart, 95.0 + 1.5 * age, current age 20 ------------
#   advantage = 30.0 s/lap ; delta = 126.50 - 118.50 = +8.00 -> pit_now
# Guards a real bug: an outlier rule keyed to the fastest lap of the stint
# discarded every legitimately degraded lap and emitted no decision at all.

def test_severe_degradation_actually_flips_the_verdict_to_pit(settings):
    decision = decide(
        settings,
        [tick(10 + n, n, "SOFT", 95.0 + 1.5 * n) for n in (10, 14, 18)],
        tick(30, 20, "SOFT", 95.0 + 1.5 * 20),
    )
    assert decision is not None, "engine returned no decision on a heavily worn tyre"
    assert decision.verdict == "pit_now"
    assert decision.fresh_tyre_advantage_s_per_lap == pytest.approx(30.0, abs=TOL)
    assert decision.projected_time_current_tyres_s == pytest.approx(126.50, abs=TOL)
    assert decision.projected_time_fresh_tyres_s == pytest.approx(96.50, abs=TOL)
    assert decision.delta_s == pytest.approx(8.0, abs=TOL)
    assert decision.laps_to_break_even == pytest.approx(0.73, abs=TOL)


# --- D: negative slope (fuel burn outpacing wear), 105.0 - 0.05 * age -----

def test_negative_degradation_is_reported_honestly_not_hidden(settings):
    decision = decide(
        settings,
        [tick(1 + n, n, "HARD", 105.0 - 0.05 * n) for n in (0, 10, 20)],
        tick(30, 25, "HARD", 105.0 - 0.05 * 25),
    )
    assert decision is not None
    assert decision.verdict == "stay_out"
    assert decision.current_compound_degradation_s_per_lap == pytest.approx(-0.05, abs=TOL)
    assert decision.fresh_tyre_advantage_s_per_lap == pytest.approx(-1.25, abs=TOL)
    assert decision.laps_to_break_even is None, (
        "reported a break-even point on a tyre that is not slowing"
    )
    assert decision.degradation_is_measurable is False
    assert decision.note and "fuel burn" in decision.note.lower()


# --- E: not enough data ---------------------------------------------------

def test_no_decision_until_three_clean_samples_exist(settings):
    engine = DecisionEngine(settings)
    results = [
        engine.observe(tick(1, 0, "MEDIUM", 90.00)),
        engine.observe(tick(2, 1, "MEDIUM", 90.08)),
        engine.observe(tick(3, 2, "MEDIUM", 90.16)),
    ]
    assert [r is None for r in results] == [True, True, False]


def test_tick_without_a_lap_time_is_skipped_not_counted(settings):
    """A null lap_duration must not become a sample."""
    engine = DecisionEngine(settings)
    assert engine.observe(tick(1, 0, "MEDIUM", None)) is None
    assert engine.observe(tick(2, 1, "MEDIUM", 90.08)) is None
    assert engine.observe(tick(3, 2, "MEDIUM", 90.16)) is None
    # Still only two usable samples, so still no decision.
    assert engine.observe(tick(4, 3, "MEDIUM", 90.24)) is not None


# --- F: contaminated input ------------------------------------------------
# Guards a real bug: with only the "similar tyre age" rule, a fully
# contaminated neighbourhood let lap 1 survive and invert the fitted slope.

def test_outliers_are_excluded_and_the_true_slope_is_recovered(settings):
    engine = DecisionEngine(settings)
    clean = [tick(n + 1, n, "MEDIUM", 90.0 + 0.10 * n) for n in range(0, 10)]
    noise = [
        tick(90, 0, "MEDIUM", 108.0, out=True),   # out-lap, flagged
        tick(91, 1, "MEDIUM", 150.0),             # safety car
        tick(92, 2, "MEDIUM", 150.0),             # safety car
    ]
    for sample in clean[:-1] + noise:
        engine.observe(sample)
    decision = engine.observe(clean[-1])

    assert decision is not None
    assert decision.current_compound_degradation_s_per_lap == pytest.approx(0.10, abs=TOL)
    assert decision.samples_used == 10
    assert decision.samples_seen == 13
    assert decision.fit_r_squared == pytest.approx(1.0, abs=TOL)


def test_contamination_at_low_tyre_age_does_not_invert_the_slope(settings):
    """Reproduces the real Baku 2025 shape: laps 1-4 all contaminated."""
    engine = DecisionEngine(settings)
    engine.observe(tick(1, 0, "HARD", 129.1))   # standing start
    engine.observe(tick(2, 1, "HARD", 164.2))   # safety car
    engine.observe(tick(3, 2, "HARD", 159.1))   # safety car
    engine.observe(tick(4, 3, "HARD", 192.2))   # safety car
    decision = None
    for n in range(4, 20):
        decision = engine.observe(tick(n + 1, n, "HARD", 105.0 + 0.05 * n))

    assert decision is not None
    assert decision.current_compound_degradation_s_per_lap > 0, (
        "contaminated early laps inverted the fitted slope"
    )
    assert decision.samples_used < decision.samples_seen


def test_all_samples_at_one_tyre_age_cannot_be_fitted(settings):
    """A vertical scatter has no slope — expect None, not a division error."""
    engine = DecisionEngine(settings)
    for n in range(5):
        result = engine.observe(tick(n + 1, 7, "HARD", 95.0 + n * 0.01))
    assert result is None


def test_each_compound_is_fitted_independently(settings):
    """A new compound starts from zero samples, even mid-race."""
    engine = DecisionEngine(settings)
    for n in range(6):
        engine.observe(tick(n + 1, n, "HARD", 95.0 + 0.05 * n))
    # First lap on a different compound: no samples for it yet.
    assert engine.observe(tick(10, 0, "SOFT", 92.0)) is None
    assert engine.observe(tick(11, 1, "SOFT", 92.1)) is None
    assert engine.observe(tick(12, 2, "SOFT", 92.2)) is not None
