"""Run the decision engine against finished races and report how it did.

    uv run python scripts/backtest.py                    # a default set of 2025 races
    uv run python scripts/backtest.py --sessions 9904,9987
    uv run python scripts/backtest.py --json out.json

Three numbers, proving different things -- see backend/backtest.py.
"""

import argparse
import asyncio
import json
import sys

import httpx

from backend.backtest import (
    BATCH_REQUEST_INTERVAL_SECONDS,
    RaceResult,
    accuracy,
    backtest_session_with_retry,
)
from backend.config import Settings
from backend.openf1_client import OpenF1Client, RateLimiter

# A spread of 2025 circuits: street, high-degradation, low-degradation.
DEFAULT_SESSIONS: list[tuple[str, str]] = [
    ("9904", "Baku"),
    ("9693", "Melbourne"),
    ("9998", "Shanghai"),
    ("10006", "Suzuka"),
    ("10014", "Sakhir"),
    ("10022", "Jeddah"),
    ("9971", "Barcelona"),
    ("9947", "Silverstone"),
]

PREDICTORS = ["model", "persistence", "compound_mean"]

# Three or more stops in a dry race is vanishingly rare; it almost always means
# rain or a red flag. Weather is out of scope, so those races are reported but
# kept out of the headline average.
MANY_STOPS = 3


def render(results: list[RaceResult]) -> str:
    out: list[str] = []
    scored = [r for r in results if not r.skipped_reason]

    out.append("=" * 78)
    out.append("PER RACE")
    out.append("=" * 78)
    out.append(
        f"{'race':<13}{'laps':>5}{'dec':>5}{'meas':>6}"
        f"{'rec pit':>9}{'actual stops':>16}{'model MAE':>11}{'persist':>9}"
    )
    for r in results:
        if r.skipped_reason:
            out.append(f"{r.label:<14}  skipped: {r.skipped_reason}")
            continue
        clean = [p for p in r.predictions if p.next_lap_is_clean]
        model = accuracy(clean, "model")
        persist = accuracy(clean, "persistence")
        rec = str(r.recommended_pit_lap) if r.recommended_pit_lap else "never"
        actual = ",".join(str(p) for p in r.actual_pit_laps) or "-"
        if len(actual) > 15:
            actual = actual[:12] + "..."
        out.append(
            f"{r.label:<13}{r.total_laps:>5}{r.decisions:>5}"
            f"{r.decisions_with_measurable_degradation:>6}"
            f"{rec:>9}{actual:>16}"
            f"{(f'{model.mae:.3f}s' if model else '-'):>11}"
            f"{(f'{persist.mae:.3f}s' if persist else '-'):>9}"
        )

    if not scored:
        out.append("")
        out.append("  No races were scored. Nothing below is meaningful.")
        return "\n".join(out)

    all_predictions = [p for r in scored for p in r.predictions]
    clean_predictions = [p for p in all_predictions if p.next_lap_is_clean]

    out.append("")
    out.append("=" * 78)
    out.append("1. PREDICTION ACCURACY  (the only non-circular measurement)")
    out.append("=" * 78)
    out.append("Next-lap time predicted from laps seen so far, vs what happened.")
    for title, sample in [
        ("clean laps only", clean_predictions),
        ("all laps, unfiltered", all_predictions),
    ]:
        out.append("")
        out.append(f"  {title}  (n={len(sample)})")
        out.append(f"    {'predictor':<16}{'MAE':>10}{'RMSE':>10}{'median AE':>12}")
        for predictor in PREDICTORS:
            a = accuracy(sample, predictor)
            if a is None:
                continue
            out.append(
                f"    {a.predictor:<16}{a.mae:>9.3f}s{a.rmse:>9.3f}s{a.median_ae:>11.3f}s"
            )
        model = accuracy(sample, "model")
        persist = accuracy(sample, "persistence")
        mean = accuracy(sample, "compound_mean")
        if model and persist and mean:
            # Mean and median can disagree, and saying only one of them would
            # be picking the flattering number. A lower MAE with a higher
            # median means the model is worse on a typical lap and better on
            # the awkward ones -- worth stating, not smoothing over.
            mae_delta = min(persist.mae, mean.mae) - model.mae
            med_delta = min(persist.median_ae, mean.median_ae) - model.median_ae
            out.append(
                f"    -> vs best baseline:  MAE {mae_delta:+.3f}s   "
                f"median AE {med_delta:+.3f}s   (positive = model better)"
            )
            if (mae_delta > 0) != (med_delta > 0):
                out.append(
                    "       mean and median disagree: the model is "
                    + ("better on outliers, worse on typical laps"
                       if mae_delta > 0
                       else "better on typical laps, worse on outliers")
                )

    out.append("")
    out.append("=" * 78)
    out.append("2. COUNTERFACTUAL PIT LAP  (circular -- internal consistency only)")
    out.append("=" * 78)
    out.append("Modelled best pit lap, scored by the same curves being evaluated.")
    out.append("")
    cf = [(r, r.counterfactual) for r in scored if r.counterfactual]
    if not cf:
        out.append("  No one-stop races with fittable curves in this set.")
    else:
        out.append(
            f"    {'race':<14}{'actual':>8}{'modelled':>10}{'diff':>7}{'saving':>11}  note"
        )
        for r, c in cf:
            flag = "  degenerate (hit sweep edge)" if c.hit_sweep_boundary else ""
            out.append(
                f"    {r.label:<14}{c.actual_pit_lap:>8}{c.modelled_best_pit_lap:>10}"
                f"{c.modelled_best_pit_lap - c.actual_pit_lap:>+7}"
                f"{c.modelled_saving_s:>10.1f}s{flag}"
            )
        usable = [(r, c) for r, c in cf if not c.hit_sweep_boundary]
        degenerate = len(cf) - len(usable)
        out.append("")
        if degenerate:
            out.append(
                f"    {degenerate} of {len(cf)} optima landed on the edge of the swept"
            )
            out.append(
                "    range. That happens when the second compound simply fits faster,"
            )
            out.append(
                "    so the sweep degenerates to 'pit immediately'. The model has no"
            )
            out.append(
                "    notion of minimum stint length, so these say nothing about"
            )
            out.append("    strategy and are excluded below.")
        if usable:
            diffs = [abs(c.modelled_best_pit_lap - c.actual_pit_lap) for _, c in usable]
            out.append(
                f"    mean |difference| over the {len(usable)} non-degenerate race(s): "
                f"{sum(diffs)/len(diffs):.1f} laps"
            )
        else:
            out.append("    No non-degenerate results: this measure told us nothing here.")

    out.append("")
    out.append("=" * 78)
    out.append("3. GAP TO THE ACTUAL PIT LAP  (context, not ground truth)")
    out.append("=" * 78)
    out.append("Teams also optimise track position, which this tool does not model,")
    out.append("so a gap is not necessarily an error on either side.")
    out.append("")
    gaps = [
        (r, r.recommended_pit_lap - r.actual_pit_laps[0])
        for r in scored
        if r.recommended_pit_lap and r.actual_pit_laps
    ]
    never = [r for r in scored if r.recommended_pit_lap is None]
    if gaps:
        out.append(
            f"    {'race':<14}{'recommended':>13}{'actual':>8}{'gap':>7}  note"
        )
        for r, gap in gaps:
            flag = (
                f"  {len(r.actual_pit_laps)} stops: likely weather/red flag"
                if len(r.actual_pit_laps) >= MANY_STOPS
                else ""
            )
            out.append(
                f"    {r.label:<14}{r.recommended_pit_lap:>13}"
                f"{r.actual_pit_laps[0]:>8}{gap:>+7}{flag}"
            )

        dry = [(r, g) for r, g in gaps if len(r.actual_pit_laps) < MANY_STOPS]
        wet = len(gaps) - len(dry)
        out.append("")
        out.append(
            f"    mean |gap|, all {len(gaps)} race(s): "
            f"{sum(abs(g) for _, g in gaps)/len(gaps):.1f} laps"
        )
        if dry and wet:
            out.append(
                f"    mean |gap|, excluding the {wet} race(s) with "
                f"{MANY_STOPS}+ stops: "
                f"{sum(abs(g) for _, g in dry)/len(dry):.1f} laps over {len(dry)}"
            )
            out.append(
                "    A race with that many stops is almost certainly wet or red-"
                "flagged."
            )
            out.append(
                "    Weather is explicitly out of scope, so those comparisons are"
            )
            out.append("    not meaningful in either direction.")
    out.append("")
    out.append(
        f"    recommended no stop at all in {len(never)} of {len(scored)} race(s)"
    )

    measurable = sum(r.decisions_with_measurable_degradation for r in scored)
    total_dec = sum(r.decisions for r in scored)
    out.append("")
    out.append("=" * 78)
    out.append("SIGNAL QUALITY")
    out.append("=" * 78)
    out.append("Where the slope's 95% confidence interval sat relative to zero.")
    out.append("")
    counts: dict[str, int] = {}
    for r in scored:
        for key, value in r.significance_counts.items():
            counts[key] = counts.get(key, 0) + value
    labels = {
        "positive": "positive  - degradation confidently real",
        "unclear": "unclear   - interval spans zero, cannot tell",
        "negative": "negative  - confidently getting faster (fuel burn)",
    }
    total_sig = sum(counts.values()) or 1
    for key in ("positive", "unclear", "negative"):
        n = counts.get(key, 0)
        out.append(f"    {labels[key]:<48}{n:>6}  {100*n/total_sig:>5.1f}%")
    out.append("")
    out.append(
        "  A point estimate alone cannot tell the second row from the third."
    )
    out.append(
        "  The confidence interval is what separates 'this tyre is not slowing'"
    )
    out.append("  from 'we do not yet have the data to say'.")

    out.append("")
    out.append("=" * 78)
    out.append("CAVEAT")
    out.append("=" * 78)
    if total_dec:
        pct = 100 * measurable / total_dec
        out.append(
            f"  Degradation was measurable (fitted slope > 0) in {measurable} of "
            f"{total_dec} decisions ({pct:.0f}%)."
        )
    out.append(
        "  Fitting lap time against tyre age measures degradation MINUS fuel burn,"
    )
    out.append(
        "  which is roughly -0.03 s/lap. Where the slope comes out flat or negative"
    )
    out.append(
        "  the engine reports no break-even point rather than inventing one, so it"
    )
    out.append("  will decline to recommend a stop.")
    return "\n".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions", help="Comma-separated session keys. Defaults to a 2025 set."
    )
    parser.add_argument("--driver-number", type=int, default=None)
    parser.add_argument("--json", help="Also write the raw results here.")
    args = parser.parse_args()

    if args.sessions:
        targets = [(k.strip(), k.strip()) for k in args.sessions.split(",") if k.strip()]
    else:
        targets = DEFAULT_SESSIONS

    settings = Settings()
    results: list[RaceResult] = []

    async with httpx.AsyncClient(headers={"Accept": "application/json"}) as http:
        # Paced for batch work rather than for a live session.
        client = OpenF1Client(
            http, settings, rate_limiter=RateLimiter(BATCH_REQUEST_INTERVAL_SECONDS)
        )
        for key, label in targets:
            print(f"fetching {label} ({key})...", file=sys.stderr)
            try:
                results.append(
                    await backtest_session_with_retry(
                        client, key, driver_number=args.driver_number,
                        label=label, settings=settings,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad race must not stop the run
                print(f"  failed: {exc}", file=sys.stderr)

    print()
    print(render(results))

    if args.json:
        import dataclasses
        with open(args.json, "w") as fh:
            json.dump([dataclasses.asdict(r) for r in results], fh, indent=2)
        print(f"\nraw results written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
