"""Persisting decisions so a race can be reviewed afterwards."""

import json

import pytest

from backend.models import DecisionMessage, StartMessage
from backend.storage import DecisionStore


def start_message(session_key="sample", source="sample", total_laps=58):
    return StartMessage(
        session_key=session_key, source=source,
        total_laps=total_laps, driver_number=1,
    )


def decision(lap, verdict="stay_out", compound="SOFT", tyre_age=5):
    return DecisionMessage(
        lap=lap, driver_number=1, compound=compound, tyre_age=tyre_age,
        verdict=verdict,
        current_compound_degradation_s_per_lap=0.11,
        raw_degradation_s_per_lap=0.05,
        fuel_correction_s_per_lap=0.06,
        fit_intercept_s=89.0, fit_r_squared=0.97,
        samples_used=10, samples_seen=11,
        projected_time_current_tyres_s=91.2,
        projected_time_fresh_tyres_s=89.1,
        pit_lane_cost_s=22.0, delta_s=-19.9,
        fresh_tyre_advantage_s_per_lap=2.1,
        laps_to_break_even=10.5,
    )


@pytest.fixture
async def store():
    s = DecisionStore(":memory:", max_runs=5)
    await s.open()
    yield s
    await s.close()


# --- recording ------------------------------------------------------------


async def test_a_run_and_its_decisions_round_trip(store):
    run_id = await store.start_run(start_message())
    assert run_id is not None

    for lap in (4, 5, 6):
        await store.record_decision(run_id, decision(lap))
    await store.finish_run(
        run_id, total_ticks=58, total_decisions=3, reason="completed"
    )

    run = await store.get_run(run_id)
    assert run is not None
    assert run.session_key == "sample"
    assert run.source == "sample"
    assert run.total_laps == 58
    assert run.total_ticks == 58
    assert run.total_decisions == 3
    assert run.end_reason == "completed"
    assert run.ended_at is not None

    stored = await store.get_decisions(run_id)
    assert [d["lap"] for d in stored] == [4, 5, 6]


async def test_the_whole_message_is_preserved_not_just_indexed_columns(store):
    """The log must reproduce what was sent, whatever shape it was."""
    run_id = await store.start_run(start_message())
    original = decision(12)
    await store.record_decision(run_id, original)

    stored = (await store.get_decisions(run_id))[0]
    assert stored == json.loads(original.model_dump_json())


async def test_decisions_come_back_in_lap_order_regardless_of_insert_order(store):
    run_id = await store.start_run(start_message())
    for lap in (9, 4, 7, 5):
        await store.record_decision(run_id, decision(lap))
    assert [d["lap"] for d in await store.get_decisions(run_id)] == [4, 5, 7, 9]


async def test_recording_the_same_lap_twice_replaces_it(store):
    """One decision per lap. A retry must not duplicate a row."""
    run_id = await store.start_run(start_message())
    await store.record_decision(run_id, decision(7, verdict="stay_out"))
    await store.record_decision(run_id, decision(7, verdict="pit_now"))

    stored = await store.get_decisions(run_id)
    assert len(stored) == 1
    assert stored[0]["verdict"] == "pit_now"


async def test_runs_are_independent(store):
    first = await store.start_run(start_message(session_key="sample"))
    second = await store.start_run(start_message(session_key="9904"))
    await store.record_decision(first, decision(4))
    await store.record_decision(second, decision(9))

    assert [d["lap"] for d in await store.get_decisions(first)] == [4]
    assert [d["lap"] for d in await store.get_decisions(second)] == [9]


async def test_runs_are_listed_newest_first(store):
    ids = [await store.start_run(start_message()) for _ in range(3)]
    listed = await store.list_runs()
    assert [r.id for r in listed] == sorted(ids, reverse=True)


async def test_a_live_run_records_no_total_laps(store):
    """total_laps is None in live mode; the schema must accept that."""
    run_id = await store.start_run(
        start_message(session_key="9999", source="live", total_laps=None)
    )
    run = await store.get_run(run_id)
    assert run is not None
    assert run.total_laps is None
    assert run.source == "live"


# --- growth -----------------------------------------------------------------


async def test_old_runs_are_pruned_beyond_the_cap(store):
    """An unattended service must not grow without bound."""
    ids = [await store.start_run(start_message()) for _ in range(8)]
    listed = await store.list_runs(limit=100)
    assert len(listed) == 5
    assert [r.id for r in listed] == sorted(ids[-5:], reverse=True)


async def test_pruning_takes_the_decisions_with_it(store):
    """Otherwise the rows the cap was meant to bound stay behind."""
    first = await store.start_run(start_message())
    await store.record_decision(first, decision(4))
    for _ in range(8):
        await store.start_run(start_message())

    assert await store.get_run(first) is None
    assert await store.get_decisions(first) == []


# --- failure containment ---------------------------------------------------


async def test_a_failing_write_is_swallowed_not_raised(store, monkeypatch):
    """The log records a race; it is not part of serving one.

    A full or read-only disk must degrade to "no log" rather than ending a
    stream somebody is watching.
    """
    run_id = await store.start_run(start_message())

    def explode(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_record_decision_sync", explode)
    # Must not raise.
    await store.record_decision(run_id, decision(4))
    assert await store.get_decisions(run_id) == []


async def test_reads_also_degrade_rather_than_raise(store, monkeypatch):
    def explode(*_args):
        raise sqlite_error()

    def sqlite_error():
        import sqlite3

        return sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_list_runs_sync", explode)
    assert await store.list_runs() == []


async def test_an_unopened_store_is_inert(monkeypatch):
    """If open() never succeeded, every operation is a no-op."""
    never_opened = DecisionStore(":memory:")
    assert await never_opened.start_run(start_message()) is None
    assert await never_opened.list_runs() == []
    assert await never_opened.get_run(1) is None
    assert await never_opened.get_decisions(1) == []
    # And recording does not raise.
    await never_opened.record_decision(1, decision(4))


async def test_finishing_a_run_that_does_not_exist_is_harmless(store):
    await store.finish_run(9999, total_ticks=1, total_decisions=1, reason="completed")
    assert await store.get_run(9999) is None
