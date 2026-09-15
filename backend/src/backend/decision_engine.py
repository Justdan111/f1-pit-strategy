"""The pit/stay decision engine. One instance per connection.

Fit lap time as a straight line in tyre age, per compound:

    lap_time(age) = b + m * age

For a set of age A, comparing the next lap:

    stay out:  b + m*(A + 1)
    pit now:   b + m*1 + pit_cost
    delta   :  m*A - pit_cost

Extending the same comparison over N laps, every b and m*N(N+1)/2 cancels,
leaving m*N*A - pit_cost. So the per-lap advantage of a fresh set is m*A --
slope times current age, constant on every future lap -- and

    laps_to_break_even = pit_cost / (m * A)

Not modelled: fuel burn (so the fit measures degradation minus fuel effect),
track position, and the tyre cliff.
"""

import logging
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .models import DecisionMessage, TickMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sample:
    """One usable observation: tyre age and how long the lap took."""

    tyre_age: int
    lap_duration_s: float
    is_pit_out_lap: bool


@dataclass(frozen=True)
class DegradationFit:
    """A fitted line: lap_time = intercept + slope * tyre_age."""

    compound: str
    slope_s_per_lap: float
    intercept_s: float
    r_squared: float
    samples_used: int
    samples_seen: int

    def predict(self, tyre_age: float) -> float:
        return self.intercept_s + self.slope_s_per_lap * tyre_age


@dataclass
class _CompoundHistory:
    compound: str
    samples: list[Sample] = field(default_factory=list)


class DecisionEngine:
    """Accumulates ticks and produces a verdict once it can justify one.

    Uses only ticks already handed to it and holds no state outliving the
    connection, so live mode reuses it unmodified.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._history: dict[str, _CompoundHistory] = {}

    def observe(self, tick: TickMessage) -> DecisionMessage | None:
        """Record a tick and return a decision, or None if one is not justified yet."""
        self._record(tick)

        fit = self._fit(tick.compound)
        if fit is None:
            return None

        return self._decide(tick, fit)

    def _record(self, tick: TickMessage) -> None:
        """Store the tick if it carries a lap time. Outliers are excluded later, at fit time."""
        if tick.lap_duration_s is None:
            return

        history = self._history.setdefault(
            tick.compound, _CompoundHistory(compound=tick.compound)
        )
        history.samples.append(
            Sample(
                tyre_age=tick.tyre_age,
                lap_duration_s=tick.lap_duration_s,
                is_pit_out_lap=tick.is_pit_out_lap,
            )
        )

    def _usable(self, samples: list[Sample]) -> list[Sample]:
        """Select samples representing normal green-flag running.

        Two monotonicity rules, both needed against real data. A lap is dropped
        if it is much slower than the best lap at a SIMILAR tyre age, or much
        slower than the best lap on an OLDER tyre.

        They are complements. The first alone fails when a whole neighbourhood
        is contaminated; the second alone cannot catch contamination at the end
        of a stint, where no older laps exist. Comparing against the fastest lap
        of the whole stint instead would discard every legitimately degraded lap
        on a fast-wearing tyre.

        Re-derived on every fit, so a baseline that was itself contaminated is
        corrected once clean laps arrive.
        """
        candidates = samples
        if self._settings.exclude_pit_out_laps_from_fit:
            candidates = [s for s in candidates if not s.is_pit_out_lap]

        if not candidates:
            return []

        window = self._settings.fit_outlier_window_laps
        excess = self._settings.max_lap_time_excess_for_fit_s

        kept: list[Sample] = []
        for sample in candidates:
            neighbours = [
                other.lap_duration_s
                for other in candidates
                if abs(other.tyre_age - sample.tyre_age) <= window
            ]
            if sample.lap_duration_s > min(neighbours) + excess:
                continue

            older = [
                other.lap_duration_s
                for other in candidates
                if other.tyre_age > sample.tyre_age
            ]
            if older and sample.lap_duration_s > min(older) + excess:
                continue

            kept.append(sample)

        return kept

    def _fit(self, compound: str) -> DegradationFit | None:
        """Least-squares line for a compound, or None when there is nothing honest to report."""
        history = self._history.get(compound)
        if history is None:
            return None

        seen = len(history.samples)
        usable = self._usable(history.samples)

        if len(usable) < self._settings.min_samples_for_fit:
            return None

        xs = [float(s.tyre_age) for s in usable]
        ys = [s.lap_duration_s for s in usable]
        n = len(xs)

        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        # Zero when every sample shares one tyre age: a vertical scatter has no slope.
        variance_x = sum((x - mean_x) ** 2 for x in xs)
        if variance_x == 0:
            logger.debug(
                "Cannot fit %s: all %d samples at tyre_age=%.0f", compound, n, mean_x
            )
            return None

        covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        slope = covariance / variance_x
        intercept = mean_y - slope * mean_x

        ss_total = sum((y - mean_y) ** 2 for y in ys)
        ss_residual = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        # A flat line fits identical lap times perfectly.
        r_squared = 1.0 if ss_total == 0 else 1.0 - (ss_residual / ss_total)

        return DegradationFit(
            compound=compound,
            slope_s_per_lap=slope,
            intercept_s=intercept,
            r_squared=r_squared,
            samples_used=n,
            samples_seen=seen,
        )

    def _decide(self, tick: TickMessage, fit: DegradationFit) -> DecisionMessage:
        """Turn a fitted curve and the current tyre state into a verdict."""
        pit_cost = self._settings.pit_lane_cost_seconds
        fresh_age = self._settings.fresh_tyre_reference_age

        projected_stay = fit.predict(tick.tyre_age + 1)
        projected_fresh = fit.predict(fresh_age)

        # Derived directly so it stays exact regardless of fresh_age.
        advantage = fit.slope_s_per_lap * tick.tyre_age
        delta = projected_stay - (projected_fresh + pit_cost)

        measurable = advantage > 0
        break_even = (pit_cost / advantage) if measurable else None

        note: str | None = None
        if not measurable:
            if fit.slope_s_per_lap <= 0:
                note = (
                    f"No measurable degradation on {fit.compound}: fitted slope is "
                    f"{fit.slope_s_per_lap:+.4f} s/lap. Lap times are not rising with "
                    "tyre age. Fuel burn (~-0.03 s/lap as the car lightens) can mask "
                    "or exceed real degradation; this engine does not correct for it. "
                    "There is no break-even point on a tyre that is not slowing."
                )
            else:
                note = (
                    "Tyre age is 0, so a fresh set offers no advantage yet and there "
                    "is no break-even point to report."
                )
        elif break_even is not None and break_even <= 1.0:
            note = (
                "Fresh tyres pay for the stop within a single lap — degradation is "
                "extreme relative to the pit-lane cost. Check the fit before acting."
            )

        return DecisionMessage(
            lap=tick.lap,
            driver_number=tick.driver_number,
            compound=tick.compound,
            tyre_age=tick.tyre_age,
            verdict="pit_now" if delta > 0 else "stay_out",
            current_compound_degradation_s_per_lap=round(fit.slope_s_per_lap, 4),
            fit_intercept_s=round(fit.intercept_s, 3),
            fit_r_squared=round(fit.r_squared, 4),
            samples_used=fit.samples_used,
            samples_seen=fit.samples_seen,
            projected_time_current_tyres_s=round(projected_stay, 3),
            projected_time_fresh_tyres_s=round(projected_fresh, 3),
            pit_lane_cost_s=pit_cost,
            delta_s=round(delta, 3),
            fresh_tyre_advantage_s_per_lap=round(advantage, 4),
            laps_to_break_even=None if break_even is None else round(break_even, 2),
            degradation_is_measurable=measurable,
            note=note,
        )
