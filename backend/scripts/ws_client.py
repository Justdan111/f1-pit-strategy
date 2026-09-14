"""A terminal WebSocket client, so you can watch the stream yourself.

curl can't speak WebSocket, so this is the equivalent. It connects, prints
every message as it arrives with the elapsed time since connecting, and
validates each one against the protocol models — so if the server ever sends
something off-spec, this tells you rather than silently printing it.

Usage (from backend/):

    uv run python scripts/ws_client.py
    uv run python scripts/ws_client.py --session-key sample --tick-interval 0.1
    uv run python scripts/ws_client.py --session-key 9222 --mode replay
    uv run python scripts/ws_client.py --mode live          # expect an error envelope

The elapsed-time column is the point: if ticks all show the same timestamp,
you built a list, not a stream.
"""

import argparse
import asyncio
import json
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

    started = time.perf_counter()
    counts: dict[str, int] = {}
    exit_code = 0

    try:
        async with websockets.connect(url) as ws:
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
                print(f"[{elapsed:7.3f}s] {kind:<6} {json.dumps(rendered)}")

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
    parser.add_argument("--session-key", default="sample")
    parser.add_argument("--mode", default="replay", choices=["replay", "live"])
    parser.add_argument("--driver-number", type=int, default=None)
    parser.add_argument(
        "--tick-interval",
        type=float,
        default=None,
        help="Override seconds between ticks (0 = as fast as possible).",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
