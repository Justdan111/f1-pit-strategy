"""Async client for the OpenF1 REST API (https://openf1.org)."""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .models import Lap, Session, Stint

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class OpenF1Error(Exception):
    """Base class for anything that went wrong talking to OpenF1."""

    code = "openf1_error"


class OpenF1Unavailable(OpenF1Error):
    """Could not reach OpenF1, or it returned a server error or timed out."""

    code = "openf1_unavailable"


class OpenF1BadResponse(OpenF1Error):
    """OpenF1 answered, but not with anything we could understand."""

    code = "openf1_bad_response"


class OpenF1RateLimited(OpenF1Error):
    """OpenF1 returned 429. Distinct from unavailable: the remedy is to slow down."""

    code = "openf1_rate_limited"

    def __init__(self, detail: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after_seconds = retry_after_seconds


class RateLimiter:
    """Enforces a minimum gap between requests.

    OpenF1 advertises no rate-limit headers, so there is nothing to react to:
    compliance is enforced before the request goes out. Not a token bucket,
    because bursts are exactly what must not be allowed.

    `clock` and `sleep` are injected so tests can drive virtual time.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._min_interval = min_interval_seconds
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._last_request_at: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until it is safe to make another request."""
        # The lock prevents two coroutines both reading the same timestamp,
        # both deciding no wait is needed, and firing simultaneously.
        async with self._lock:
            now = self._clock()
            if self._last_request_at is not None:
                elapsed = now - self._last_request_at
                remaining = self._min_interval - elapsed
                if remaining > 0:
                    await self._sleep(remaining)
                    now = self._clock()
            self._last_request_at = now


class OpenF1Client:
    """HTTP in, validated models out. Makes no judgement about what the data means."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._http = http
        self._base_url = settings.openf1_base_url.rstrip("/")
        self._timeout = settings.openf1_timeout_seconds
        # One limiter for the whole client: a per-endpoint limiter would let
        # each endpoint run at the full rate and jointly double it.
        self._rate_limiter = rate_limiter or RateLimiter(
            settings.openf1_min_request_interval_seconds
        )

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        clean = {k: v for k, v in params.items() if v is not None}

        # No bypass: "just this one quick call" is how rate limits get breached.
        await self._rate_limiter.acquire()

        try:
            response = await self._http.get(url, params=clean, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise OpenF1Unavailable(
                f"OpenF1 timed out after {self._timeout}s calling {path}."
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenF1Unavailable(f"Could not reach OpenF1 at {url}: {exc}") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_seconds = None
            raise OpenF1RateLimited(
                f"OpenF1 rate-limited the request to {path}. "
                "Polling is too frequent; backing off.",
                retry_after_seconds=retry_seconds,
            )

        # API quirk: a query matching nothing returns 404, not 200 []. That is
        # an empty result, not a rejected request.
        if response.status_code == 404 and "No results found" in response.text:
            logger.info("OpenF1 has no results for %s with params %r", path, clean)
            return []

        if response.status_code >= 500:
            raise OpenF1Unavailable(
                f"OpenF1 returned {response.status_code} for {path}. "
                "The API is having problems; this is not a problem with the request."
            )
        if response.status_code >= 400:
            raise OpenF1BadResponse(
                f"OpenF1 rejected the request to {path} with "
                f"{response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenF1BadResponse(
                f"OpenF1 returned non-JSON from {path}: {response.text[:200]}"
            ) from exc

        if not isinstance(payload, list):
            raise OpenF1BadResponse(
                f"Expected a JSON array from {path}, got {type(payload).__name__}."
            )

        return payload

    async def get_stints(
        self,
        session_key: str,
        driver_number: int | None = None,
    ) -> list[Stint]:
        """Fetch stints. Unparseable rows are dropped, not fatal: one retirement
        must not make the other nineteen drivers unusable."""
        rows = await self._get(
            "stints",
            {"session_key": session_key, "driver_number": driver_number},
        )
        return self._parse(rows, Stint, "stint", session_key)

    async def get_laps(
        self,
        session_key: str,
        driver_number: int | None = None,
    ) -> list[Lap]:
        """Fetch per-lap timing. Laps with a null duration are kept: the lap is
        still real, it just cannot feed the fit."""
        rows = await self._get(
            "laps",
            {"session_key": session_key, "driver_number": driver_number},
        )
        return self._parse(rows, Lap, "lap", session_key)

    async def get_sessions(
        self,
        *,
        session_key: str | int | None = None,
        year: int | None = None,
        country_name: str | None = None,
    ) -> list[Session]:
        """Fetch session metadata. `session_key="latest"` is OpenF1's shortcut
        for the most recent or currently-running session.

        Makes no judgement about whether a session is live; that is the live
        window's job, where it can be tested against a mocked clock.
        """
        rows = await self._get(
            "sessions",
            {
                "session_key": session_key,
                "year": year,
                "country_name": country_name,
            },
        )
        return self._parse(rows, Session, "session", session_key)

    @staticmethod
    def _parse(
        rows: list[dict[str, Any]],
        model: type[ModelT],
        label: str,
        session_key: Any,
    ) -> list[ModelT]:
        parsed: list[ModelT] = []
        skipped = 0
        for row in rows:
            try:
                parsed.append(model.model_validate(row))
            except ValidationError:
                skipped += 1
                logger.warning("Skipping unparseable %s row: %r", label, row)

        if skipped:
            logger.warning(
                "Dropped %d of %d %s rows for session_key=%s",
                skipped,
                len(rows),
                label,
                session_key,
            )
        return parsed
