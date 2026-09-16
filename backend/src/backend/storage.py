"""Persisting decisions so a race can be reviewed after it has finished.

SQLite, for one reason: this records one stream at a time at roughly one row
per lap, which is a workload a single file handles without a server to run,
back up or pay for.

DURABILITY WARNING: on a host with an ephemeral filesystem -- Render's free
tier, most container platforms without a mounted volume -- this file is lost
on every deploy and every spin-down. It persists within a session but not
across them. A mounted disk or a hosted database is needed for anything more.

No storage interface here, deliberately. TickSource was written as an ABC with
one implementation because a second one was known and dated. A second storage
backend is speculative, so this is a concrete class with a narrow surface;
replacing it later is a contained change. An interface added for symmetry
would be the over-engineering that case was not.
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import DecisionMessage, StartMessage

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key     TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    driver_number   INTEGER,
    total_laps      INTEGER,
    started_at      TEXT    NOT NULL,
    ended_at        TEXT,
    total_ticks     INTEGER NOT NULL DEFAULT 0,
    total_decisions INTEGER NOT NULL DEFAULT 0,
    end_reason      TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    lap         INTEGER NOT NULL,
    recorded_at TEXT    NOT NULL,
    verdict     TEXT    NOT NULL,
    compound    TEXT    NOT NULL,
    tyre_age    INTEGER NOT NULL,
    payload     TEXT    NOT NULL,
    PRIMARY KEY (run_id, lap)
);

CREATE INDEX IF NOT EXISTS idx_runs_started  ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions (run_id, lap);
"""


@dataclass
class RunSummary:
    """One recorded stream."""

    id: int
    session_key: str
    source: str
    driver_number: int | None
    total_laps: int | None
    started_at: str
    ended_at: str | None
    total_ticks: int
    total_decisions: int
    end_reason: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionStore:
    """Records runs and the decisions emitted during them.

    Every public method is async and does its SQLite work in a worker thread.
    sqlite3 is synchronous, and calling it directly from the event loop would
    stall every other connected client for the duration of the write -- on a
    server whose whole job is streaming, that is the one thing it must not do.

    A single connection is shared under a lock. SQLite allows cross-thread use
    with check_same_thread=False provided access is serialised, which the lock
    guarantees, and WAL mode lets the read endpoints run while a stream writes.
    """

    def __init__(self, path: str, *, max_runs: int = 200) -> None:
        self._path = path
        self._max_runs = max_runs
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # WAL keeps readers from blocking the writer; foreign keys are off by
        # default in SQLite, so the cascade on decisions needs enabling.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        connection.commit()
        self._connection = connection
        logger.info("Decision log ready at %s", self._path)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    # --- writing ----------------------------------------------------------

    async def start_run(self, start: StartMessage) -> int | None:
        """Record the beginning of a stream and return its id."""
        return await self._guarded("start_run", self._start_run_sync, start)

    def _start_run_sync(self, start: StartMessage) -> int:
        assert self._connection is not None
        cursor = self._connection.execute(
            "INSERT INTO runs (session_key, source, driver_number, total_laps,"
            " started_at) VALUES (?, ?, ?, ?, ?)",
            (
                start.session_key,
                start.source,
                start.driver_number,
                start.total_laps,
                _now(),
            ),
        )
        self._connection.commit()
        self._prune_sync()
        return int(cursor.lastrowid)

    async def record_decision(self, run_id: int, decision: DecisionMessage) -> None:
        await self._guarded(
            "record_decision", self._record_decision_sync, run_id, decision
        )

    def _record_decision_sync(self, run_id: int, decision: DecisionMessage) -> None:
        assert self._connection is not None
        # The full message is stored as JSON alongside a few columns worth
        # querying. DecisionMessage has gained fields three times in a week;
        # a column per field would mean a migration each time, and the log's
        # job is to reproduce what was actually sent, whatever shape it was.
        self._connection.execute(
            "INSERT OR REPLACE INTO decisions (run_id, lap, recorded_at, verdict,"
            " compound, tyre_age, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                decision.lap,
                _now(),
                decision.verdict,
                decision.compound,
                decision.tyre_age,
                json.dumps(decision.model_dump(mode="json")),
            ),
        )
        self._connection.commit()

    async def finish_run(
        self, run_id: int, *, total_ticks: int, total_decisions: int, reason: str
    ) -> None:
        await self._guarded(
            "finish_run",
            self._finish_run_sync,
            run_id,
            total_ticks,
            total_decisions,
            reason,
        )

    def _finish_run_sync(
        self, run_id: int, total_ticks: int, total_decisions: int, reason: str
    ) -> None:
        assert self._connection is not None
        self._connection.execute(
            "UPDATE runs SET ended_at = ?, total_ticks = ?, total_decisions = ?,"
            " end_reason = ? WHERE id = ?",
            (_now(), total_ticks, total_decisions, reason, run_id),
        )
        self._connection.commit()

    def _prune_sync(self) -> None:
        """Drop the oldest runs beyond the cap, so the file cannot grow forever."""
        assert self._connection is not None
        self._connection.execute(
            "DELETE FROM runs WHERE id NOT IN ("
            " SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
            (self._max_runs,),
        )
        self._connection.commit()

    # --- reading ----------------------------------------------------------

    async def list_runs(self, limit: int = 50) -> list[RunSummary]:
        result = await self._guarded("list_runs", self._list_runs_sync, limit)
        return result or []

    def _list_runs_sync(self, limit: int) -> list[RunSummary]:
        assert self._connection is not None
        rows = self._connection.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunSummary(**dict(row)) for row in rows]

    async def get_run(self, run_id: int) -> RunSummary | None:
        return await self._guarded("get_run", self._get_run_sync, run_id)

    def _get_run_sync(self, run_id: int) -> RunSummary | None:
        assert self._connection is not None
        row = self._connection.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return RunSummary(**dict(row)) if row else None

    async def get_decisions(self, run_id: int) -> list[dict]:
        result = await self._guarded("get_decisions", self._get_decisions_sync, run_id)
        return result or []

    def _get_decisions_sync(self, run_id: int) -> list[dict]:
        assert self._connection is not None
        rows = self._connection.execute(
            "SELECT payload FROM decisions WHERE run_id = ? ORDER BY lap", (run_id,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # --- failure containment ---------------------------------------------

    async def _guarded(self, label: str, fn, *args):
        """Run a database operation without letting it break the caller.

        The decision log is a record of a race, not part of serving one. A
        disk that is full, read-only or gone must degrade to "no log" rather
        than ending a stream somebody is watching -- the same principle as the
        decision engine's own error containment.
        """
        if self._connection is None:
            return None
        try:
            async with self._lock:
                return await asyncio.to_thread(fn, *args)
        except Exception:
            logger.exception("Decision log %s failed; continuing without it.", label)
            return None
