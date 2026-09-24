"""Terminal WebSocket client, since curl cannot speak WebSocket.

Prints every message with the elapsed time since connecting and validates it
against the protocol models. If ticks all show the same timestamp, it is not
streaming.

    uv run python scripts/ws_client.py
    uv run python scripts/ws_client.py --session-key 9904 --driver-number 1
    uv run python scripts/ws_client.py --session-key latest --mode live
"""

import argparse
import asyncio
import json
import os
import time
from urllib.parse import urlencode

import websockets
from pydantic import TypeAdapter, ValidationError

from backend.models import StreamMessage

MESSAGE_ADAPTER = TypeAdapter(StreamMessage)


def build_url(args: argparse.Namespace) -> str:
    params: dict[str, str] = {"mode": args.mode}
    if args.tick_interval is not None:
        params["tick_interval"] = str(args.tick_interval)
    if args.driver_number is not None:
        params["driver_number"] = str(args.driver_number)
    return f"{args.host}/ws/race/{args.session_key}?{urlencode(params)}"


async def run(args: argparse.Namespace) -> int:
    url = build_url(args)
    print(f"connecting: {url}\n")

    # A header, not a query parameter: the key would otherwise be written to
    # the server's access log on every connection.
    headers = {}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    started = time.perf_counter()
    counts: dict[str, int] = {}
    exit_code = 0

    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            async for raw in ws:
                elapsed = time.perf_counter() - started
                payload = json.loads(raw)

                try:
                    message = MESSAGE_ADAPTER.validate_python(payload)
                    kind = message.type
                    rendered = message.model_dump(mode="json")
                except ValidationError as exc:
                    kind = "INVALID"
                    rendered = {"raw": payload, "error": str(exc)}
                    exit_code = 1

                counts[kind] = counts.get(kind, 0) + 1

                if kind == "decision" and not args.raw:
                    # Show only the fields that drive the call; --raw for all.
                    d = rendered
                    be = d["laps_to_break_even"]
                    be_txt = f"{be:>5.1f} laps" if be is not None else "     n/a  "
                    print(
                        f"[{elapsed:7.3f}s] {kind:<8} "
                        f"{d['verdict']:<8} "
                        f"deg={d['current_compound_degradation_s_per_lap']:+.4f}s/lap "
                        f"adv={d['fresh_tyre_advantage_s_per_lap']:+6.2f}s/lap "
                        f"breakeven={be_txt} "
                        f"delta={d['delta_s']:+8.2f}s "
                        f"n={d['samples_used']}/{d['samples_seen']} "
                        f"r2={d['fit_r_squared']:.3f}"
                    )
                else:
                    print(f"[{elapsed:7.3f}s] {kind:<8} {json.dumps(rendered)}")

                if kind == "error":
                    exit_code = 1
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        print(f"\nconnection failed: {exc}")
        print("is the server running?  uv run uvicorn backend.main:app --reload")
        return 2

    total = time.perf_counter() - started
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"
    print(f"\nclosed after {total:.3f}s  ({summary})")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="ws://127.0.0.1:8000")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("F1_API_KEY"),
        help="API key, if the server requires one. Defaults to $F1_API_KEY.",
    )
    parser.add_argument("--session-key", default="sample")
    parser.add_argument("--mode", default="replay", choices=["replay", "live"])
    parser.add_argument("--driver-number", type=int, required=True)
    parser.add_argument(
        "--raw", action="store_true", help="Print decision messages as raw JSON."
    )
    parser.add_argument(
        "--tick-interval",
        type=float,
        default=None,
        help="Override seconds between ticks (0 = as fast as possible).",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
