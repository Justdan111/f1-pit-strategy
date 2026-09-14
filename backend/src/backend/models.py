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


class EndMessage(BaseModel):
    """Stream finished normally: replay exhausted, or live session closed."""

    type: Literal["end"] = "end"
    session_key: str
    total_ticks: int
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
    Union[StartMessage, TickMessage, EndMessage, ErrorMessage],
    Field(discriminator="type"),
]
