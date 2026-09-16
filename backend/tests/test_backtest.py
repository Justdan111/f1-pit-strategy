"""Backtest scoring.

The load-bearing test here is causality: a prediction for lap N+1 must depend
only on laps up to N. If the engine ever fitted on the whole race, every
accuracy number the backtest produces would be meaningless while looking fine.
"""

import pytest

from backend.backtest import (
    CLEAN_LAP_MEDIAN_MARGIN_S,
    Prediction,
    _clean_next_laps,
    _recommended_pit_lap,
    accuracy,
    backtest_session,
    backtest_session_with_retry,
)
from backend.models import DecisionMessage, Lap, Stint, TickMessage
from backend.openf1_client import OpenF1RateLimited

from .conftest import FakeOpenF1Client


def tick(lap, age, compound, duration, stint=1, out=False):
    return TickMessage(
        lap=lap, driver_number=1, compound=compound, tyre_age=age,
        stint_number=stint, lap_duration_s=duration, is_pit_out_lap=out,
    )


def prediction(actual, model, persistence=0.0, mean=0.0, clean=True):
    return Prediction(
        lap=1, compound="HARD", tyre_age=1, actual_s=actual, model_s=model,
        persistence_s=persistence, compound_mean_s=mean, next_lap_is_clean=clean,
    )


# --- error statistics -----------------------------------------------------


def test_accuracy_computes_mae_rmse_and_median():
    # Errors of 1, 2, 3: MAE 2, RMSE sqrt(14/3), median 2.
    samples = [
        prediction(actual=100.0, model=101.0),
        prediction(actual=100.0, model=102.0),
        prediction(actual=100.0, model=103.0),
    ]
    a = accuracy(samples, "model")
    assert a is not None
    assert a.n == 3
    assert a.mae == pytest.approx(2.0)
    assert a.rmse == pytest.approx((14 / 3) ** 0.5)
    assert a.median_ae == pytest.approx(2.0)


def test_accuracy_is_none_for_no_samples():
    assert accuracy([], "model") is None


def test_accuracy_uses_absolute_error_in_both_directions():
    """Over- and under-prediction must count the same."""
    over = accuracy([prediction(actual=100.0, model=102.0)], "model")
    under = accuracy([prediction(actual=100.0, model=98.0)], "model")
    assert over.mae == under.mae == pytest.approx(2.0)


# --- clean-lap classification ---------------------------------------------


def test_out_laps_and_in_laps_are_not_clean():
    ticks = [
        tick(1, 0, "HARD", 95.0, stint=1),
        tick(2, 1, "HARD", 95.1, stint=1),
        tick(3, 2, "HARD", 99.0, stint=1),          # in-lap: last of a non-final stint
        tick(4, 0, "SOFT", 110.0, stint=2, out=True),  # out-lap
        tick(5, 1, "SOFT", 93.0, stint=2),
        tick(6, 2, "SOFT", 93.1, stint=2),
    ]
    clean = _clean_next_laps(ticks)
    assert clean[1] and clean[2]
    assert not clean[3], "in-lap counted as clean"
    assert not clean[4], "out-lap counted as clean"
    assert clean[5] and clean[6]


def test_a_lap_far_slower_than_its_stint_median_is_not_clean():
    """Catches safety cars and traffic."""
    ticks = [tick(n, n - 1, "HARD", 95.0) for n in range(1, 10)]
    ticks.append(tick(10, 9, "HARD", 95.0 + CLEAN_LAP_MEDIAN_MARGIN_S + 1))
    clean = _clean_next_laps(ticks)
    assert not clean[10]


def test_legitimately_degraded_late_laps_stay_clean():
    """A worn tyre is slow for a reason the model claims to explain.

    Guards the Day 2 bug in a different place: a filter keyed to the fastest
    lap would discard exactly the evidence degradation is happening.
    """
    ticks = [tick(n, n - 1, "SOFT", 90.0 + 0.1 * (n - 1)) for n in range(1, 41)]
    clean = _clean_next_laps(ticks)
    assert clean[40], "a 4s-degraded lap was excluded as an anomaly"


def test_lap_with_no_timing_is_not_clean():
    ticks = [tick(1, 0, "HARD", 95.0), tick(2, 1, "HARD", None)]
    assert not _clean_next_laps(ticks)[2]


# --- recommended pit lap --------------------------------------------------


def decision(lap, break_even):
    return DecisionMessage(
        lap=lap, driver_number=1, compound="SOFT", tyre_age=lap,
        verdict="stay_out",
        current_compound_degradation_s_per_lap=0.1, fit_intercept_s=90.0,
        fit_r_squared=0.9, samples_used=5, samples_seen=5,
        projected_time_current_tyres_s=91.0, projected_time_fresh_tyres_s=90.1,
        pit_lane_cost_s=22.0, delta_s=-21.0,
        fresh_tyre_advantage_s_per_lap=1.0, laps_to_break_even=break_even,
    )


def test_recommends_the_first_lap_where_the_stop_pays_back_in_time():
    # 50-lap race. At lap 10, 40 remain; break-even 45 does not fit. At lap
    # 20, 30 remain and break-even 25 does.
    decisions = [decision(10, 45.0), decision(20, 25.0), decision(30, 5.0)]
    assert _recommended_pit_lap(decisions, total_laps=50) == 20


def test_recommends_nothing_when_no_stop_ever_pays_back():
    decisions = [decision(10, 500.0), decision(20, 400.0)]
    assert _recommended_pit_lap(decisions, total_laps=50) is None


def test_decisions_without_a_break_even_are_skipped_not_treated_as_zero():
    """A tyre that is not slowing has no break-even; that must not read as 'pit now'."""
    decisions = [decision(5, None), decision(6, None), decision(7, 10.0)]
    assert _recommended_pit_lap(decisions, total_laps=50) == 7


# --- causality: the test this file exists for -----------------------------


async def _run(settings, stints, laps):
    client = FakeOpenF1Client(stints=stints, laps=laps)
    return await backtest_session(client, "test", driver_number=1, settings=settings)


async def test_predictions_do_not_depend_on_laps_that_come_after_them(settings):
    """Change the end of the race; the early predictions must not move.

    If the engine fitted over the whole race, altering late laps would shift
    early predictions, and every accuracy figure in the report would be
    measuring hindsight rather than forecasting.
    """
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=40,
              compound="HARD", tyre_age_at_start=0)
    ]
    baseline_laps = [
        Lap(driver_number=1, lap_number=n, lap_duration=95.0 + 0.05 * n)
        for n in range(1, 41)
    ]
    # Same race, but the last ten laps run 3s slower.
    #
    # The size matters. A wilder alteration (say +3s PER LAP) would be thrown
    # out by the engine's own outlier filter, so leaking it would change
    # nothing and this test would pass whether or not causality held -- which
    # is exactly what an earlier version of it did. 3s is inside the filter's
    # tolerance, so it genuinely moves the fit.
    altered_laps = [
        Lap(
            driver_number=1,
            lap_number=n,
            lap_duration=(95.0 + 0.05 * n) + (0.0 if n <= 30 else 3.0),
        )
        for n in range(1, 41)
    ]

    baseline = await _run(settings, stints, baseline_laps)
    altered = await _run(settings, stints, altered_laps)

    early_baseline = {p.lap: p.model_s for p in baseline.predictions if p.lap <= 30}
    early_altered = {p.lap: p.model_s for p in altered.predictions if p.lap <= 30}

    assert early_baseline, "no early predictions produced; the test proves nothing"
    assert early_baseline == early_altered, (
        "predictions for early laps changed when only later laps were altered, "
        "so the fit is using data from the future"
    )


async def test_a_prediction_is_scored_against_the_following_lap(settings):
    """The model figure must be compared with lap N+1, not lap N."""
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=12,
              compound="HARD", tyre_age_at_start=0)
    ]
    laps = [
        Lap(driver_number=1, lap_number=n, lap_duration=95.0 + 0.1 * n)
        for n in range(1, 13)
    ]
    result = await _run(settings, stints, laps)

    assert result.predictions
    for p in result.predictions:
        # actual_s is the duration of the lap the prediction is labelled with.
        assert p.actual_s == pytest.approx(95.0 + 0.1 * p.lap)
        # persistence is the lap before it.
        assert p.persistence_s == pytest.approx(95.0 + 0.1 * (p.lap - 1))


async def test_predictions_never_cross_a_pit_stop(settings):
    """A prediction about the current tyres cannot be scored on a new set."""
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=10,
              compound="HARD", tyre_age_at_start=0),
        Stint(driver_number=1, stint_number=2, lap_start=11, lap_end=20,
              compound="SOFT", tyre_age_at_start=0),
    ]
    laps = [
        Lap(driver_number=1, lap_number=n, lap_duration=95.0 + 0.1 * n)
        for n in range(1, 21)
    ]
    result = await _run(settings, stints, laps)

    assert result.predictions
    assert all(p.lap != 11 for p in result.predictions), (
        "scored a prediction across a tyre change"
    )


async def test_a_race_with_too_little_data_is_skipped_not_scored(settings):
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=3,
              compound="HARD", tyre_age_at_start=0)
    ]
    laps = [Lap(driver_number=1, lap_number=n, lap_duration=95.0) for n in (1, 2, 3)]
    result = await _run(settings, stints, laps)
    assert result.skipped_reason is not None
    assert result.predictions == []


async def test_no_stint_data_is_skipped_not_an_exception(settings):
    result = await _run(settings, [], [])
    assert result.skipped_reason == "no stint data"


# --- batch rate-limit handling --------------------------------------------


class RateLimitedThenFine(FakeOpenF1Client):
    """Rate-limits the first `fail_first` calls to get_stints, then behaves."""

    def __init__(self, fail_first: int, **kwargs):
        super().__init__(**kwargs)
        self.remaining = fail_first
        self.attempts = 0

    async def get_stints(self, session_key, driver_number=None):
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise OpenF1RateLimited("slow down", retry_after_seconds=1.0)
        return await super().get_stints(session_key, driver_number)


async def test_a_rate_limited_race_is_retried_not_dropped(settings):
    """A single 429 silently dropping a race would change every aggregate."""
    stints = [
        Stint(driver_number=1, stint_number=1, lap_start=1, lap_end=20,
              compound="HARD", tyre_age_at_start=0)
    ]
    laps = [
        Lap(driver_number=1, lap_number=n, lap_duration=95.0 + 0.1 * n)
        for n in range(1, 21)
    ]
    client = RateLimitedThenFine(fail_first=2, stints=stints, laps=laps)

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    result = await backtest_session_with_retry(
        client, "test", driver_number=1, settings=settings, sleep=fake_sleep
    )

    assert result.skipped_reason is None, "race was dropped instead of retried"
    assert result.predictions, "retried but produced nothing"
    assert client.attempts == 3
    assert len(slept) == 2, "did not back off between attempts"
    assert slept[1] > slept[0], "backoff did not grow"


async def test_persistent_rate_limiting_is_reported_not_silent(settings):
    """Giving up must leave a visible reason, not an empty row."""
    client = RateLimitedThenFine(fail_first=99)

    async def fake_sleep(_seconds):
        return None

    result = await backtest_session_with_retry(
        client, "test", label="Somewhere", settings=settings, sleep=fake_sleep
    )
    assert result.skipped_reason is not None
    assert "rate-limited" in result.skipped_reason
