"""Backtest the decision engine against finished races.

Three measurements, which prove very different things:

1. PREDICTION ACCURACY -- how close the fitted curve's next-lap estimate is to
   the lap time that actually happened. Ground truth is a real lap time, so
   this is the only non-circular number here. Reported against two baselines,
   because a model that cannot beat them is not earning its place:
     - persistence: next lap will match this lap
     - compound mean: next lap will match the average so far on this compound

2. COUNTERFACTUAL PIT LAP -- total race time under alternative pit laps,
   using the fitted curves. CIRCULAR: the model being evaluated supplies the
   answer, so it can only show internal consistency, never correctness.

3. GAP TO THE ACTUAL PIT LAP -- distance between what the engine would have
   recommended and what the team did. NOT ground truth: teams also optimise
   track position, which this project deliberately does not model, so a gap
   is not necessarily an error on either side.

Everything is computed incrementally. The engine sees laps in order and each
prediction uses only laps before the one being predicted, so nothing is fitted
on data from the future.
"""

import asyncio
import logging
import statistics
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .decision_engine import DecisionEngine
from .models import DecisionMessage, TickMessage
from .openf1_client import OpenF1Client, OpenF1RateLimited
from .tick_builder import flatten_to_ticks, resolve_driver

logger = logging.getLogger(__name__)


@dataclass
class Prediction:
    """One next-lap prediction and what actually happened."""

    lap: int
    compound: str
    tyre_age: int
    actual_s: float
    model_s: float
    persistence_s: float
    compound_mean_s: float
    next_lap_is_clean: bool

    def error(self, predictor: str) -> float:
        return abs(getattr(self, f"{predictor}_s") - self.actual_s)


@dataclass
class Accuracy:
    """Error statistics for one predictor."""

    predictor: str
    n: int
    mae: float
    rmse: float
    median_ae: float


@dataclass
class Counterfactual:
    """Modelled best pit lap versus the one actually taken. Circular by construction."""

    actual_pit_lap: int
    modelled_best_pit_lap: int
    modelled_saving_s: float
    # True when the optimum landed on the edge of the swept range, which means
    # the model wanted to pit even earlier and was stopped by the boundary.
    # That happens whenever the second compound simply fits faster than the
    # first: with no notion of minimum stint length or of tyres it has no data
    # for, the sweep degenerates to "pit immediately". Such a result says
    # nothing about pit strategy and is excluded from the summary.
    hit_sweep_boundary: bool = False
    note: str = (
        "Uses the same fitted curves being evaluated, so this shows internal "
        "consistency, not correctness."
    )


@dataclass
class RaceResult:
    session_key: str
    label: str
    driver_number: int
    total_laps: int
    predictions: list[Prediction] = field(default_factory=list)
    actual_pit_laps: list[int] = field(default_factory=list)
    recommended_pit_lap: int | None = None
    decisions: int = 0
    decisions_with_measurable_degradation: int = 0
    # How the slope's 95% interval sat relative to zero. Splits the blunt
    # "not measurable" count into a tyre that genuinely is not slowing versus
    # data too sparse or noisy to tell.
    significance_counts: dict[str, int] = field(default_factory=dict)
    counterfactual: Counterfactual | None = None
    skipped_reason: str | None = None


# A next lap is "clean" if it is not distorted by something the degradation
# model does not claim to explain. Stated explicitly rather than tuned:
#   - not an out-lap (starts in the pit lane)
#   - not the last lap of a stint (the in-lap, driving to pit entry)
#   - within CLEAN_LAP_MEDIAN_MARGIN_S of that stint's median lap time, which
#     catches safety cars, traffic and mistakes. Median rather than fastest,
#     so a legitimately degraded late lap is not thrown away.
# Unfiltered numbers are reported alongside, so nothing is hidden by this.
CLEAN_LAP_MEDIAN_MARGIN_S = 5.0


def _clean_next_laps(ticks: list[TickMessage]) -> dict[int, bool]:
    """Which laps are undistorted enough to hold the model to."""
    by_stint: dict[int, list[TickMessage]] = {}
    for tick in ticks:
        by_stint.setdefault(tick.stint_number, []).append(tick)

    clean: dict[int, bool] = {}
    for stint_ticks in by_stint.values():
        timed = [t.lap_duration_s for t in stint_ticks if t.lap_duration_s is not None]
        median = statistics.median(timed) if timed else None
        last_lap = max(t.lap for t in stint_ticks)
        is_final_stint = last_lap == max(t.lap for t in ticks)

        for tick in stint_ticks:
            if tick.lap_duration_s is None or median is None:
                clean[tick.lap] = False
            elif tick.is_pit_out_lap:
                clean[tick.lap] = False
            elif tick.lap == last_lap and not is_final_stint:
                clean[tick.lap] = False
            else:
                clean[tick.lap] = (
                    tick.lap_duration_s - median <= CLEAN_LAP_MEDIAN_MARGIN_S
                )
    return clean


def accuracy(predictions: list[Prediction], predictor: str) -> Accuracy | None:
    """Error statistics for one predictor over a set of predictions."""
    errors = [p.error(predictor) for p in predictions]
    if not errors:
        return None
    return Accuracy(
        predictor=predictor,
        n=len(errors),
        mae=sum(errors) / len(errors),
        rmse=(sum(e * e for e in errors) / len(errors)) ** 0.5,
        median_ae=statistics.median(errors),
    )


def _recommended_pit_lap(
    decisions: list[DecisionMessage], total_laps: int
) -> int | None:
    """First lap where a stop pays for itself before the race ends.

    `total_laps` is legitimate to use here: a race distance is published in
    advance, so a strategist genuinely knows it. It is `None` in the live
    message protocol only because OpenF1 does not report a lap count for a
    session in progress.
    """
    for decision in decisions:
        if decision.laps_to_break_even is None:
            continue
        laps_remaining = total_laps - decision.lap
        if decision.laps_to_break_even <= laps_remaining:
            return decision.lap
    return None


def _counterfactual(
    engine: DecisionEngine,
    ticks: list[TickMessage],
    actual_pit_lap: int,
    settings: Settings,
) -> Counterfactual | None:
    """Sweep pit laps and total the modelled race time for each.

    Only attempted for a one-stop race, where there is a single pit lap to
    vary. The compounds are whichever were actually used.
    """
    stints = sorted({t.stint_number for t in ticks})
    if len(stints) != 2:
        return None

    first = [t for t in ticks if t.stint_number == stints[0]]
    second = [t for t in ticks if t.stint_number == stints[1]]
    compound_a, compound_b = first[0].compound, second[0].compound
    start_age_a, start_age_b = first[0].tyre_age, second[0].tyre_age

    fit_a = engine._fit(compound_a)  # noqa: SLF001 - deliberate: backtest inspects the fit
    fit_b = engine._fit(compound_b)  # noqa: SLF001
    if fit_a is None or fit_b is None:
        return None

    total_laps = max(t.lap for t in ticks)
    pit_cost = settings.pit_lane_cost_seconds

    def total_time(pit_lap: int) -> float:
        first_stint = sum(
            fit_a.predict(start_age_a + (lap - 1)) for lap in range(1, pit_lap + 1)
        )
        second_stint = sum(
            fit_b.predict(start_age_b + (lap - pit_lap - 1))
            for lap in range(pit_lap + 1, total_laps + 1)
        )
        return first_stint + pit_cost + second_stint

    # Leave a few laps either end: pitting on lap 1 or the last lap is not a
    # strategy, and sweeping them only adds noise to the optimum.
    candidates = range(3, total_laps - 2)
    if not candidates:
        return None

    best = min(candidates, key=total_time)
    return Counterfactual(
        actual_pit_lap=actual_pit_lap,
        modelled_best_pit_lap=best,
        modelled_saving_s=total_time(actual_pit_lap) - total_time(best),
        hit_sweep_boundary=best in (candidates[0], candidates[-1]),
    )


# A batch of races is 2 requests each, back to back. The client's 0.4s gap
# keeps us under the 3 req/s ceiling but says nothing about the 30 req/min one,
# and a real 429 from OpenF1 was what surfaced this. Batch work has no latency
# requirement, so it is paced far more slowly than live polling.
BATCH_REQUEST_INTERVAL_SECONDS = 2.5
BATCH_MAX_RETRIES = 4
BATCH_RETRY_BACKOFF_SECONDS = 20.0


async def backtest_session_with_retry(
    client: OpenF1Client,
    session_key: str,
    *,
    driver_number: int | None = None,
    label: str = "",
    settings: Settings | None = None,
    sleep=asyncio.sleep,
) -> RaceResult:
    """Backtest one race, retrying if OpenF1 rate-limits us.

    Without this a single 429 drops a race from the report silently, which
    would quietly change every aggregate number in it.
    """
    wait = BATCH_RETRY_BACKOFF_SECONDS
    for attempt in range(1, BATCH_MAX_RETRIES + 1):
        try:
            return await backtest_session(
                client, session_key, driver_number=driver_number,
                label=label, settings=settings,
            )
        except OpenF1RateLimited as exc:
            if attempt == BATCH_MAX_RETRIES:
                return RaceResult(
                    session_key=session_key, label=label, driver_number=0,
                    total_laps=0,
                    skipped_reason=f"rate-limited after {attempt} attempts",
                )
            pause = max(wait, exc.retry_after_seconds or 0)
            logger.warning(
                "Rate-limited on %s (attempt %d/%d); waiting %.0fs.",
                label or session_key, attempt, BATCH_MAX_RETRIES, pause,
            )
            await sleep(pause)
            wait *= 2
    raise AssertionError("unreachable")


async def backtest_session(
    client: OpenF1Client,
    session_key: str,
    *,
    driver_number: int | None = None,
    label: str = "",
    settings: Settings | None = None,
) -> RaceResult:
    """Replay one finished race through the real decision engine and score it."""
    settings = settings or get_settings()

    stints = await client.get_stints(session_key)
    if not stints:
        return RaceResult(
            session_key=session_key, label=label, driver_number=0, total_laps=0,
            skipped_reason="no stint data",
        )

    driver = resolve_driver(stints, driver_number, session_key)
    laps = await client.get_laps(session_key, driver)
    driver_stints = [s for s in stints if s.driver_number == driver]
    ticks = flatten_to_ticks(driver_stints, laps)

    if len(ticks) < 10:
        return RaceResult(
            session_key=session_key, label=label, driver_number=driver,
            total_laps=len(ticks), skipped_reason=f"only {len(ticks)} laps",
        )

    result = RaceResult(
        session_key=session_key,
        label=label,
        driver_number=driver,
        total_laps=max(t.lap for t in ticks),
    )
    result.actual_pit_laps = sorted(
        min(t.lap for t in ticks if t.stint_number == s)
        for s in sorted({t.stint_number for t in ticks})
    )[1:]

    clean = _clean_next_laps(ticks)
    by_lap = {t.lap: t for t in ticks}

    engine = DecisionEngine(settings)
    decisions: list[DecisionMessage] = []
    # Running mean per compound, as an independent baseline predictor.
    compound_totals: dict[str, list[float]] = {}

    for tick in ticks:
        decision = engine.observe(tick)

        if tick.lap_duration_s is not None and not tick.is_pit_out_lap:
            compound_totals.setdefault(tick.compound, []).append(tick.lap_duration_s)

        if decision is None:
            continue
        decisions.append(decision)
        result.decisions += 1
        if decision.degradation_is_measurable:
            result.decisions_with_measurable_degradation += 1
        key = decision.degradation_significance
        result.significance_counts[key] = result.significance_counts.get(key, 0) + 1

        # The prediction is for the NEXT lap, so it is only scoreable if that
        # lap exists and was run on the same tyre set.
        nxt = by_lap.get(tick.lap + 1)
        if nxt is None or nxt.lap_duration_s is None:
            continue
        if nxt.stint_number != tick.stint_number:
            continue
        if tick.lap_duration_s is None:
            continue

        seen = compound_totals.get(tick.compound, [])
        result.predictions.append(
            Prediction(
                lap=nxt.lap,
                compound=nxt.compound,
                tyre_age=nxt.tyre_age,
                actual_s=nxt.lap_duration_s,
                model_s=decision.projected_time_current_tyres_s,
                persistence_s=tick.lap_duration_s,
                compound_mean_s=sum(seen) / len(seen) if seen else tick.lap_duration_s,
                next_lap_is_clean=clean.get(nxt.lap, False),
            )
        )

    result.recommended_pit_lap = _recommended_pit_lap(decisions, result.total_laps)
    if len(result.actual_pit_laps) == 1:
        result.counterfactual = _counterfactual(
            engine, ticks, result.actual_pit_laps[0], settings
        )

    return result
