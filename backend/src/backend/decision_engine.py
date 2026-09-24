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
from .models import (
    DecisionMessage,
    DegradationSignificance,
    TickMessage,
    VerdictBasis,
)
from .statistics_helpers import CONFIDENCE_LEVEL, t_critical_95

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sample:
    """One usable observation: tyre age and how long the lap took."""

    tyre_age: int
    lap_duration_s: float
    is_pit_out_lap: bool
    # Needed for the fuel correction, which depends on race position rather
    # than tyre age: a fresh set on lap 40 carries far less fuel than the
    # same fresh set on lap 5.
    lap: int = 0


@dataclass(frozen=True)
class DegradationFit:
    """A fitted line: lap_time = intercept + slope * tyre_age."""

    compound: str
    slope_s_per_lap: float
    intercept_s: float
    r_squared: float
    samples_used: int
    samples_seen: int
    slope_std_error: float = 0.0
    slope_ci_low: float = 0.0
    slope_ci_high: float = 0.0
    # Seconds per lap added back to undo fuel burn. Zero when correction is
    # disabled, in which case the slope means what it did before: degradation
    # minus fuel effect.
    fuel_correction_s_per_lap: float = 0.0

    def predict(self, tyre_age: float) -> float:
        """Fuel-free lap time: what this tyre would do carrying no fuel.

        Not comparable to a real lap time. Use predict_actual for that.
        """
        return self.intercept_s + self.slope_s_per_lap * tyre_age

    def predict_actual(self, tyre_age: float, lap: int) -> float:
        """Predicted real lap time, with the fuel the car carries on that lap."""
        return self.predict(tyre_age) - self.fuel_correction_s_per_lap * (lap - 1)

    @property
    def raw_slope_s_per_lap(self) -> float:
        """The uncorrected slope, which is degradation minus fuel effect."""
        return self.slope_s_per_lap - self.fuel_correction_s_per_lap

    @property
    def significance(self) -> DegradationSignificance:
        """Whether the slope is distinguishable from zero.

        An interval spanning zero means "cannot tell", which is a different
        statement from "the tyre is not degrading" and must not be reported as
        though it were.
        """
        if self.slope_ci_low > 0:
            return "positive"
        if self.slope_ci_high < 0:
            return "negative"
        return "unclear"


@dataclass
class _CompoundHistory:
    compound: str
    samples: list[Sample] = field(default_factory=list)


class DecisionEngine:
    """Accumulates ticks and produces a verdict once it can justify one.

    Uses only ticks already handed to it and holds no state outliving the
    connection, so live mode reuses it unmodified.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        total_laps: int | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._history: dict[str, _CompoundHistory] = {}
        self._total_laps = total_laps

    @property
    def fuel_correction_s_per_lap(self) -> float:
        """Lap time to add back per lap to remove fuel burn.

        Zero when disabled. Otherwise the fuel effect per kg times burn per
        lap, which is start fuel over race distance -- so a shorter race gets
        a larger correction, because it burns fuel faster.
        """
        if not self._settings.fuel_correction_enabled:
            return 0.0
        laps = self._total_laps or self._settings.assumed_race_laps
        if laps <= 0:
            return 0.0
        burn_per_lap = self._settings.race_start_fuel_kg / laps
        return self._settings.fuel_effect_s_per_kg * burn_per_lap

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
                lap=tick.lap,
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

        # Add back the time fuel burn saved, so what remains is tyre wear.
        # Within a stint, lap number and tyre age move together, so this is a
        # constant shift of the slope -- but it is applied per sample rather
        # than added to the slope afterwards, so the intercept, residuals and
        # r-squared all describe the corrected data consistently.
        correction = self.fuel_correction_s_per_lap
        xs = [float(s.tyre_age) for s in usable]
        ys = [s.lap_duration_s + correction * (s.lap - 1) for s in usable]
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

        # Standard error of the slope: sqrt(residual variance / spread in x).
        # Needs n - 2 degrees of freedom, which the 3-sample minimum
        # guarantees. Wide intervals on few samples are correct, not a defect.
        degrees_of_freedom = n - 2
        if degrees_of_freedom >= 1:
            residual_variance = ss_residual / degrees_of_freedom
            std_error = (residual_variance / variance_x) ** 0.5
            margin = t_critical_95(degrees_of_freedom) * std_error
        else:
            std_error = 0.0
            margin = 0.0

        return DegradationFit(
            compound=compound,
            slope_s_per_lap=slope,
            intercept_s=intercept,
            r_squared=r_squared,
            samples_used=n,
            samples_seen=seen,
            slope_std_error=std_error,
            slope_ci_low=slope - margin,
            slope_ci_high=slope + margin,
            fuel_correction_s_per_lap=correction,
        )

    def _decide(self, tick: TickMessage, fit: DegradationFit) -> DecisionMessage:
        """Turn a fitted curve and the current tyre state into a verdict."""
        pit_cost = self._settings.pit_lane_cost_seconds
        fresh_age = self._settings.fresh_tyre_reference_age

        # Real predicted lap times for the NEXT lap, so both carry that lap's
        # fuel load. The fuel term is identical either way -- the same lap is
        # run whichever tyre is on the car -- so it cancels out of `delta` and
        # the comparison turns purely on the corrected degradation slope,
        # which is the whole point of correcting it.
        next_lap = tick.lap + 1
        projected_stay = fit.predict_actual(tick.tyre_age + 1, next_lap)
        projected_fresh = fit.predict_actual(fresh_age, next_lap)

        # Derived directly so it stays exact regardless of fresh_age.
        advantage = fit.slope_s_per_lap * tick.tyre_age
        delta = projected_stay - (projected_fresh + pit_cost)

        measurable = advantage > 0
        break_even = (pit_cost / advantage) if measurable else None

        # The verdict compares stopping against staying out over the LAPS THAT
        # REMAIN, not over the next lap alone. A one-lap comparison can only
        # favour stopping when the tyre loses more than the whole pit cost in a
        # single lap -- roughly 1.2 s/lap on a 19-lap-old set -- which no real
        # tyre approaches. That made the verdict structurally incapable of ever
        # saying "pit", which is a question nobody asks.
        #
        # Over the remaining laps the fresh-set advantage is collected every
        # lap, so the comparison becomes meaningful:
        #     net gain = advantage_per_lap * laps_remaining - pit_cost
        laps_remaining: int | None = None
        net_gain: float | None = None
        basis: VerdictBasis = "next_lap_only"

        if self._total_laps is not None:
            laps_remaining = max(0, self._total_laps - tick.lap)
            net_gain = advantage * laps_remaining - pit_cost
            basis = "race_remaining"
            pays_back = net_gain > 0
        else:
            # Race distance unknown, which in practice means live mode: OpenF1
            # reports no lap count for a session in progress. Fall back to the
            # one-lap comparison and say so, rather than guessing a distance
            # and presenting the result as a recommendation.
            pays_back = delta > 0

        # The same payback period at the ends of the slope's interval. A
        # shallower slope means a longer wait, so the LOW end of the slope
        # gives the HIGH end of the payback. When that end reaches zero there
        # is no upper bound: a tyre that might not be slowing might never pay
        # a stop back.
        break_even_low: float | None = None
        break_even_high: float | None = None
        if tick.tyre_age > 0:
            if fit.slope_ci_high > 0:
                break_even_low = pit_cost / (fit.slope_ci_high * tick.tyre_age)
            if fit.slope_ci_low > 0:
                break_even_high = pit_cost / (fit.slope_ci_low * tick.tyre_age)

        significance = fit.significance

        # A stop is only recommended on degradation the data can actually
        # show. A positive point estimate whose 95% interval reaches zero is
        # noise that landed on the positive side: on Baku 2025 every pit_now
        # without this gate came from exactly that, including one from three
        # laps of data at +/-10 s/lap. Insufficient signal means stay_out.
        should_pit = pays_back and significance == "positive"
        suppressed = pays_back and not should_pit

        # Caveats accumulate rather than overriding each other: the verdict's
        # basis and the fit's quality are separate concerns, and a reader needs
        # both. An earlier version let the basis note replace the degradation
        # note, hiding the more informative of the two.
        notes: list[str] = []

        if basis == "next_lap_only":
            notes.append(
                "The race distance is unknown, so this verdict compares only the "
                "next lap -- a comparison that can almost never favour stopping, "
                "since a pit stop cannot be repaid in one lap. Read "
                "laps_to_break_even instead, or supply the race distance."
            )

        if suppressed:
            notes.append(
                "The point estimate says a stop would pay back, but degradation "
                "is not significantly positive, so the verdict is stay_out: "
                "insufficient signal, not a recommendation against stopping."
            )

        if significance == "unclear" and measurable:
            notes.append(
                f"Degradation on {fit.compound} is not distinguishable from zero: "
                f"the slope is {fit.slope_s_per_lap:+.4f} s/lap but its 95% interval "
                f"[{fit.slope_ci_low:+.4f}, {fit.slope_ci_high:+.4f}] spans zero, on "
                f"{fit.samples_used} samples. Treat the figures as provisional."
            )
        elif not measurable:
            if fit.slope_s_per_lap <= 0:
                confidence = (
                    "confidently so"
                    if significance == "negative"
                    else "though the data is too noisy to be sure"
                )
                notes.append(
                    f"No measurable degradation on {fit.compound} ({confidence}): "
                    f"fitted slope is {fit.slope_s_per_lap:+.4f} s/lap. Lap times "
                    "are not rising with tyre age. Fuel burn (~-0.03 s/lap as the "
                    "car lightens) can mask or exceed real degradation. There is "
                    "no break-even point on a tyre that is not slowing."
                )
            else:
                notes.append(
                    "Tyre age is 0, so a fresh set offers no advantage yet and "
                    "there is no break-even point to report."
                )
        elif break_even is not None and break_even <= 1.0:
            notes.append(
                "Fresh tyres pay for the stop within a single lap -- degradation "
                "is extreme relative to the pit-lane cost. Check the fit before "
                "acting."
            )

        note = " ".join(notes) if notes else None

        return DecisionMessage(
            lap=tick.lap,
            driver_number=tick.driver_number,
            compound=tick.compound,
            tyre_age=tick.tyre_age,
            verdict="pit_now" if should_pit else "stay_out",
            verdict_basis=basis,
            laps_remaining=laps_remaining,
            net_gain_s=None if net_gain is None else round(net_gain, 2),
            current_compound_degradation_s_per_lap=round(fit.slope_s_per_lap, 4),
            raw_degradation_s_per_lap=round(fit.raw_slope_s_per_lap, 4),
            fuel_correction_s_per_lap=round(fit.fuel_correction_s_per_lap, 4),
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
            laps_to_break_even_low=(
                None if break_even_low is None else round(break_even_low, 2)
            ),
            laps_to_break_even_high=(
                None if break_even_high is None else round(break_even_high, 2)
            ),
            slope_std_error_s_per_lap=round(fit.slope_std_error, 5),
            slope_ci_low_s_per_lap=round(fit.slope_ci_low, 4),
            slope_ci_high_s_per_lap=round(fit.slope_ci_high, 4),
            degradation_significance=significance,
            degradation_is_measurable=measurable,
            note=note,
        )
