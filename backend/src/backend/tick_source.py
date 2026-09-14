"""The TickSource interface — the seam between "where data comes from" and
"what we do with it".

There is exactly one implementation today (ReplayTickSource). The interface
exists anyway, on purpose: LiveTickSource arrives on Day 4, and the whole
point of Day 1 (see DAY1.md) is that it should slot in *behind this contract*
without the WebSocket handler or the decision engine changing at all.

The contract is three methods:

    open()   -> do any I/O needed to begin, and describe the stream
    ticks()  -> yield ticks one at a time, as they become available
    close()  -> release anything open() acquired

Why open() is separate from ticks()
-----------------------------------
Both implementations need to do fallible I/O *before* the first tick exists:
replay has to fetch and flatten a race; live has to check whether a session
is actually running right now. That work can fail in ways the client needs
told about specifically ("no such session_key", "no live session right now"),
and it's also what produces the metadata for the `start` message — total_laps
in particular is unknowable until the data is in hand.

Separating it gives the caller a clean shape:

    start = await source.open()     # may raise -> send an `error` envelope
    send(start)                     # succeeded -> send the `start` envelope
    async for tick in source.ticks():
        send(tick)

If open() and ticks() were fused into one generator, the handler would have
to pull the first item before it knew whether the stream was even viable, and
the distinction between "couldn't start" and "started then died" would blur.

Why ticks() is an async iterator
--------------------------------
"Give me the next tick" is literally what an async iterator is. It matters
that it is *async* and that it is an *iterator*:

- async: producing the next tick involves waiting. Replay waits on a timer;
  live waits on an HTTP poll or an MQTT message. In both cases the event loop
  is free to serve other connections while we wait. A synchronous generator
  that called time.sleep() would block every other client on the server.

- iterator: the consumer pulls. The source cannot run ahead and pile up ticks
  the consumer hasn't sent yet, because it is suspended at `yield` until
  asked for the next one. That gives us backpressure for free — if a slow
  WebSocket client can't keep up, the source simply isn't advanced. (A
  push-based design, where the source called a callback, would need an
  explicit queue and a policy for what to do when it fills.)

A Python subtlety worth naming: the abstract method below is declared with
plain `def` returning AsyncIterator, not `async def`. An implementation
written as `async def ticks(self): ... yield ...` is an *async generator
function* — calling it returns an async generator immediately, without being
awaited. So `def ... -> AsyncIterator[TickMessage]` is the signature that
actually describes both, and `async for tick in source.ticks():` is correct
in either case. Declaring the abstract as `async def` would be a lie that
type checkers would then hold you to.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from .models import StartMessage, TickMessage


class TickSourceError(Exception):
    """A source could not produce a stream, for a reason worth telling the client.

    Anything raised as (a subclass of) this is expected to be rendered into an
    `error` envelope rather than blowing up as a 500. `code` is the stable,
    machine-readable half; str(exc) is the human half.
    """

    code = "tick_source_error"

    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code


class NoDataError(TickSourceError):
    """The source opened fine but there is nothing to stream.

    E.g. a valid-looking session_key that OpenF1 has no stints for. Distinct
    from "the API is down" — the request worked, the answer was empty.
    """

    code = "no_data"


class TickSource(ABC):
    """The contract every data source implements. See module docstring."""

    @property
    @abstractmethod
    def session_key(self) -> str:
        """Identifies the stream. "sample" for the offline fixture."""

    @property
    @abstractmethod
    def source(self) -> str:
        """One of the SourceKind values: sample / historical_replay / live."""

    @abstractmethod
    async def open(self) -> StartMessage:
        """Acquire whatever is needed to stream, and describe the stream.

        Raises TickSourceError (or a subclass) if the stream cannot start.
        Must be called exactly once, before ticks().
        """

    @abstractmethod
    def ticks(self) -> AsyncIterator[TickMessage]:
        """Yield ticks in order, pausing between them as the mode requires.

        Implemented as an `async def ... yield ...` async generator. Returns
        normally when the stream is exhausted (replay finished, live session
        ended); the caller turns that into the `end` envelope.
        """

    async def close(self) -> None:
        """Release resources acquired by open(). Safe to call if open() failed.

        Not abstract: sources with nothing to release (like the sample-backed
        replay) inherit this no-op rather than being forced to write one.
        """
        return None
