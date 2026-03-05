#!/usr/bin/env python3
"""Session bootstrap helper for ZenRipple + MCPorter workflows."""

import argparse
import asyncio
import os
from pathlib import Path
import sys

import websockets

from zenripple_session_file import write_session_file


DEFAULT_WS_URL = os.environ.get("ZENRIPPLE_WS_URL", "ws://localhost:9876")


def _read_auth_token() -> str:
    """Read auth token from env var or ~/.zenripple/auth file."""
    from_env = os.environ.get("ZENRIPPLE_AUTH_TOKEN", "").strip()
    if from_env:
        return from_env
    auth_file = Path.home() / ".zenripple" / "auth"
    try:
        return auth_file.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return ""


async def _create_session(ws_url: str) -> str:
    token = _read_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with websockets.connect(f"{ws_url}/new", additional_headers=headers) as ws:
        resp_headers = None
        if hasattr(ws, "response") and ws.response:
            resp_headers = ws.response.headers
        elif hasattr(ws, "response_headers"):
            resp_headers = ws.response_headers

        session_id = resp_headers.get("X-ZenRipple-Session") if resp_headers else None
        if not session_id:
            raise RuntimeError("Missing X-ZenRipple-Session header from ZenRipple")
        return session_id


def _print_value(value: str, shell: bool) -> None:
    if shell:
        print(f"export ZENRIPPLE_SESSION_ID={value}")
    else:
        print(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or resolve ZenRipple session IDs for MCPorter/CLI use."
    )
    parser.add_argument(
        "mode",
        choices=("new", "ensure"),
        help="'new' creates a fresh browser session; 'ensure' reuses ZENRIPPLE_SESSION_ID if set.",
    )
    parser.add_argument(
        "--ws-url",
        default=DEFAULT_WS_URL,
        help=f"ZenRipple websocket base URL (default: {DEFAULT_WS_URL})",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Print in shell export format: export ZENRIPPLE_SESSION_ID=...",
    )
    parser.add_argument(
        "--write-file",
        action="store_true",
        help="Also persist session ID to ~/.zenripple/sessions/<caller_key> for auto-session reuse.",
    )
    args = parser.parse_args()

    try:
        if args.mode == "ensure":
            existing = os.environ.get("ZENRIPPLE_SESSION_ID", "").strip()
            if existing:
                _print_value(existing, args.shell)
                if args.write_file:
                    write_session_file(existing)
                return 0

        created = asyncio.run(_create_session(args.ws_url))
        _print_value(created, args.shell)
        if args.write_file:
            write_session_file(created)
        return 0
    except Exception as exc:  # pragma: no cover - simple CLI fallback path
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
