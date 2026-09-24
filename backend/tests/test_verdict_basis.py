"""The verdict compares over the remaining race, not over one lap.

A one-lap comparison can only favour stopping when the tyre loses more than the
entire pit cost in a single lap, which no real tyre approaches -- so a verdict
built on it is structurally incapable of ever saying "pit". These tests pin down
the replacement and the fallback.
"""

import pytest

from backend.config import Settings
from backend.decision_engine import DecisionEngine
from backend.models import TickMessage


def tick(lap, age, duration, compound="SOFT"):
    return TickMessage(
        lap=lap, driver_number=1, compound=compound, tyre_age=age,
        stint_number=1, lap_duration_s=duration, is_pit_out_lap=False,
    )


def run(settings, *, total_laps, slope=0.10, laps=20, start_lap=1):
    """Feed a clean degrading stint and return the last decision."""
    engine = DecisionEngine(settings, total_laps=total_laps)
    decision = None
    for i in range(laps):
        lap = start_lap + i
        decision = engine.observe(tick(lap, i, 95.0 + slope * i))
    return decision


# --- the new basis --------------------------------------------------------


def test_verdict_uses_the_remaining_race_when_the_distance_is_known(raw_settings):
    d = run(raw_settings, total_laps=60)
    assert d is not None
    assert d.verdict_basis == "race_remaining"
    assert d.laps_remaining == 60 - d.lap
    assert d.net_gain_s is not None


def test_net_gain_is_advantage_times_laps_remaining_minus_pit_cost(raw_settings):
    d = run(raw_settings, total_laps=60)
    assert d is not None
    expected = (
        d.fresh_tyre_advantage_s_per_lap * d.laps_remaining - d.pit_lane_cost_s
    )
    assert d.net_gain_s == pytest.approx(expected, abs=0.02)


def test_the_verdict_follows_the_net_gain(raw_settings):
    d = run(raw_settings, total_laps=60)
    assert d is not None
    significant = d.degradation_significance == "positive"
    assert d.verdict == ("pit_now" if d.net_gain_s > 0 and significant else "stay_out")


def test_a_worn_tyre_with_a_long_race_left_says_pit(raw_settings):
    """The case the old rule could never reach.

    Slope 0.10, age 19 -> 1.9 s/lap advantage. With 40 laps left that is 76s
    against a 22s stop, so stopping is clearly right.
    """
    d = run(raw_settings, total_laps=60, slope=0.10, laps=20)
    assert d is not None
    assert d.laps_remaining == 40
    assert d.net_gain_s > 0
    assert d.verdict == "pit_now"


def test_the_same_tyre_late_in_the_race_says_stay_out(raw_settings):
    """Two laps left cannot repay a 22-second stop, however worn the tyre."""
    d = run(raw_settings, total_laps=21, slope=0.10, laps=20)
    assert d is not None
    assert d.laps_remaining == 1
    assert d.net_gain_s < 0
    assert d.verdict == "stay_out"


def test_a_tyre_that_is_not_degrading_never_says_pit(raw_settings):
    d = run(raw_settings, total_laps=60, slope=0.0, laps=20)
    assert d is not None
    assert d.verdict == "stay_out"
    assert d.laps_to_break_even is None


def test_the_one_lap_delta_is_still_reported(raw_settings):
    """Kept for transparency, and to show why a verdict on it was useless."""
    d = run(raw_settings, total_laps=60, slope=0.10, laps=20)
    assert d is not None
    assert d.delta_s < 0, "the one-lap comparison should still disfavour stopping"
    assert d.verdict == "pit_now", "yet stopping is right over the remaining race"


# --- the fallback ---------------------------------------------------------


def test_without_a_race_distance_it_falls_back_and_says_so(raw_settings):
    """Live mode: OpenF1 reports no lap count for a session in progress."""
    d = run(raw_settings, total_laps=None, slope=0.10, laps=20)
    assert d is not None
    assert d.verdict_basis == "next_lap_only"
    assert d.laps_remaining is None
    assert d.net_gain_s is None
    assert d.note is not None and "race distance is unknown" in d.note


def test_the_fallback_matches_the_old_one_lap_rule(raw_settings):
    d = run(raw_settings, total_laps=None, slope=0.10, laps=20)
    assert d is not None
    significant = d.degradation_significance == "positive"
    assert d.verdict == ("pit_now" if d.delta_s > 0 and significant else "stay_out")


def test_a_supplied_distance_restores_an_actionable_verdict(raw_settings):
    """Same data, distance supplied: the verdict becomes usable."""
    without = run(raw_settings, total_laps=None, slope=0.10, laps=20)
    with_it = run(raw_settings, total_laps=60, slope=0.10, laps=20)
    assert without.verdict == "stay_out"
    assert with_it.verdict == "pit_now"
    assert with_it.verdict_basis == "race_remaining"


# --- notes compose --------------------------------------------------------


def test_the_basis_caveat_does_not_hide_the_degradation_caveat(raw_settings):
    """Two separate concerns; a reader needs both.

    An earlier version let the basis note replace the fit note, hiding the
    more informative of the two.
    """
    engine = DecisionEngine(raw_settings, total_laps=None)
    decision = None
    for i in range(12):
        # Falling lap times: no measurable degradation.
        decision = engine.observe(tick(i + 1, i, 100.0 - 0.1 * i))
    assert decision is not None
    assert decision.note is not None
    assert "race distance is unknown" in decision.note
    assert "No measurable degradation" in decision.note


# --- the sample race end to end -------------------------------------------


async def test_the_sample_race_now_produces_a_pit_recommendation(settings):
    """The user-visible symptom: the fixture used to say stay_out on every lap."""
    from backend.replay import ReplayTickSource

    source = ReplayTickSource.from_sample(tick_interval_seconds=0, settings=settings)
    start = await source.open()
    engine = DecisionEngine(settings, total_laps=start.total_laps)

    verdicts = []
    async for t in source.ticks():
        d = engine.observe(t)
        if d is not None:
            verdicts.append(d.verdict)

    assert "pit_now" in verdicts, "the fixture still never recommends a stop"
    assert "stay_out" in verdicts, "a verdict that always says pit is no better"
