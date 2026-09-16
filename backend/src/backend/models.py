"""Domain data (stints, laps, sessions) and the WebSocket message protocol."""

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# Where a stream's data came from. "live" means a session happening right now;
# a replayed historical race is never "live".
SourceKind = Literal["sample", "historical_replay", "live"]

Verdict = Literal["pit_now", "stay_out"]

# Whether the fitted degradation slope is distinguishable from zero at 95%
# confidence. "unclear" is the honest answer when the data is too noisy or too
# sparse to tell, which is different from a tyre that genuinely is not slowing.
DegradationSignificance = Literal["positive", "unclear", "negative"]


class Stint(BaseModel):
    """A continuous run on one set of tyres. Field names match OpenF1 /v1/stints."""

    model_config = ConfigDict(extra="ignore")

    driver_number: int
    stint_number: int
    lap_start: int
    lap_end: int
    compound: str = "UNKNOWN"
    tyre_age_at_start: int = 0

    session_key: int | None = None
    meeting_key: int | None = None

    @property
    def lap_count(self) -> int:
        return max(0, self.lap_end - self.lap_start + 1)


class Lap(BaseModel):
    """One completed lap. Field names match OpenF1 /v1/laps."""

    model_config = ConfigDict(extra="ignore")

    driver_number: int
    lap_number: int
    # Nullable upstream. Never defaulted: a guessed lap time is
    # indistinguishable from a real one once it is inside the regression.
    lap_duration: float | None = None
    is_pit_out_lap: bool = False

    session_key: int | None = None
    meeting_key: int | None = None


class Session(BaseModel):
    """One session from OpenF1 /v1/sessions. Timestamps are timezone-aware."""

    model_config = ConfigDict(extra="ignore")

    session_key: int
    session_name: str = "Unknown"
    session_type: str = "Unknown"
    date_start: datetime
    date_end: datetime
    is_cancelled: bool = False

    meeting_key: int | None = None
    country_name: str | None = None
    location: str | None = None
    year: int | None = None

    @property
    def label(self) -> str:
        where = self.location or self.country_name
        return f"{self.session_name} at {where}" if where else self.session_name


class StartMessage(BaseModel):
    """First message on every stream."""

    type: Literal["start"] = "start"
    session_key: str
    source: SourceKind
    # None in live mode: a race in progress has no known total.
    total_laps: int | None = None
    driver_number: int | None = None


class TickMessage(BaseModel):
    """One lap of one driver. Identical in every mode, so the engine cannot branch on mode."""

    type: Literal["tick"] = "tick"
    lap: int
    driver_number: int
    compound: str
    tyre_age: int
    stint_number: int
    lap_duration_s: float | None = None
    is_pit_out_lap: bool = False


class DecisionMessage(BaseModel):
    """The pit/stay call and every number behind it, so the verdict can be recomputed by hand.

    Sign convention: delta_s is time SAVED by pitting over the next lap, so
    delta_s > 0 means verdict == "pit_now".
    """

    type: Literal["decision"] = "decision"
    lap: int
    driver_number: int
    compound: str
    tyre_age: int

    verdict: Verdict

    # Tyre degradation with fuel burn removed. Before fuel correction this
    # field silently meant "degradation minus fuel effect", which is why it
    # came out negative on most real races.
    current_compound_degradation_s_per_lap: float
    # The uncorrected slope, reported so the correction is auditable rather
    # than an invisible adjustment.
    raw_degradation_s_per_lap: float = 0.0
    # Seconds per lap added back. Zero when correction is disabled, in which
    # case the two slopes above are equal.
    fuel_correction_s_per_lap: float = 0.0

    fit_intercept_s: float
    fit_r_squared: float
    samples_used: int
    samples_seen: int

    # Uncertainty on the slope, at 95% confidence. With few samples the
    # interval is wide, which is the point: it separates "this tyre is not
    # slowing" from "we cannot yet tell whether it is".
    slope_std_error_s_per_lap: float = 0.0
    slope_ci_low_s_per_lap: float = 0.0
    slope_ci_high_s_per_lap: float = 0.0
    degradation_significance: DegradationSignificance = "unclear"

    projected_time_current_tyres_s: float
    projected_time_fresh_tyres_s: float
    pit_lane_cost_s: float
    delta_s: float

    # Slope times CURRENT tyre age. Constant on every future lap, and the
    # quantity the whole decision turns on.
    fresh_tyre_advantage_s_per_lap: float

    # pit_cost / advantage. Not pit_cost / slope, which would overstate the
    # window by a factor of the tyre's age. None when the advantage is not
    # positive.
    laps_to_break_even: float | None = None

    # The same figure at the ends of the slope's confidence interval. `high`
    # is None when the interval reaches zero or below, because a tyre that
    # might not be slowing has no upper bound on its payback period.
    laps_to_break_even_low: float | None = None
    laps_to_break_even_high: float | None = None

    # False when the fitted slope is <= 0, e.g. fuel burn masking tyre wear.
    degradation_is_measurable: bool = True
    note: str | None = None


class NoLiveSessionMessage(BaseModel):
    """No session is running. A dedicated type, not an error: this is the normal case."""

    type: Literal["no_live_session"] = "no_live_session"
    detail: str
    checked_at: datetime

    next_session_key: int | None = None
    next_session_name: str | None = None
    next_session_start: datetime | None = None


class EndMessage(BaseModel):
    """Stream finished normally: replay exhausted, or live session closed."""

    type: Literal["end"] = "end"
    session_key: str
    total_ticks: int
    total_decisions: int = 0
    reason: str = "completed"


class ErrorMessage(BaseModel):
    """Stream failed or was refused. `code` for machines, `detail` for humans."""

    type: Literal["error"] = "error"
    detail: str
    code: str | None = None


StreamMessage = Annotated[
    Union[
        StartMessage,
        TickMessage,
        DecisionMessage,
        NoLiveSessionMessage,
        EndMessage,
        ErrorMessage,
    ],
    Field(discriminator="type"),
]
