"""The TickSource interface: the seam between where data comes from and what we do with it."""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from .models import StartMessage, TickMessage


class TickSourceError(Exception):
    """A source could not produce a stream. Rendered as an `error` envelope."""

    code = "tick_source_error"

    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code


class NoDataError(TickSourceError):
    """The source opened but there is nothing to stream. The request worked; the answer was empty."""

    code = "no_data"


class TickSource(ABC):
    """The contract every data source implements."""

    @property
    @abstractmethod
    def session_key(self) -> str:
        """Identifies the stream. "sample" for the offline fixture."""

    @property
    @abstractmethod
    def source(self) -> str:
        """One of: sample, historical_replay, live."""

    @abstractmethod
    async def open(self) -> StartMessage:
        """Acquire what is needed to stream and describe it. Raises TickSourceError if it cannot start."""

    @abstractmethod
    def ticks(self) -> AsyncIterator[TickMessage]:
        """Yield ticks in order, pausing between them as the mode requires.

        Declared with `def`, not `async def`: an `async def ... yield`
        implementation is an async generator function, so calling it returns
        the generator without awaiting.
        """

    async def close(self) -> None:
        """Release resources acquired by open(). Safe to call if open() failed."""
        return None
