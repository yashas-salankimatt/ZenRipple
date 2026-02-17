#!/usr/bin/env python3
"""Session bootstrap helper for ZenLeap + MCPorter workflows."""

import argparse
import asyncio
import os
import sys

import websockets


DEFAULT_WS_URL = os.environ.get("ZENLEAP_WS_URL", "ws://localhost:9876")


async def _create_session(ws_url: str) -> str:
    async with websockets.connect(f"{ws_url}/new") as ws:
        headers = None
        if hasattr(ws, "response") and ws.response:
            headers = ws.response.headers
        elif hasattr(ws, "response_headers"):
            headers = ws.response_headers

        session_id = headers.get("X-ZenLeap-Session") if headers else None
        if not session_id:
            raise RuntimeError("Missing X-ZenLeap-Session header from ZenLeap agent")
        return session_id


def _print_value(value: str, shell: bool) -> None:
    if shell:
        print(f"export ZENLEAP_SESSION_ID={value}")
    else:
        print(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or resolve ZenLeap session IDs for MCPorter/CLI use."
    )
    parser.add_argument(
        "mode",
        choices=("new", "ensure"),
        help="'new' creates a fresh browser session; 'ensure' reuses ZENLEAP_SESSION_ID if set.",
    )
    parser.add_argument(
        "--ws-url",
        default=DEFAULT_WS_URL,
        help=f"ZenLeap websocket base URL (default: {DEFAULT_WS_URL})",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Print in shell export format: export ZENLEAP_SESSION_ID=...",
    )
    args = parser.parse_args()

    try:
        if args.mode == "ensure":
            existing = os.environ.get("ZENLEAP_SESSION_ID", "").strip()
            if existing:
                _print_value(existing, args.shell)
                return 0

        created = asyncio.run(_create_session(args.ws_url))
        _print_value(created, args.shell)
        return 0
    except Exception as exc:  # pragma: no cover - simple CLI fallback path
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
