"""Pydantic models: the domain data (stints) and the wire protocol (messages).

Two distinct groups live in this file, and it's worth being clear about which
is which:

1. `Stint` is *input* data. It mirrors what OpenF1's /v1/stints returns.
2. `StartMessage` / `TickMessage` / `EndMessage` / `ErrorMessage` are *output*
   data: the exact JSON envelopes that go down the WebSocket, defined in
   SPEC section 8.

Keeping them separate matters. The wire protocol is a contract with the
frontend (Day 3) and must stay stable; the stint shape is a contract with
somebody else's API, which can change under us. If we streamed raw OpenF1
rows to the browser, an upstream field rename would become a frontend bug.
"""

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# Where a stream's data came from. SPEC section 8 pins these three values.
#   "sample"            -> the offline fixture in sample_data.py
#   "historical_replay" -> a finished race, fetched from OpenF1 and paced out
#   "live"              -> a genuinely live session (Day 4, not built yet)
# The point of naming all three now is that "live" must mean exactly one
# thing forever: a real session happening right now. A replayed historical
# race is never "live", however live it looks in the UI.
SourceKind = Literal["sample", "historical_replay", "live"]


class Stint(BaseModel):
    """One stint: a continuous run on one set of tyres, for one driver.

    Field names deliberately match OpenF1's /v1/stints response so a raw row
    can be validated straight into this model. `extra="ignore"` means OpenF1
    can add fields without breaking us.

    NOTE (DAY1.md): these field names were taken from OpenF1's documentation,
    not yet confirmed against a real HTTP call from this machine. Verify
    before Day 2's maths depends on them.
    """

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
        """How many laps this stint covers, inclusive of both ends."""
        return max(0, self.lap_end - self.lap_start + 1)


class Lap(BaseModel):
    """One completed lap, from OpenF1 /v1/laps.

    Field names match the real API (verified 2026-09-14 against session_key
    9904). Only the fields Day 2 needs are modelled; `extra="ignore"` drops
    the sector times, speed traps and segment arrays we don't use.

    `lap_duration` is genuinely nullable upstream — a lap that was never
    completed has no duration. We keep it optional rather than defaulting it,
    because inventing a lap time would feed a fabricated number straight into
    the degradation fit.
    """

    model_config = ConfigDict(extra="ignore")

    driver_number: int
    lap_number: int
    lap_duration: float | None = None
    is_pit_out_lap: bool = False

    session_key: int | None = None
    meeting_key: int | None = None


class Session(BaseModel):
    """One session from OpenF1 /v1/sessions.

    Field names verified against the real API on 2026-09-14. `date_start` and
    `date_end` arrive as ISO-8601 with an explicit UTC offset, which pydantic
    parses into timezone-aware datetimes — that awareness is load-bearing,
    since comparing a naive datetime against an aware one raises, and the
    whole live-window check is a datetime comparison.
    """

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
        """Human description, e.g. 'Race at Baku'."""
        where = self.location or self.country_name
        return f"{self.session_name} at {where}" if where else self.session_name


class StartMessage(BaseModel):
    """First message on every stream. Tells the client what it's about to get.

    `total_laps` is None for live mode, because a race in progress has no
    known end yet. Clients must therefore treat a progress bar as optional,
    not assume a total is always present.
    """

    type: Literal["start"] = "start"
    session_key: str
    source: SourceKind
    total_laps: int | None = None
    driver_number: int | None = None


class TickMessage(BaseModel):
    """One lap of one driver. Identical shape in every mode, by design.

    This is the payload the Day 2 decision engine will consume. It must not
    gain a "this came from a replay" flag — the engine is supposed to be
    unable to tell. Mode belongs in the start message, once, not on every tick.
    """

    type: Literal["tick"] = "tick"
    lap: int
    driver_number: int
    compound: str
    tyre_age: int
    stint_number: int

    # --- Day 2 additions ---
    # Both fields live in the SHARED tick contract, not in replay-specific
    # code, because LiveTickSource must supply them too (DAY2.md). Both come
    # from OpenF1 /v1/laps, which live mode polls exactly like replay reads
    # it, so neither field is one that only replay can fill.

    # Optional on purpose. OpenF1 returns null lap_duration for laps that
    # were never completed, and a tick with an unknown lap time is still a
    # valid tick — it just can't feed the degradation fit. Making this a
    # required float would force us to invent a number, and a fabricated lap
    # time is indistinguishable from a real one once it's in the regression.
    lap_duration_s: float | None = None

    # An out-lap starts in the pit lane and is many seconds slow for reasons
    # that have nothing to do with tyre wear. Carried so the decision engine
    # can exclude it from the fit; measured at +18.1s on real Baku data.
    is_pit_out_lap: bool = False


Verdict = Literal["pit_now", "stay_out"]


class DecisionMessage(BaseModel):
    """The pit/stay call, plus every number that produced it.

    SPEC section 10: "explainability over accuracy". Nothing here is a black
    box — a reader should be able to recompute `verdict` by hand from the
    other fields, and disagree with it if the numbers look wrong.

    Field names started from the DAY2.md draft. Two notes on where they moved:

    1. The draft's example numbers do not actually compute
       (92.4 vs 89.1 + 22.0 is a gap of -18.7s, but the example shows
       delta_s: -0.7). That draft was written before the model existed, so
       the shape was kept and the arithmetic defined properly here.

    2. `laps_to_break_even` is new, and it is the field that makes the rest
       usable. See its docstring below.

    SIGN CONVENTION, stated once and relied on everywhere:

        delta_s = time SAVED by pitting, over the next lap
                = projected_time_current_tyres_s
                  - (projected_time_fresh_tyres_s + pit_lane_cost_s)

    So delta_s > 0 means pitting is faster, and verdict == "pit_now".
    """

    type: Literal["decision"] = "decision"
    lap: int
    driver_number: int
    compound: str
    tyre_age: int

    verdict: Verdict

    # --- the fitted curve ---
    # Slope of lap time against tyre age for this compound, from the samples
    # seen so far IN THIS CONNECTION. Positive = the tyre is getting slower,
    # which is the normal case. Can legitimately come out negative or zero
    # (see `degradation_is_measurable`).
    current_compound_degradation_s_per_lap: float
    fit_intercept_s: float
    fit_r_squared: float
    samples_used: int
    samples_seen: int

    # --- the one-lap comparison (the rule DAY2.md specifies) ---
    projected_time_current_tyres_s: float
    projected_time_fresh_tyres_s: float
    pit_lane_cost_s: float
    delta_s: float

    # --- the number the one-lap comparison hides ---
    # How much time a fresh set of this compound would save, per lap, versus
    # the set currently on the car. Derived, not fitted:
    #
    #   old tyre on lap k from now:  b + m*(A + k)
    #   new tyre on lap k from now:  b + m*k
    #   difference:                  m * A          <- constant in k
    #
    # So the advantage is the degradation slope times the CURRENT TYRE AGE,
    # and it is the same on every future lap. This is the quantity the whole
    # decision turns on, so it is reported rather than left implicit.
    fresh_tyre_advantage_s_per_lap: float

    # --- what the one-lap comparison cannot tell you ---
    # Laps you must run on the fresh set before the stop pays for itself:
    #
    #   laps_to_break_even = pit_lane_cost_s / fresh_tyre_advantage_s_per_lap
    #
    # NOT pit_cost / slope. The advantage per lap is m*A, not m — dividing by
    # the slope alone would overstate the payback period by a factor of the
    # tyre's age (a factor of ~19 on an old set), turning a realistic 10-lap
    # pit window into a nonsensical 200-lap one.
    #
    # This field exists because the one-lap rule DAY2.md specifies is
    # structurally near-incapable of saying "pit": delta_s only goes positive
    # when the advantage exceeds the whole 22-second pit cost within a single
    # lap, which needs m*A > 22 — far outside anything a real tyre reaches.
    # The verdict is still computed exactly as specified; this is the field
    # that makes it actionable rather than permanently "stay out".
    #
    # None when the advantage is not positive — a tyre that is not getting
    # slower has no break-even point.
    laps_to_break_even: float | None = None

    # False when the fitted slope is <= 0, i.e. the data does not show the
    # tyre getting slower. Real causes: fuel burn (the car sheds ~100kg over
    # a stint, worth roughly -0.03 s/lap of lap time, which can exceed a hard
    # tyre's degradation), a short sample, or track evolution. Not an error —
    # a state the client must be able to display honestly rather than
    # dressing up as a confident verdict.
    degradation_is_measurable: bool = True

    # Human-readable caveat, or None. Surfaced so a UI never has to infer
    # "why does this say stay out forever?" from the numbers alone.
    note: str | None = None


class NoLiveSessionMessage(BaseModel):
    """There is no F1 session running right now.

    A DEDICATED TYPE, deliberately not an `error`. SPEC section 10: live mode
    "will spend most of its life with no session to connect to; this must be
    an expected, clearly-communicated state, not an error."

    The distinction is not pedantry. Nothing has failed here — the request
    worked, the API answered, and the answer was "no race is happening". If
    this were an ErrorMessage the dashboard would show a red failure banner
    for the single most common outcome of live mode, training the user to
    ignore the one component that is supposed to tell them when something is
    actually broken.

    Carries the next session when one is known, so the answer is "not until
    Thursday 08:30" rather than a bare no.
    """

    type: Literal["no_live_session"] = "no_live_session"
    detail: str
    # When the check ran, so a stale page cannot look current.
    checked_at: datetime

    next_session_key: int | None = None
    next_session_name: str | None = None
    next_session_start: datetime | None = None


class EndMessage(BaseModel):
    """Stream finished normally: replay exhausted, or live session closed."""

    type: Literal["end"] = "end"
    session_key: str
    total_ticks: int
    # Ticks without decisions is a normal outcome (early laps, or a compound
    # with too few clean samples), so the two counts are reported separately
    # rather than assumed equal.
    total_decisions: int = 0
    reason: str = "completed"


class ErrorMessage(BaseModel):
    """Stream failed, or was refused. `code` is for machines, `detail` for humans.

    Day 4 adds an expected, non-exceptional case here: "no live session right
    now" (SPEC section 8) — which is why this carries a machine-readable code
    rather than just prose.
    """

    type: Literal["error"] = "error"
    detail: str
    code: str | None = None


# A discriminated union over the `type` field. Pydantic uses `type` to decide
# which model a blob of JSON is, without trial-and-error validation. We don't
# strictly need it server-side (we only ever send), but declaring it makes the
# protocol a single named thing that tests and future clients can validate
# against, instead of four unrelated classes.
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
