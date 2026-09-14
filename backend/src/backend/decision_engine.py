"""The pit/stay decision engine.

One instance per WebSocket connection. It is fed ticks in order, and after
each one either returns a DecisionMessage or None (not enough data yet).

Three properties it must have, in order of how easy they are to break:

1. INCREMENTAL. It may only ever use ticks it has already been handed. It
   never sees the race in advance, never looks ahead, and holds no state that
   outlives the connection. This is not a stylistic preference — it is what
   lets the same engine serve LiveTickSource on Day 4 without modification.
   A live race has no future to peek at, so an engine that peeks would work
   in replay and silently mislead in live mode.

2. MODE-BLIND. Nothing here imports ReplayTickSource, checks `source`, or
   knows what a fixture is. It consumes TickMessage.

3. EXPLAINABLE. Every number behind the verdict is emitted. A reader should
   be able to recompute the verdict by hand and disagree with it.


The maths, in full
------------------
Fit lap time as a straight line in tyre age, per compound:

    lap_time(age) = b + m * age          (m = degradation, seconds per lap)

Given the car is currently on a set of age A, compare the next lap:

    stay out:  b + m*(A + 1)
    pit now:   b + m*1        + pit_cost      (fresh set, read at age 1)

    delta = stay - pit = m*A - pit_cost

So the one-lap verdict is "pit" exactly when m*A > pit_cost. With a 22-second
pit cost that needs the tyre to be losing 22 seconds per lap, which no real
tyre approaches — the one-lap rule says "stay out" essentially always. That
is a true property of the rule DAY2.md specifies, not a bug in this code, and
it is why the engine also reports the two fields below.

Extending the same comparison over N laps instead of one:

    stay out:  N*b + m*(N*A + N(N+1)/2)
    pit now:   pit_cost + N*b + m*N(N+1)/2
    difference:  m*N*A - pit_cost

Every b and every m*N(N+1)/2 cancels, leaving a per-lap advantage of exactly

    m * A          <- slope times CURRENT age, constant on every future lap

and therefore

    laps_to_break_even = pit_cost / (m * A)

That is a genuine one-lap-lookahead result — no multi-lap simulation, no
projection of future laps — because the advantage turns out to be constant.
It is the number a strategist actually acts on.


What this deliberately does not model
-------------------------------------
- Fuel burn. Real lap times fall through a stint as the car sheds fuel
  (roughly -0.03 s/lap). Fitting lap time against tyre age therefore measures
  (degradation MINUS fuel effect), not degradation. On real Baku data this
  was enough to make a HARD tyre's measured slope come out NEGATIVE. The
  engine reports that honestly via `degradation_is_measurable` rather than
  hiding it; correcting for it is not Day 2 scope.
- Track position. Whether pitting drops you behind another car is a
  multi-driver question and is dropped from this project entirely, not
  deferred (DAY2.md). This engine answers "is it faster", never "will it
  cost a place".
- The tyre cliff. Real degradation is not linear; it steepens sharply late in
  a stint. A straight line will under-predict a cliff. DAY2.md explicitly
  asks for linear and warns against reaching for anything fancier.
"""

import logging
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .models import DecisionMessage, TickMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sample:
    """One usable observation: how old the tyre was, how long the lap took."""

    tyre_age: int
    lap_duration_s: float
    is_pit_out_lap: bool


@dataclass(frozen=True)
class DegradationFit:
    """A fitted straight line: lap_time = intercept + slope * tyre_age."""

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
    """Every sample seen for one compound, in this connection."""

    compound: str
    samples: list[Sample] = field(default_factory=list)


class DecisionEngine:
    """Accumulates ticks and produces a verdict once it can justify one."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._history: dict[str, _CompoundHistory] = {}

    # --- public API -------------------------------------------------------

    def observe(self, tick: TickMessage) -> DecisionMessage | None:
        """Record a tick and return a decision, or None if one isn't justified.

        Returning None is a normal outcome, not a failure: at the start of a
        stint on a compound never seen before, there is genuinely nothing to
        say. The caller sends a `tick` with no `decision` after it.
        """
        self._record(tick)

        fit = self._fit(tick.compound)
        if fit is None:
            return None

        return self._decide(tick, fit)

    # --- accumulation -----------------------------------------------------

    def _record(self, tick: TickMessage) -> None:
        """Store the tick if it carries a lap time.

        Note what is NOT filtered here: out-laps and slow laps are recorded,
        and excluded later at fit time. That is deliberate — see _usable().
        """
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
        """Select the samples that represent normal green-flag running.

        Two rules, both needed against real data:

        1. Drop flagged out-laps. An out-lap starts in the pit lane and is
           slow for reasons unrelated to tyre wear (+18.1s, measured).

        2. Drop any lap more than `max_lap_time_excess_for_fit_s` slower than
           the fastest lap seen at a SIMILAR TYRE AGE — within
           `fit_outlier_window_laps` either side. Catches safety cars,
           in-laps, traffic, mistakes and the standing start with one rule.

        Why rule 2 is local, not global
        -------------------------------
        Comparing against the fastest lap of the whole stint was the first
        version, and it was wrong in a way that only a hand-check exposed.
        On a tyre degrading at 1.5 s/lap, a lap at age 18 is legitimately 27
        seconds slower than a lap at age 0 — so a global threshold threw away
        every degraded lap, left one sample, and the engine silently returned
        no decision at all. It discarded precisely the evidence that
        degradation was happening.

        A local baseline separates the two cases, because degradation is
        monotonic: a lap should never be much slower than a lap run on a
        similarly-aged tyre. An anomaly is slow relative to its neighbours;
        a worn tyre is slow relative to a NEW tyre but normal for its age.

        Re-evaluated from scratch on every fit, against every sample seen so
        far. That matters: if the opening laps on a compound are behind a
        safety car, the baseline is initially contaminated too. As clean laps
        arrive, the baseline drops and the earlier laps are retroactively
        excluded. Filtering once at admission time would have let them in
        permanently.

        This is still strictly incremental — every sample considered has
        already been seen. Re-deriving the clean set does not look ahead; it
        re-reads the past with better information.
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
            # Rule 2a — local: slower than the best lap at a similar age.
            # `neighbours` always contains `sample` itself, so min() is safe.
            neighbours = [
                other.lap_duration_s
                for other in candidates
                if abs(other.tyre_age - sample.tyre_age) <= window
            ]
            if sample.lap_duration_s > min(neighbours) + excess:
                continue

            # Rule 2b — monotonic: slower than the best lap on an OLDER tyre.
            #
            # Needed because 2a fails when an entire window is contaminated.
            # Real case, Baku 2025: laps 1-4 were the standing start and a
            # safety car, so at tyre age 0 every neighbour within +/-3 laps
            # was also garbage, the local baseline was garbage, and lap 1
            # (+25.7s) survived as the minimum of its own bad window — then
            # dragged the fitted slope sharply negative.
            #
            # The argument is the same monotonicity as 2a, read the other
            # way: a worn tyre should be SLOWER than a fresher one, so a lap
            # that is much slower than a lap run later on a more worn set did
            # not lose that time to tyre wear.
            #
            # 2a and 2b are complements, not duplicates: 2b cannot catch
            # contamination at the very end of a stint (no older laps exist
            # to compare against), and 2a cannot catch contamination whose
            # whole neighbourhood is contaminated.
            older = [
                other.lap_duration_s
                for other in candidates
                if other.tyre_age > sample.tyre_age
            ]
            if older and sample.lap_duration_s > min(older) + excess:
                continue

            kept.append(sample)

        return kept

    # --- fitting ----------------------------------------------------------

    def _fit(self, compound: str) -> DegradationFit | None:
        """Least-squares straight line for a compound, or None if not yet possible.

        Returns None — not an error, not a zeroed fit — when there is nothing
        honest to report: too few clean samples, or every sample at the same
        tyre age (a vertical scatter has no slope; the denominator is zero).
        """
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

        # Σ(x - x̄)² — zero when every sample shares one tyre age.
        variance_x = sum((x - mean_x) ** 2 for x in xs)
        if variance_x == 0:
            logger.debug(
                "Cannot fit %s: all %d samples at tyre_age=%.0f", compound, n, mean_x
            )
            return None

        covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        slope = covariance / variance_x
        intercept = mean_y - slope * mean_x

        # r² = 1 - SS_res/SS_tot. When SS_tot is zero every lap time was
        # identical, and a flat line fits that perfectly, so r² is 1.0.
        ss_total = sum((y - mean_y) ** 2 for y in ys)
        ss_residual = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        r_squared = 1.0 if ss_total == 0 else 1.0 - (ss_residual / ss_total)

        return DegradationFit(
            compound=compound,
            slope_s_per_lap=slope,
            intercept_s=intercept,
            r_squared=r_squared,
            samples_used=n,
            samples_seen=seen,
        )

    # --- the decision -----------------------------------------------------

    def _decide(self, tick: TickMessage, fit: DegradationFit) -> DecisionMessage:
        """Turn a fitted curve and the current tyre state into a verdict."""
        pit_cost = self._settings.pit_lane_cost_seconds
        fresh_age = self._settings.fresh_tyre_reference_age

        # Next lap on the current set: it will be one lap older than now.
        projected_stay = fit.predict(tick.tyre_age + 1)

        # Next lap on a fresh set of the same compound. Read at age 1 rather
        # than 0 (DAY2.md): lap one on a new set is not the tyre's best.
        projected_fresh = fit.predict(fresh_age)

        # Per-lap gain from swapping to a fresh set, constant on every future
        # lap. Derived directly rather than as a difference of the two
        # projections above, so it stays exact regardless of fresh_age.
        advantage = fit.slope_s_per_lap * tick.tyre_age

        # The rule DAY2.md specifies, over exactly one lap.
        # Positive => pitting is faster => pit_now.
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
