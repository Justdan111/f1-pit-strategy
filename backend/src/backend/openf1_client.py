"""Thin async client for the OpenF1 REST API (https://openf1.org).

Scope today (Day 1): fetch stints for a session_key. That's all replay mode
needs. Day 4 will add the session-window lookups live mode needs, plus an
explicit rate limiter — OpenF1's free tier is roughly 3 req/s and 30 req/min
(SPEC section 10), and blowing through that mid-race is the worst possible
failure for the primary use case. One request per replay connection is
comfortably under it, so no limiter today; the note is here so Day 4 doesn't
"discover" the requirement late.

Design notes:

- The client does not own its httpx.AsyncClient. It is passed one. That keeps
  connection-pool lifetime a decision for the application (see main.py's
  lifespan), so we aren't opening a fresh TCP connection and TLS handshake
  per WebSocket connection, and so tests can inject a mock transport.

- Every failure mode becomes a specific, named exception. SPEC section 7.1:
  "fails loudly and specifically on unreachable API, bad session_key, or
  unexpected response shape." A caller can then map each to a sensible
  `error` envelope instead of showing the user a stack trace.

- Rows that don't validate are dropped, not fatal. If OpenF1 returns 40 good
  stints and one with a null lap_end (it happens — a car that retired), a
  hard failure would make the whole race unusable. We skip the bad row and
  carry on. The one thing we refuse to do is *guess* at a missing value.
"""

import logging
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings
from .models import Lap, Stint

logger = logging.getLogger(__name__)


class OpenF1Error(Exception):
    """Base class for anything that went wrong talking to OpenF1."""

    code = "openf1_error"


class OpenF1Unavailable(OpenF1Error):
    """Could not reach OpenF1, or it returned a server error / timed out."""

    code = "openf1_unavailable"


class OpenF1BadResponse(OpenF1Error):
    """OpenF1 answered, but not with anything we could understand."""

    code = "openf1_bad_response"


class OpenF1Client:
    """Async wrapper over the OpenF1 endpoints this project uses."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._base_url = settings.openf1_base_url.rstrip("/")
        self._timeout = settings.openf1_timeout_seconds

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """GET one OpenF1 endpoint and return its rows.

        OpenF1 returns a JSON array at the top level for these endpoints.
        Anything else is treated as an unexpected shape rather than being
        coerced, because quietly accepting a surprise here would push the
        confusion downstream into the replay logic.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"

        # Drop None params so callers can pass optional filters unconditionally.
        clean = {k: v for k, v in params.items() if v is not None}

        try:
            response = await self._http.get(url, params=clean, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise OpenF1Unavailable(
                f"OpenF1 timed out after {self._timeout}s calling {path}."
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenF1Unavailable(f"Could not reach OpenF1 at {url}: {exc}") from exc

        if response.status_code >= 500:
            raise OpenF1Unavailable(
                f"OpenF1 returned {response.status_code} for {path}. "
                "The API is having problems; this is not a problem with the request."
            )
        # API quirk, confirmed against the real API: OpenF1 answers a query
        # that simply matched nothing with 404 {"detail": "No results found."}
        # rather than 200 []. That is not a rejected request, it is an empty
        # result, so we normalise it to an empty list and let the caller
        # decide what "no data" means. Any other 404 is a real problem.
        if response.status_code == 404 and "No results found" in response.text:
            logger.info("OpenF1 has no results for %s with params %r", path, clean)
            return []

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
        """Fetch stints for a session, optionally narrowed to one driver.

        Returns every stint OpenF1 knows about for that session, in the order
        it gave them. Filtering to a single driver and ordering by lap is
        ReplayTickSource's job, not the client's — the client's only
        responsibility is "HTTP in, validated models out".
        """
        rows = await self._get(
            "stints",
            {"session_key": session_key, "driver_number": driver_number},
        )

        stints: list[Stint] = []
        skipped = 0
        for row in rows:
            try:
                stints.append(Stint.model_validate(row))
            except ValidationError:
                # Usually a null lap_end (a car still running, or retired
                # mid-stint). Not fatal for the rest of the session.
                skipped += 1
                logger.warning("Skipping unparseable stint row: %r", row)

        if skipped:
            logger.warning(
                "Dropped %d of %d stint rows for session_key=%s",
                skipped,
                len(rows),
                session_key,
            )

        return stints

    async def get_laps(
        self,
        session_key: str,
        driver_number: int | None = None,
    ) -> list[Lap]:
        """Fetch per-lap timing for a session, optionally narrowed to one driver.

        Day 2 needs this: stints tell you which tyre was on the car, laps tell
        you how fast it went. A degradation curve needs both.

        Same contract as get_stints — HTTP in, validated models out. Laps with
        a null lap_duration are returned as-is rather than dropped: the tick
        for that lap is still real and still belongs in the stream, it just
        can't contribute a data point to the fit. Deciding what to do about a
        missing lap time is the decision engine's call, not the client's.
        """
        rows = await self._get(
            "laps",
            {"session_key": session_key, "driver_number": driver_number},
        )

        laps: list[Lap] = []
        skipped = 0
        for row in rows:
            try:
                laps.append(Lap.model_validate(row))
            except ValidationError:
                skipped += 1
                logger.warning("Skipping unparseable lap row: %r", row)

        if skipped:
            logger.warning(
                "Dropped %d of %d lap rows for session_key=%s",
                skipped,
                len(rows),
                session_key,
            )

        return laps
