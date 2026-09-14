"""Hand-checked scenarios for the decision engine.

DAY2.md: "a decision engine that runs without crashing but gives bad advice is
worse than one that visibly fails, since a bad number looks fine until you
check it by hand."

Every expected value below was computed by hand from the stated line and tyre
age BEFORE running the engine, and is hardcoded here. If the engine's output
drifts, this fails. Run:  uv run python scripts/check_decisions.py

SUPERSEDED (Day 4): these scenarios now live in tests/test_decision_engine.py
and run under pytest with `uv run pytest`. This script is kept because it
prints the full comparison table, which is useful when working on the engine
by hand; the pytest version is what CI and regressions rely on.
"""

import sys

from backend.config import Settings
from backend.decision_engine import DecisionEngine
from backend.models import TickMessage

SETTINGS = Settings()
TOL = 0.005

failures: list[str] = []


def tick(lap, age, compound, duration, out=False):
    return TickMessage(
        lap=lap,
        driver_number=1,
        compound=compound,
        tyre_age=age,
        stint_number=1,
        lap_duration_s=duration,
        is_pit_out_lap=out,
    )


def run(name, samples, final, expected):
    """Feed samples, then `final`, and compare the decision with expectations."""
    engine = DecisionEngine(SETTINGS)
    for t in samples:
        engine.observe(t)
    decision = engine.observe(final)

    print(f"\n{name}")
    if decision is None:
        got = {"decision": None}
    else:
        got = decision.model_dump()

    ok = True
    for key, want in expected.items():
        have = got.get(key)
        if isinstance(want, float) and isinstance(have, (int, float)):
            match = abs(have - want) < TOL
        else:
            match = have == want
        flag = "ok " if match else "FAIL"
        if not match:
            ok = False
        print(f"  [{flag}] {key:<42} expected={want!r:<12} actual={have!r}")
    if not ok:
        failures.append(name)
    return decision


# --- A: nearly-new MEDIUM, line 90.0 + 0.08*age, current age 4 -------------
run(
    "A  MEDIUM, m=0.08, age=4  -> stay_out, break-even 68.75 laps",
    [tick(n + 1, n, "MEDIUM", 90.0 + 0.08 * n) for n in range(0, 4)],
    tick(5, 4, "MEDIUM", 90.0 + 0.08 * 4),
    {
        "verdict": "stay_out",
        "current_compound_degradation_s_per_lap": 0.08,
        "fresh_tyre_advantage_s_per_lap": 0.32,
        "projected_time_current_tyres_s": 90.40,
        "projected_time_fresh_tyres_s": 90.08,
        "delta_s": -21.68,
        "laps_to_break_even": 68.75,
        "degradation_is_measurable": True,
        "fit_r_squared": 1.0,
    },
)

# --- B: old SOFT, line 89.0 + 0.11*age, current age 19 ---------------------
run(
    "B  SOFT, m=0.11, age=19   -> stay_out, break-even 10.53 laps",
    [tick(40 + n, n, "SOFT", 89.0 + 0.11 * n) for n in (2, 6, 10, 14, 18)],
    tick(58, 19, "SOFT", 89.0 + 0.11 * 19),
    {
        "verdict": "stay_out",
        "current_compound_degradation_s_per_lap": 0.11,
        "fresh_tyre_advantage_s_per_lap": 2.09,
        "projected_time_current_tyres_s": 91.20,
        "projected_time_fresh_tyres_s": 89.11,
        "delta_s": -19.91,
        "laps_to_break_even": 10.53,
        "degradation_is_measurable": True,
    },
)

# --- C: destroyed tyre, line 95.0 + 1.5*age, current age 20 ----------------
c = run(
    "C  m=1.50, age=20        -> PIT_NOW, delta +8.00s",
    [tick(10 + n, n, "SOFT", 95.0 + 1.5 * n) for n in (10, 14, 18)],
    tick(30, 20, "SOFT", 95.0 + 1.5 * 20),
    {
        "verdict": "pit_now",
        "current_compound_degradation_s_per_lap": 1.5,
        "fresh_tyre_advantage_s_per_lap": 30.0,
        "projected_time_current_tyres_s": 126.50,
        "projected_time_fresh_tyres_s": 96.50,
        "delta_s": 8.0,
        "laps_to_break_even": 0.73,
        "degradation_is_measurable": True,
    },
)

# --- D: negative slope (fuel burn), line 105.0 - 0.05*age, age 25 ----------
d = run(
    "D  m=-0.05, age=25       -> stay_out, no break-even, flagged",
    [tick(1 + n, n, "HARD", 105.0 - 0.05 * n) for n in (0, 10, 20)],
    tick(30, 25, "HARD", 105.0 - 0.05 * 25),
    {
        "verdict": "stay_out",
        "current_compound_degradation_s_per_lap": -0.05,
        "fresh_tyre_advantage_s_per_lap": -1.25,
        "projected_time_current_tyres_s": 103.70,
        "projected_time_fresh_tyres_s": 104.95,
        "delta_s": -23.25,
        "laps_to_break_even": None,
        "degradation_is_measurable": False,
    },
)

# --- E: fewer than min_samples_for_fit -> no decision, no crash ------------
print("\nE  insufficient data      -> observe() returns None until 3 samples")
engine = DecisionEngine(SETTINGS)
results = [
    engine.observe(tick(1, 0, "MEDIUM", 90.00)),
    engine.observe(tick(2, 1, "MEDIUM", 90.08)),
    engine.observe(tick(3, 2, "MEDIUM", 90.16)),
]
shape = [r is None for r in results]
want = [True, True, False]
ok = shape == want
print(f"  [{'ok ' if ok else 'FAIL'}] None-ness after 1,2,3 samples       "
      f"expected={want} actual={shape}")
if not ok:
    failures.append("E insufficient data")

# --- F: fit hygiene -- contaminated laps must be excluded ------------------
print("\nF  contaminated input     -> outliers excluded, slope recovers to 0.10")
engine = DecisionEngine(SETTINGS)
clean = [tick(n + 1, n, "MEDIUM", 90.0 + 0.10 * n) for n in range(0, 10)]
noise = [
    tick(90, 0, "MEDIUM", 108.0, out=True),   # out-lap, flagged
    tick(91, 1, "MEDIUM", 150.0),             # safety car
    tick(92, 2, "MEDIUM", 150.0),             # safety car
]
for t in clean[:-1] + noise:
    engine.observe(t)
f = engine.observe(clean[-1])
checks = {
    "current_compound_degradation_s_per_lap": 0.10,
    "samples_used": 10,
    "samples_seen": 13,
    "fit_r_squared": 1.0,
}
ok = True
for k, want_v in checks.items():
    have = getattr(f, k)
    match = abs(have - want_v) < TOL if isinstance(want_v, float) else have == want_v
    ok &= match
    print(f"  [{'ok ' if match else 'FAIL'}] {k:<42} expected={want_v!r:<12} actual={have!r}")
if not ok:
    failures.append("F fit hygiene")

# --- notes attached to C and D --------------------------------------------
print("\nnotes emitted:")
print(f"  C: {c.note}")
print(f"  D: {d.note}")

print("\n" + "=" * 70)
if failures:
    print(f"FAILED: {len(failures)} scenario(s): {failures}")
    sys.exit(1)
print("All scenarios match the hand-computed values.")
