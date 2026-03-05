#!/usr/bin/env python3
"""
ZenRipple MCP Server
Exposes Zen Browser control tools to Claude Code via Model Context Protocol.
Connects to the ZenRipple WebSocket server running in the browser.
"""

import asyncio
import base64
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows — file locking disabled
import functools
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import websockets

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from zenripple_session_file import (
    read_session_file as _read_session_file,
    write_session_file as _write_session_file,
    delete_session_file as _delete_session_file,
)

BROWSER_WS_URL = os.environ.get("ZENRIPPLE_WS_URL", "ws://localhost:9876")
SESSION_ID = os.environ.get("ZENRIPPLE_SESSION_ID", "")

# Grounding VLM configuration for browser_grounded_click.
# Uses an external VLM (default: Qwen3-VL-235B-A22B via OpenRouter) for accurate
# pixel coordinate prediction from screenshots.
# The API key is loaded from env var on startup, but also persisted in Firefox
# prefs so the user only needs to provide it once.
_GROUNDING_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_GROUNDING_MODEL = os.environ.get(
    "ZENRIPPLE_GROUNDING_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"
)
_GROUNDING_API_URL = os.environ.get(
    "ZENRIPPLE_GROUNDING_API_URL", "https://openrouter.ai/api/v1/chat/completions"
)
# Coordinate mode: "norm1000" for Qwen3-VL/Qwen3.5 (0-1000 normalized grid),
# "absolute" for Qwen2.5-VL/UI-TARS (raw pixel coords).
_GROUNDING_COORD_MODE = os.environ.get("ZENRIPPLE_GROUNDING_COORD_MODE", "norm1000")
_GROUNDING_KEY_SYNCED = False  # Whether we've synced with browser prefs yet

mcp = FastMCP(
    "zenripple-browser",
    instructions=(
        "Full browser control for Zen Browser — navigate pages, click elements, fill forms, "
        "take screenshots, read content, execute JavaScript, and more. "
        "All tab operations are scoped to the 'ZenRipple' workspace."
    ),
)

_ws_connection = None
_ws_lock = asyncio.Lock()


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
_ws_command_lock = asyncio.Lock()
_session_id: str | None = None  # Populated from X-ZenRipple-Session after connect

# Cache screenshot ↔ viewport dimensions for auto-scaling click coordinates.
# Keyed by tab_id (empty string for default tab).
_last_screenshot_dims: dict[str, dict] = {}

# ── Session Replay State (Tool Call Logging) ──────────────────────
_replay_state_loaded: bool = False  # Have we loaded from disk this process?
_replay_active: bool = False
_replay_dir: str | None = None

# Opt-out: set ZENRIPPLE_NO_REPLAY=1 to disable automatic tool call logging
REPLAY_DISABLED = os.environ.get("ZENRIPPLE_NO_REPLAY", "").strip().lower() not in ("", "0", "false", "no")

# Max replay sessions to keep on disk (oldest pruned at startup)
REPLAY_KEEP = int(os.environ.get("ZENRIPPLE_REPLAY_KEEP", "50"))

_replay_pruned: bool = False  # Have we run pruning this process?


def _sanitize_session_id(raw: str) -> str:
    """Strip unsafe characters from session ID to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)


async def get_ws():
    """Get or create WebSocket connection to browser.

    Reconnection strategy:
    1. If ZENRIPPLE_SESSION_ID env var is set, always join that session
    2. If we previously connected and have a saved _session_id, rejoin it
    3. If a session file exists for this terminal (~/.zenripple/sessions/), rejoin that session
    4. Otherwise create a new session via /new
    This prevents tab loss when the WebSocket connection drops mid-operation.
    """
    global _ws_connection, _session_id, _replay_state_loaded
    async with _ws_lock:
        if _ws_connection is not None:
            try:
                await _ws_connection.ping()
                return _ws_connection
            except Exception:
                old_ws = _ws_connection
                _ws_connection = None
                try:
                    await old_ws.close()
                except Exception:
                    pass
                # Keep _session_id for reconnection — don't clear it

        # Route: env var > in-memory > session file > new
        reconnect_id = SESSION_ID or _session_id or _read_session_file()
        if reconnect_id:
            url = f"{BROWSER_WS_URL}/session/{reconnect_id}"
        else:
            url = f"{BROWSER_WS_URL}/new"

        token = _read_auth_token()
        _auth_headers = {"Authorization": f"Bearer {token}"} if token else {}
        _connect_kwargs = dict(
            max_size=10 * 1024 * 1024,  # 10MB — screenshots can exceed 1MB
            ping_interval=30,  # Send keepalive every 30s
            ping_timeout=120,  # Wait up to 120s for pong (browser may be busy)
            additional_headers=_auth_headers,
        )

        try:
            _ws_connection = await websockets.connect(url, **_connect_kwargs)
        except Exception as first_err:
            if reconnect_id and not SESSION_ID:
                # Session was destroyed (grace timer expired) — create a new one
                _session_id = None
                url = f"{BROWSER_WS_URL}/new"
                try:
                    _ws_connection = await websockets.connect(url, **_connect_kwargs)
                except OSError:
                    raise ConnectionError(
                        f"Could not connect to Zen Browser on {BROWSER_WS_URL}. "
                        "Make sure Zen Browser is running with ZenRipple installed. "
                        "If you just installed, restart Zen Browser first."
                    ) from first_err
            elif isinstance(first_err, OSError):
                raise ConnectionError(
                    f"Could not connect to Zen Browser on {BROWSER_WS_URL}. "
                    "Make sure Zen Browser is running with ZenRipple installed. "
                    "If you just installed, restart Zen Browser first."
                ) from first_err
            else:
                raise

        # Extract session ID from response headers
        # websockets v16+: ws.response.headers; older: ws.response_headers
        headers = None
        if hasattr(_ws_connection, "response") and _ws_connection.response:
            headers = _ws_connection.response.headers
        elif hasattr(_ws_connection, "response_headers"):
            headers = _ws_connection.response_headers
        if headers:
            prev_session = _session_id
            _session_id = headers.get("X-ZenRipple-Session")
            # Persist for other processes (skip when explicit env var is set)
            if _session_id and not SESSION_ID:
                _write_session_file(_session_id)
            # If auto-session just populated _session_id for the first time,
            # allow replay to re-check (it may have skipped due to no session).
            if _session_id and not prev_session and not SESSION_ID:
                _replay_state_loaded = False

        return _ws_connection


async def browser_command(method: str, params: dict | None = None) -> dict:
    """Send a command to the browser and return the response.

    Retries once on connection-level failure (reconnects to same session).
    Browser-level errors (e.g. "Tab not found") are never retried.
    """
    global _ws_connection
    async with _ws_command_lock:
        for attempt in range(2):
            try:
                ws = await get_ws()
                msg_id = str(uuid4())
                msg = {"id": msg_id, "method": method, "params": params or {}}
                await ws.send(json.dumps(msg))
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                resp = json.loads(raw)
            except Exception:
                # Connection-level error (send/recv failed, timeout, malformed data)
                if attempt == 0:
                    old_ws = _ws_connection
                    _ws_connection = None
                    if old_ws is not None:
                        try:
                            await old_ws.close()
                        except Exception:
                            pass
                    continue  # retry with reconnection
                raise
            # Validate response ID matches what we sent
            resp_id = resp.get("id")
            if resp_id is not None and resp_id != msg_id:
                raise Exception(
                    f"Response ID mismatch: expected {msg_id}, got {resp_id}"
                )
            # Extract piggybacked notifications (dialog/popup events) from any response
            notifications = resp.get("_notifications")
            if notifications:
                _pending_notifications.extend(notifications)
                # Cap to prevent unbounded growth if drain isn't called
                if len(_pending_notifications) > 200:
                    _pending_notifications[:] = _pending_notifications[-200:]
            if "error" in resp:
                raise Exception(resp["error"].get("message", "Unknown browser error"))
            return resp.get("result", {})
    raise RuntimeError("browser_command: unreachable")


# ── Proactive Notifications ───────────────────────────────────

_pending_notifications: list[dict] = []


def _drain_notifications() -> str:
    """Drain accumulated notifications and format as human-readable text."""
    global _pending_notifications
    if not _pending_notifications:
        return ""
    notifs = _pending_notifications
    _pending_notifications = []
    parts = []
    for n in notifs:
        if n["type"] == "dialog_opened":
            parts.append(
                f'\n\n--- NOTIFICATION: A {n.get("dialog_type", "unknown")} dialog appeared: '
                f'"{n.get("message", "")}" ---\n'
                f'Use browser_handle_dialog(action="accept") or '
                f'browser_handle_dialog(action="dismiss") to handle it.'
            )
        elif n["type"] == "popup_blocked":
            urls = n.get("popup_urls") or []
            url_str = ", ".join(urls)
            count = n.get("blocked_count", 1)
            parts.append(
                f"\n\n--- NOTIFICATION: The browser blocked {count} popup(s)"
                f'{" (" + url_str + ")" if url_str else ""} ---\n'
                f"Use browser_allow_blocked_popup() to open them, or ignore."
            )
        else:
            parts.append(
                f'\n\n--- NOTIFICATION ({n.get("type", "unknown")}): '
                f'{json.dumps(n, default=str)} ---'
            )
    return "".join(parts)


def _append_notifications(result_text: str) -> str:
    """Append any pending notifications to a tool result string."""
    return result_text + _drain_notifications()


def text_result(data) -> str:
    """Format result as string for MCP tool return."""
    if isinstance(data, (dict, list)):
        return json.dumps(data, indent=2)
    return str(data)


# ── Tool Call Logging (Session Replay) ──────────────────────────
#
# Every tool call is logged to $TMPDIR/zenripple_replay_{session_id}/tool_log.jsonl
# with a screenshot captured after each call. This provides a complete observability
# trail for each session: what was called, with what args, what it returned, and
# what the page looked like.

# Navigation tools need a brief delay before screenshot so the page has loaded.
_NAVIGATION_TOOLS = frozenset({
    "browser_create_tab", "browser_navigate", "browser_go_back",
    "browser_go_forward", "browser_reload", "browser_batch_navigate",
})

# Tools that destroy state — screenshot must be captured BEFORE the tool runs.
_PRE_SCREENSHOT_TOOLS = frozenset({
    "browser_close_tab", "browser_delete_cookies", "browser_delete_storage",
    "browser_session_close",
})


def _prune_old_replays(current_dir: str | None) -> None:
    """Delete oldest replay directories when count exceeds REPLAY_KEEP.

    Reads each directory's manifest.json for started_at to sort by age.
    Skips the current session directory. Runs once per process.
    """
    global _replay_pruned
    if _replay_pruned or REPLAY_KEEP <= 0:
        return
    _replay_pruned = True

    tmp = tempfile.gettempdir()
    prefix = "zenripple_replay_"
    try:
        candidates = [
            os.path.join(tmp, d)
            for d in os.listdir(tmp)
            if d.startswith(prefix) and os.path.isdir(os.path.join(tmp, d))
        ]
    except OSError:
        return

    if len(candidates) <= REPLAY_KEEP:
        return

    # Read started_at from each manifest; fall back to dir mtime
    dated: list[tuple[str, str]] = []
    for d in candidates:
        manifest = os.path.join(d, "manifest.json")
        ts = ""
        try:
            with open(manifest, "r") as f:
                ts = json.load(f).get("started_at", "")
        except Exception:
            pass
        if not ts:
            try:
                ts = datetime.fromtimestamp(
                    os.path.getmtime(d), tz=timezone.utc
                ).isoformat()
            except OSError:
                ts = ""
        dated.append((ts, d))

    # Sort oldest first
    dated.sort(key=lambda x: x[0])

    # Remove oldest, keeping REPLAY_KEEP. Never remove current session dir.
    # Exclude current dir from candidates so REPLAY_KEEP is honored exactly.
    norm_current = os.path.normpath(current_dir) if current_dir else None
    removable = [(ts, d) for ts, d in dated if not norm_current or os.path.normpath(d) != norm_current]
    to_remove = len(removable) - (REPLAY_KEEP - (1 if norm_current else 0))
    removed = 0
    for ts, d in removable:
        if removed >= to_remove:
            break
        try:
            shutil.rmtree(d)
            removed += 1
        except Exception as exc:
            print(f"[zenripple] prune error for {d}: {exc}", file=sys.stderr)

    if removed:
        print(f"[zenripple] pruned {removed} old replay dir(s)", file=sys.stderr)


def _load_replay_state() -> bool:
    """Initialize replay state from disk. Returns True if logging is active.

    Called once per process (fast-path after first call). Auto-initializes when
    a session ID is available, so logging works across MCPorter process spawns.
    """
    global _replay_state_loaded, _replay_active, _replay_dir

    if _replay_state_loaded:
        return _replay_active

    _replay_state_loaded = True

    effective_session = SESSION_ID or _session_id
    if REPLAY_DISABLED or not effective_session:
        _replay_active = False
        return False

    safe_id = _sanitize_session_id(effective_session)
    if not safe_id:
        _replay_active = False
        return False

    _replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    os.makedirs(_replay_dir, exist_ok=True)

    # Initialize manifest if it doesn't exist or is corrupt — under lock to
    # prevent two concurrent processes from both writing next_seq: 0.
    manifest_path = os.path.join(_replay_dir, "manifest.json")
    lock_path = os.path.join(_replay_dir, "replay.lock")

    def _ensure_manifest():
        needs_fresh = not os.path.exists(manifest_path)
        if not needs_fresh:
            try:
                with open(manifest_path, "r") as f:
                    json.load(f)
            except (json.JSONDecodeError, OSError):
                needs_fresh = True
        if needs_fresh:
            manifest = {
                "session_id": safe_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "next_seq": 0,
            }
            tmp = manifest_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(manifest, f)
            os.replace(tmp, manifest_path)

    try:
        if fcntl:
            with open(lock_path, "a") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    _ensure_manifest()
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        else:
            _ensure_manifest()
    except Exception as exc:
        print(f"[zenripple] manifest init error: {exc}", file=sys.stderr)

    _replay_active = True
    _prune_old_replays(_replay_dir)
    return True


def _claim_next_seq() -> int:
    """Atomically claim the next sequence number from the manifest. Returns -1 on error."""
    if not _replay_dir:
        return -1
    manifest_path = os.path.join(_replay_dir, "manifest.json")
    lock_path = os.path.join(_replay_dir, "replay.lock")

    def _do_claim():
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            manifest = {"next_seq": 0}

        seq = manifest.get("next_seq", 0)
        manifest["next_seq"] = seq + 1

        tmp = manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(manifest, f)
        os.replace(tmp, manifest_path)
        return seq

    try:
        os.makedirs(_replay_dir, exist_ok=True)
        if fcntl:
            with open(lock_path, "a") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    return _do_claim()
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        else:
            return _do_claim()
    except Exception as exc:
        print(f"[zenripple] _claim_next_seq error: {exc}", file=sys.stderr)
        return -1


def _serialize_for_log(value) -> object:
    """Convert a tool result or args dict to a JSON-safe representation.
    Uses default=str in the final json.dumps, so only strips Image objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value  # json.dumps(default=str) handles non-serializable values
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, Image):
                parts.append("[Image data]")
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(str(item))
        return parts
    return str(value)


def _append_log_entry(entry: dict) -> None:
    """Append a single JSON line to tool_log.jsonl under file lock."""
    if not _replay_dir:
        return
    log_path = os.path.join(_replay_dir, "tool_log.jsonl")
    lock_path = os.path.join(_replay_dir, "replay.lock")
    line = json.dumps(entry, default=str) + "\n"

    try:
        if fcntl:
            with open(lock_path, "a") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    with open(log_path, "a") as f:
                        f.write(line)
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        else:
            with open(log_path, "a") as f:
                f.write(line)
    except Exception as exc:
        print(f"[zenripple] _append_log_entry error: {exc}", file=sys.stderr)


async def _save_replay_screenshot(tool_name: str, seq: int,
                                   target_tab: str = "") -> str | None:
    """Capture and save a screenshot for a replay log entry. Returns filename or None."""
    try:
        ss_result = await browser_command("screenshot", {"tab_id": target_tab or None})
        data_url = ss_result.get("image", "")
        if data_url:
            b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
            raw_bytes = base64.b64decode(b64)
            filename = f"{seq:05d}_{tool_name}.jpg"
            with open(os.path.join(_replay_dir, filename), "wb") as f:
                f.write(raw_bytes)
            return filename
    except Exception:
        pass  # Screenshot is best-effort
    return None


async def _log_tool_call(tool_name: str, call_args: dict, result, timestamp: str,
                         duration_ms: float, error: bool = False,
                         pre_seq: int = -1,
                         pre_screenshot: str | None = None) -> None:
    """Log a tool call with screenshot to the session replay directory.

    If pre_seq >= 0, uses that seq number (already claimed) instead of claiming a new one.
    If pre_screenshot is set, skips screenshot capture and uses the provided filename.
    """
    if not _load_replay_state():
        return

    seq = pre_seq if pre_seq >= 0 else _claim_next_seq()
    if seq < 0:
        return

    # Use pre-captured screenshot if provided, otherwise capture now.
    screenshot_file = pre_screenshot
    if screenshot_file is None:
        target_tab = call_args.get("tab_id", "") if isinstance(call_args, dict) else ""
        if tool_name in _NAVIGATION_TOOLS:
            try:
                await asyncio.sleep(0.5)
                await browser_command("wait_for_load", {"tab_id": target_tab or None, "timeout": 5})
            except Exception:
                pass
        screenshot_file = await _save_replay_screenshot(tool_name, seq, target_tab)

    _append_log_entry({
        "seq": seq,
        "tool": tool_name,
        "args": _serialize_for_log(call_args),
        "result": _serialize_for_log(result),
        "timestamp": timestamp,
        "duration_ms": round(duration_ms, 1),
        "screenshot": screenshot_file,
        "error": error,
    })


def _with_call_logging(fn):
    """Decorator: wraps a tool function to log args, result, and screenshot.
    Logging is awaited (not fire-and-forget) to ensure ordered screenshots."""
    tool_name = fn.__name__
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        # Bind args to parameter names for structured logging
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)
        except Exception:
            call_args = kwargs if kwargs else {}

        # For destructive tools, capture screenshot BEFORE the tool runs.
        pre_seq = -1
        pre_screenshot = None
        if tool_name in _PRE_SCREENSHOT_TOOLS and _load_replay_state():
            pre_seq = _claim_next_seq()
            if pre_seq >= 0:
                target_tab = call_args.get("tab_id", "") if isinstance(call_args, dict) else ""
                pre_screenshot = await _save_replay_screenshot(tool_name, pre_seq, target_tab)

        ts = datetime.now(timezone.utc).isoformat()
        start = time.monotonic()

        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            duration_ms = (time.monotonic() - start) * 1000
            try:
                await _log_tool_call(tool_name, call_args, result, ts, duration_ms,
                                     pre_seq=pre_seq, pre_screenshot=pre_screenshot)
            except Exception:
                pass
            return result
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            try:
                await _log_tool_call(tool_name, call_args, str(exc), ts, duration_ms,
                                     error=True, pre_seq=pre_seq, pre_screenshot=pre_screenshot)
            except Exception:
                pass
            raise

    return wrapper


# Monkey-patch mcp.tool() to wrap every tool with call logging.
# This runs before any @mcp.tool() decorators below, ensuring universal coverage.
_original_mcp_tool = mcp.tool


def _logging_tool_wrapper(*args, **kwargs):
    original_decorator = _original_mcp_tool(*args, **kwargs)

    def combined(fn):
        return original_decorator(_with_call_logging(fn))

    return combined


mcp.tool = _logging_tool_wrapper


# ── Tab Management ──────────────────────────────────────────────


@mcp.tool()
async def browser_create_tab(url: str = "about:blank", persist: bool = True) -> str:
    """Create a new browser tab in the ZenRipple workspace and navigate to a URL.
    Set persist=true to keep the tab alive after session close (it will be
    released back to unclaimed instead of destroyed)."""
    result = text_result(await browser_command("create_tab", {"url": url, "persist": persist}))

    return _append_notifications(result)


@mcp.tool()
async def browser_close_tab(tab_id: str = "") -> str:
    """Close a browser tab. If no tab_id, closes the active tab."""

    result = text_result(
        await browser_command("close_tab", {"tab_id": tab_id or None})
    )
    # Clean up cached screenshot dimensions for the closed tab
    _last_screenshot_dims.pop(tab_id or "", None)
    return _append_notifications(result)


@mcp.tool()
async def browser_switch_tab(tab_id: str) -> str:
    """Switch to a different tab in the ZenRipple workspace."""
    result = text_result(await browser_command("switch_tab", {"tab_id": tab_id}))

    return _append_notifications(result)


@mcp.tool()
async def browser_list_tabs() -> str:
    """List all open tabs in the ZenRipple workspace with IDs, titles, and URLs."""
    return text_result(await browser_command("list_tabs"))


# ── Navigation ──────────────────────────────────────────────────


@mcp.tool()
async def browser_navigate(url: str, tab_id: str = "") -> str:
    """Navigate a tab to a URL. If no tab_id, navigates the active tab."""
    result = text_result(
        await browser_command("navigate", {"url": url, "tab_id": tab_id or None})
    )

    return _append_notifications(result)


@mcp.tool()
async def browser_go_back(tab_id: str = "") -> str:
    """Navigate back in a tab's history."""
    result = text_result(
        await browser_command("go_back", {"tab_id": tab_id or None})
    )

    return _append_notifications(result)


@mcp.tool()
async def browser_go_forward(tab_id: str = "") -> str:
    """Navigate forward in a tab's history."""
    result = text_result(
        await browser_command("go_forward", {"tab_id": tab_id or None})
    )

    return _append_notifications(result)


@mcp.tool()
async def browser_reload(tab_id: str = "") -> str:
    """Reload a tab."""
    result = text_result(
        await browser_command("reload", {"tab_id": tab_id or None})
    )

    return _append_notifications(result)


# ── Tab Events ──────────────────────────────────────────────────


@mcp.tool()
async def browser_get_tab_events() -> str:
    """Get and drain the queue of tab open/close events since the last call.
    Useful for detecting popups, new tabs opened by links (target=_blank), etc.
    Returns events with type (tab_opened/tab_closed), tab_id, opener_tab_id."""
    return text_result(await browser_command("get_tab_events"))


# ── Dialogs ─────────────────────────────────────────────────────


@mcp.tool()
async def browser_get_dialogs() -> str:
    """Get any pending alert/confirm/prompt dialogs that the browser is showing.
    Returns a list of dialog objects with type, message, and default_value."""
    return text_result(await browser_command("get_dialogs"))


@mcp.tool()
async def browser_handle_dialog(action: str, text: str = "") -> str:
    """Handle (accept or dismiss) the oldest pending dialog.
    action: 'accept' to click OK/Yes, 'dismiss' to click Cancel/No.
    text: optional text to enter for prompt dialogs before accepting."""
    params = {"action": action}
    if text:
        params["text"] = text
    result = text_result(await browser_command("handle_dialog", params))

    return _append_notifications(result)


# ── Popup Blocked ──────────────────────────────────────────────


@mcp.tool()
async def browser_get_popup_blocked_events() -> str:
    """Get and drain the queue of popup-blocked events since the last call.
    Returns events with type, tab_id, popup_url, and timestamp."""
    return text_result(await browser_command("get_popup_blocked_events"))


@mcp.tool()
async def browser_allow_blocked_popup(tab_id: str = "", index: int = -1) -> str:
    """Allow blocked popups for a tab, opening them as new tabs.
    Call after receiving a popup_blocked notification.
    Pass index to unblock a specific popup, or omit to unblock all."""
    params: dict = {"tab_id": tab_id or None}
    if index >= 0:
        params["index"] = index
    result = text_result(await browser_command("allow_blocked_popup", params))

    return _append_notifications(result)


# ── Navigation Status ───────────────────────────────────────────


@mcp.tool()
async def browser_get_navigation_status(tab_id: str = "") -> str:
    """Get the HTTP status and error code for the last navigation in a tab.
    Returns {url, http_status, error_code, loading}. Useful to detect 404s,
    server errors, or network failures after navigation."""
    return text_result(
        await browser_command(
            "get_navigation_status", {"tab_id": tab_id or None}
        )
    )


# ── Frames ──────────────────────────────────────────────────────


@mcp.tool()
async def browser_list_frames(tab_id: str = "") -> str:
    """List all frames (iframes) in a tab. Returns frame IDs that can be passed to
    other tools (get_dom, click, fill, etc.) to interact with content inside iframes."""
    return text_result(
        await browser_command("list_frames", {"tab_id": tab_id or None})
    )


# ── Observation ─────────────────────────────────────────────────


@mcp.tool()
async def browser_get_page_info(tab_id: str = "") -> str:
    """Get info about a tab: URL, title, loading state, navigation history."""
    return text_result(
        await browser_command("get_page_info", {"tab_id": tab_id or None})
    )


@mcp.tool()
async def browser_screenshot(tab_id: str = "") -> list:
    """Take a screenshot of a browser tab. Returns the image and viewport dimensions.
    Use this to verify page state, understand layouts, or see visual content.
    Coordinates from this screenshot are auto-scaled when passed to browser_click_coordinates."""
    result = await browser_command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if not data_url:
        raise Exception("Screenshot returned empty image data")
    # Strip data URL prefix: "data:image/jpeg;base64,..." or "data:image/png;base64,..."
    if data_url.startswith("data:") and "," in data_url:
        header, b64 = data_url.split(",", 1)
        fmt = "jpeg" if "jpeg" in header else "png"
    else:
        b64 = data_url
        fmt = "jpeg"
    raw_bytes = base64.b64decode(b64)

    # Cache screenshot ↔ viewport dimensions for auto-scaling
    sw = result.get("width")
    sh = result.get("height")
    vw = result.get("viewport_width", sw)
    vh = result.get("viewport_height", sh)
    tab_key = tab_id or ""
    if sw and sh:
        _last_screenshot_dims[tab_key] = {"sw": sw, "sh": sh, "vw": vw, "vh": vh}

    blocks: list = [Image(data=raw_bytes, format=fmt)]
    if sw and sh and vw and vh:
        info = f"Screenshot: {sw}x{sh}px | Viewport: {vw}x{vh}px"
        if sw != vw:
            info += f" | Scale factor: {vw / sw:.3f}"
        blocks.append(info)
    notif_text = _drain_notifications()
    if notif_text:
        blocks.append(notif_text)
    return blocks


@mcp.tool()
async def browser_get_dom(
    tab_id: str = "",
    frame_id: int = 0,
    viewport_only: bool = False,
    max_elements: int = 0,
    incremental: bool = False,
) -> str:
    """Get all interactive elements on the current page with indices.
    Returns elements like buttons, links, inputs, selects with their index numbers.
    Use these indices with click/fill tools in the future.
    viewport_only: only return elements visible in the current viewport.
    max_elements: limit the number of elements returned (0 = unlimited).
    incremental: return a diff against the previous get_dom call instead of full list."""
    params: dict = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    if viewport_only:
        params["viewport_only"] = True
    if max_elements:
        params["max_elements"] = max_elements
    if incremental:
        params["incremental"] = True
    result = await browser_command("get_dom", params)
    if isinstance(result, dict) and "elements" in result:
        lines = [
            f"Page: {result.get('url', '?')}",
            f"Title: {result.get('title', '?')}",
            f"Total: {result.get('total', len(result['elements']))} elements",
        ]
        if result.get("incremental") and "diff" in result:
            diff = result["diff"]
            lines.append(
                f"Changes: +{diff.get('added', 0)} -{diff.get('removed', 0)}"
            )
            if diff.get("added_elements"):
                lines.append("")
                lines.append("Added:")
                for el in diff["added_elements"]:
                    lines.append(f"  [{el.get('index', '?')}] <{el.get('tag', '?')}>{el.get('text', '')}")
            if diff.get("removed_elements"):
                lines.append("")
                lines.append("Removed:")
                for el in diff["removed_elements"]:
                    lines.append(f"  <{el.get('tag', '?')}>{el.get('text', '')}")
        lines.append("")
        lines.append("Interactive elements:")
        for el in result["elements"]:
            attrs = " ".join(
                f'{k}="{v}"' for k, v in (el.get("attributes") or {}).items()
            )
            text = el.get("text", "").strip()
            tag = el["tag"]
            rect = el.get("rect", {})
            pos = (
                f"({rect.get('x', 0)},{rect.get('y', 0)} "
                f"{rect.get('w', 0)}x{rect.get('h', 0)})"
            )
            lines.append(f"[{el['index']}] <{tag} {attrs}>{text}</{tag}> {pos}")
        return "\n".join(lines)
    return text_result(result)


@mcp.tool()
async def browser_get_page_text(tab_id: str = "", frame_id: int = 0) -> str:
    """Get the full visible text content of the current page or a specific iframe."""
    params = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    result = await browser_command("get_page_text", params)
    if isinstance(result, dict) and "text" in result:
        return result["text"]
    return text_result(result)


@mcp.tool()
async def browser_get_page_html(tab_id: str = "", frame_id: int = 0) -> str:
    """Get the full HTML source of the current page or a specific iframe."""
    params = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    result = await browser_command("get_page_html", params)
    if isinstance(result, dict) and "html" in result:
        return result["html"]
    return text_result(result)


# ── Compact DOM / Accessibility (Phase 8) ─────────────────────


@mcp.tool()
async def browser_get_elements_compact(
    tab_id: str = "",
    frame_id: int = 0,
    viewport_only: bool = False,
    max_elements: int = 0,
) -> str:
    """Get a compact, token-efficient representation of interactive elements.
    Returns one line per element: [index] text (tag →href/value).
    5-10x fewer tokens than browser_get_dom. Use this when you need element indices
    but don't need full attribute details or bounding boxes."""
    params: dict = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    if viewport_only:
        params["viewport_only"] = True
    if max_elements:
        params["max_elements"] = max_elements
    result = await browser_command("get_dom", params)
    if isinstance(result, dict) and "elements" in result:
        lines = [
            f"URL: {result.get('url', '?')} | Title: {result.get('title', '?')}",
        ]
        for el in result["elements"]:
            tag = el["tag"]
            text = el.get("text", "").strip()
            attrs = el.get("attributes") or {}
            # Build compact detail
            detail = ""
            if attrs.get("href"):
                detail = f" \u2192{attrs['href']}"
            elif attrs.get("value"):
                detail = f" ={attrs['value']}"
            elif attrs.get("type"):
                detail = f" type={attrs['type']}"
            role = f" role={el['role']}" if el.get("role") else ""
            lines.append(f"[{el['index']}] {text} ({tag}{role}{detail})")
        return "\n".join(lines)
    return text_result(result)


@mcp.tool()
async def browser_get_accessibility_tree(tab_id: str = "", frame_id: int = 0) -> str:
    """Get the accessibility tree for the current page.
    Returns semantic nodes with role, name, value, and depth.
    Useful for understanding page structure without visual rendering.
    Falls back gracefully if the accessibility service is unavailable."""
    params: dict = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    result = await browser_command("get_accessibility_tree", params)
    if isinstance(result, dict):
        if result.get("error"):
            return f"Accessibility tree error: {result['error']}"
        nodes = result.get("nodes", [])
        if not nodes:
            return "(no accessibility nodes found)"
        lines = [f"Accessibility tree ({result.get('total', len(nodes))} nodes):"]
        for node in nodes:
            indent = "  " * node.get("depth", 0)
            role = node.get("role", "?")
            name = node.get("name", "")
            value = node.get("value", "")
            entry = f"{indent}[{role}]"
            if name:
                entry += f" {name}"
            if value:
                entry += f" ={value}"
            lines.append(entry)
        return "\n".join(lines)
    return text_result(result)


# ── Interaction ────────────────────────────────────────────────


@mcp.tool()
async def browser_click(index: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Click an interactive element by its index from browser_get_dom.
    Always call browser_get_dom first to get element indices."""
    params = {"tab_id": tab_id or None, "index": index}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("click_element", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_click_coordinates(x: int, y: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Click at specific x,y coordinates on the page.
    Use browser_screenshot + browser_get_dom to identify coordinates.
    If screenshot and viewport dimensions differ, coordinates are auto-scaled
    from screenshot-space to viewport-space."""
    tab_key = tab_id or ""
    dims = _last_screenshot_dims.get(tab_key)

    # Scale from screenshot-space to viewport-space if they differ
    if dims and dims["sw"] and dims["vw"] and dims["sw"] != dims["vw"]:
        scale_x = dims["vw"] / dims["sw"]
        scale_y = dims["vh"] / dims["sh"]
        x = round(x * scale_x)
        y = round(y * scale_y)

    params = {"tab_id": tab_id or None, "x": x, "y": y}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("click_coordinates", params))

    return _append_notifications(result)


def _parse_grounding_coordinates(
    text: str, img_w: int, img_h: int, coord_mode: str = "absolute"
):
    """Parse pixel coordinates from a grounding VLM response.

    Handles formats from Qwen-VL, UI-TARS, and generic models:
      - Bounding box: (x1,y1,x2,y2) or [x1,y1,x2,y2] → center
      - Absolute: (x, y) or [x, y]
      - Normalized: (0.xx, 0.yy) → scaled to image dims
      - Qwen box tokens: <|box_start|>(x,y)<|box_end|>
      - Point tags: <point>x y</point>

    Args:
        coord_mode: "norm1000" for Qwen3-VL/Qwen3.5 (0-1000 normalized grid,
                    pixel = coord/1000 * img_dim), "absolute" for raw pixels.
    """
    def _denorm(x, y):
        """Apply 0-1000 denormalization if coord_mode is norm1000."""
        if coord_mode == "norm1000":
            return round(x * img_w / 1000), round(y * img_h / 1000)
        return x, y

    # Qwen box token format
    m = re.search(r"<\|box_start\|>\((\d+),\s*(\d+)\)<\|box_end\|>", text)
    if m:
        return _denorm(int(m.group(1)), int(m.group(2)))
    # <point>x y</point>
    m = re.search(r"<point>(\d+)\s+(\d+)</point>", text)
    if m:
        return _denorm(int(m.group(1)), int(m.group(2)))
    # Bounding box (x1, y1, x2, y2) → center
    m = re.search(
        r"[\(\[]\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*[\)\]]", text
    )
    if m:
        x1, y1, x2, y2 = (
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)), int(m.group(4)),
        )
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        return _denorm(cx, cy)
    # Normalized floats (0.xx, 0.yy) — already scale to image dims, no denorm needed
    m = re.search(r"[\(\[]\s*([01]\.\d+)\s*,\s*([01]\.\d+)\s*[\)\]]", text)
    if m:
        return round(float(m.group(1)) * img_w), round(float(m.group(2)) * img_h)
    # Absolute integers (x, y)
    m = re.search(r"[\(\[]\s*(\d+)\s*,\s*(\d+)\s*[\)\]]", text)
    if m:
        return _denorm(int(m.group(1)), int(m.group(2)))
    # Absolute decimals (x.x, y.y) — some VLMs return non-integer pixel coords
    m = re.search(r"[\(\[]\s*(\d+\.\d+)\s*,\s*(\d+\.\d+)\s*[\)\]]", text)
    if m:
        x, y = round(float(m.group(1))), round(float(m.group(2)))
        return _denorm(x, y)
    return None, None


async def _ensure_grounding_key() -> str:
    """Get the grounding API key, syncing between env var and Firefox prefs.

    Priority: env var > Firefox prefs.
    If env var is set, store it to Firefox prefs for future sessions.
    If env var is empty, try to load from Firefox prefs.
    Returns the key or empty string.
    """
    global _GROUNDING_API_KEY, _GROUNDING_KEY_SYNCED
    if _GROUNDING_API_KEY and not _GROUNDING_KEY_SYNCED:
        # We have an env var key — persist it to browser config
        try:
            await browser_command("set_config", {
                "key": "openrouter_api_key",
                "value": _GROUNDING_API_KEY,
            })
            _GROUNDING_KEY_SYNCED = True
        except Exception:
            pass  # Non-fatal: key still works from env var, retry sync next call
        return _GROUNDING_API_KEY

    if not _GROUNDING_API_KEY and not _GROUNDING_KEY_SYNCED:
        # No env var — try loading from browser config
        try:
            result = await browser_command("get_config", {
                "key": "openrouter_api_key",
            })
            stored_key = result.get("value", "")
            if stored_key:
                _GROUNDING_API_KEY = stored_key
            _GROUNDING_KEY_SYNCED = True
        except Exception:
            pass  # Browser not connected yet — retry sync next call

    return _GROUNDING_API_KEY


@mcp.tool()
async def browser_grounded_click(
    description: str, tab_id: str = "",
    frame_id: int = 0,  # Kept for backwards compat; ignored — click_native auto-routes into iframes
) -> str:
    """Click on a page element described in natural language using VLM grounding.

    Takes a screenshot, sends it to a grounding VLM (Qwen3-VL-235B-A22B by default)
    which returns precise pixel coordinates, then clicks at that position.
    Much more accurate than manual coordinate estimation for dense UIs.

    The API key is read from OPENROUTER_API_KEY env var on first use, then
    persisted in the browser so it doesn't need to be provided again.

    Args:
        description: Natural language description of what to click
                     (e.g. "the Submit button", "the search input field")
        tab_id: Optional tab ID (defaults to active tab)
        frame_id: Optional frame ID for iframe content
    """
    api_key = await _ensure_grounding_key()
    if not api_key:
        return "Error: OPENROUTER_API_KEY not set (provide via env var or it will be remembered from a previous session)"

    # Step 1: Take a screenshot
    result = await browser_command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if not data_url:
        return "Error: screenshot returned empty image"

    if data_url.startswith("data:") and "," in data_url:
        header, b64 = data_url.split(",", 1)
        media_type = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
    else:
        b64 = data_url
        media_type = "image/png"

    sw = result.get("width", 0)
    sh = result.get("height", 0)
    vw = result.get("viewport_width", sw)
    vh = result.get("viewport_height", sh)

    # Cache dimensions for consistency
    tab_key = tab_id or ""
    if sw and sh:
        _last_screenshot_dims[tab_key] = {"sw": sw, "sh": sh, "vw": vw, "vh": vh}

    # Step 2: Ask the grounding VLM for coordinates
    prompt = (
        f"This is a {sw}x{sh} pixel screenshot. Find the exact pixel coordinates "
        f"of {description}. Return ONLY the center point coordinates as (x, y). "
        f"Nothing else."
    )

    payload = {
        "model": _GROUNDING_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{b64}"
            }},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 100,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Retry with exponential backoff for transient errors (transport failures,
    # server errors, rate limits). Do NOT retry 4xx client errors (except 429).
    max_retries = 3
    last_error = None
    vlm_text = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(
                    _GROUNDING_API_URL,
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                try:
                    vlm_text = resp.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    return f"Error: unexpected VLM response format: {e}"
                break
            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code
                # Only retry 5xx server errors and 429 rate limits
                if status >= 500 or status == 429:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0 * (2**attempt))
                    continue
                # 4xx client errors (auth, bad request, etc.) — fail immediately
                return f"Error: VLM API returned {status}: {e.response.text[:200]}"
            except httpx.TransportError as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (2**attempt))
                continue

    if vlm_text is None:
        return f"Error: VLM request failed after {max_retries} attempts: {last_error}"

    # Step 3: Parse coordinates and validate bounds
    px, py = _parse_grounding_coordinates(vlm_text, sw, sh, _GROUNDING_COORD_MODE)
    if px is None or py is None:
        return f"Error: could not parse coordinates from VLM response: {vlm_text[:200]}"
    # Clamp to screenshot bounds — VLM may return slightly out-of-range values
    px = max(0, min(px, sw - 1)) if sw else px
    py = max(0, min(py, sh - 1)) if sh else py

    # Step 4: Scale from screenshot-space to viewport-space if needed
    click_x, click_y = px, py
    if sw and vw and sw != vw:
        click_x = round(px * vw / sw)
        click_y = round(py * vh / sh)

    # Step 5: Click using chrome-level native mouse events — routes through
    # iframes exactly like a real user click. No frame_id needed.
    click_result = text_result(await browser_command("click_native", {
        "tab_id": tab_id or None, "x": click_x, "y": click_y,
    }))



    return _append_notifications(
        f"Grounded click: \"{description}\" → VLM predicted ({px},{py}), "
        f"clicked ({click_x},{click_y}). {click_result}"
    )


@mcp.tool()
async def browser_fill(index: int, value: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Fill a form field (input/textarea) with a value by its index from browser_get_dom.
    Clears existing content and sets the new value, dispatching input/change events."""
    params = {"tab_id": tab_id or None, "index": index, "value": value}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("fill_field", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_select_option(index: int, value: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Select an option in a <select> dropdown by its index from browser_get_dom.
    The value can be the option's value attribute or visible text."""
    params = {"tab_id": tab_id or None, "index": index, "value": value}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("select_option", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_type(text: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Type text character-by-character into the currently focused element.
    Dispatches keydown/keypress/keyup and input events for each character.
    Focus an element first with browser_click."""
    params = {"tab_id": tab_id or None, "text": text}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("type_text", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_press_key(
    key: str, ctrl: bool = False, shift: bool = False, alt: bool = False, meta: bool = False, tab_id: str = "", frame_id: int = 0
) -> str:
    """Press a keyboard key (Enter, Tab, Escape, ArrowDown, a, etc.) with optional modifiers.
    Dispatches keydown/keypress/keyup events on the focused element."""
    modifiers = {"ctrl": ctrl, "shift": shift, "alt": alt, "meta": meta}
    params = {"tab_id": tab_id or None, "key": key, "modifiers": modifiers}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("press_key", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_scroll(
    direction: str = "down", amount: int = 500, tab_id: str = "", frame_id: int = 0
) -> str:
    """Scroll the page in a direction (up/down/left/right) by a pixel amount.
    Default is 500 pixels down."""
    params = {"tab_id": tab_id or None, "direction": direction, "amount": amount}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("scroll", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_hover(index: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Hover over an interactive element by its index from browser_get_dom.
    Dispatches mouseenter/mouseover/mousemove events. Useful for revealing tooltips or dropdown menus."""
    params = {"tab_id": tab_id or None, "index": index}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("hover", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_hover_coordinates(x: int, y: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Hover at specific x,y coordinates on the page, dispatching native mouse events.
    Triggers mouseenter/mouseover/mousemove at the position — useful for revealing
    tooltips, dropdown menus, hover-dependent UI, and positioning for scroll_at_point.
    Shows a visual cursor overlay (lime green) for 12 seconds.
    If screenshot and viewport dimensions differ, coordinates are auto-scaled."""
    tab_key = tab_id or ""
    dims = _last_screenshot_dims.get(tab_key)
    if dims and dims["sw"] and dims["vw"] and dims["sw"] != dims["vw"]:
        scale_x = dims["vw"] / dims["sw"]
        scale_y = dims["vh"] / dims["sh"]
        x = round(x * scale_x)
        y = round(y * scale_y)
    params = {"tab_id": tab_id or None, "x": x, "y": y}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("hover_coordinates", params))
    await _capture_replay_frame("hover_coordinates")
    return _append_notifications(result)


@mcp.tool()
async def browser_grounded_hover(
    description: str, tab_id: str = "",
    frame_id: int = 0,  # Kept for backwards compat; ignored — hover_coordinates auto-routes into iframes
) -> str:
    """Hover on a page element described in natural language using VLM grounding.

    Takes a screenshot, sends it to a grounding VLM (Qwen3-VL-235B-A22B by default)
    which returns precise pixel coordinates, then hovers at that position.
    Dispatches native mouse events to trigger tooltips, dropdowns, and hover-dependent UI.

    Args:
        description: Natural language description of what to hover over
                     (e.g. "the user avatar", "the Settings menu item")
        tab_id: Optional tab ID (defaults to active tab)
        frame_id: Optional frame ID for iframe content
    """
    api_key = await _ensure_grounding_key()
    if not api_key:
        return "Error: OPENROUTER_API_KEY not set (provide via env var or it will be remembered from a previous session)"

    # Step 1: Take a screenshot
    result = await browser_command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if not data_url:
        return "Error: screenshot returned empty image"

    if data_url.startswith("data:") and "," in data_url:
        header, b64 = data_url.split(",", 1)
        media_type = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
    else:
        b64 = data_url
        media_type = "image/png"

    sw = result.get("width", 0)
    sh = result.get("height", 0)
    vw = result.get("viewport_width", sw)
    vh = result.get("viewport_height", sh)

    tab_key = tab_id or ""
    if sw and sh:
        _last_screenshot_dims[tab_key] = {"sw": sw, "sh": sh, "vw": vw, "vh": vh}

    # Step 2: Ask the grounding VLM for coordinates
    prompt = (
        f"This is a {sw}x{sh} pixel screenshot. Find the exact pixel coordinates "
        f"of {description}. Return ONLY the center point coordinates as (x, y). "
        f"Nothing else."
    )
    payload = {
        "model": _GROUNDING_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{b64}"
            }},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 100,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    max_retries = 3
    last_error = None
    vlm_text = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(
                    _GROUNDING_API_URL, headers=headers, json=payload,
                )
                resp.raise_for_status()
                try:
                    vlm_text = resp.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    return f"Error: unexpected VLM response format: {e}"
                break
            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code
                if status >= 500 or status == 429:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0 * (2**attempt))
                    continue
                return f"Error: VLM API returned {status}: {e.response.text[:200]}"
            except httpx.TransportError as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (2**attempt))
                continue

    if vlm_text is None:
        return f"Error: VLM request failed after {max_retries} attempts: {last_error}"

    # Step 3: Parse coordinates and validate bounds
    px, py = _parse_grounding_coordinates(vlm_text, sw, sh, _GROUNDING_COORD_MODE)
    if px is None or py is None:
        return f"Error: could not parse coordinates from VLM response: {vlm_text[:200]}"
    px = max(0, min(px, sw - 1)) if sw else px
    py = max(0, min(py, sh - 1)) if sh else py

    # Step 4: Scale from screenshot-space to viewport-space if needed
    hover_x, hover_y = px, py
    if sw and vw and sw != vw:
        hover_x = round(px * vw / sw)
        hover_y = round(py * vh / sh)

    # Step 5: Hover using the hover_coordinates command
    hover_result = text_result(await browser_command("hover_coordinates", {
        "tab_id": tab_id or None, "x": hover_x, "y": hover_y,
    }))

    await _capture_replay_frame("grounded_hover")

    return _append_notifications(
        f"Grounded hover: \"{description}\" -> VLM predicted ({px},{py}), "
        f"hovered ({hover_x},{hover_y}). {hover_result}"
    )


@mcp.tool()
async def browser_scroll_at_point(
    x: int, y: int, direction: str = "down", amount: int = 500,
    tab_id: str = "", frame_id: int = 0
) -> str:
    """Scroll at specific x,y coordinates using native wheel events.
    Unlike browser_scroll which scrolls the whole page, this scrolls whatever
    scrollable container is under the given coordinates — dropdowns, sub-menus,
    iframes, overflow containers, etc.
    If screenshot and viewport dimensions differ, coordinates are auto-scaled.

    Args:
        x: X coordinate (in viewport or screenshot space)
        y: Y coordinate (in viewport or screenshot space)
        direction: Scroll direction (up/down/left/right)
        amount: Scroll amount in pixels (default 500)
        tab_id: Optional tab ID
        frame_id: Optional frame ID for iframe content
    """
    tab_key = tab_id or ""
    dims = _last_screenshot_dims.get(tab_key)
    if dims and dims["sw"] and dims["vw"] and dims["sw"] != dims["vw"]:
        scale_x = dims["vw"] / dims["sw"]
        scale_y = dims["vh"] / dims["sh"]
        x = round(x * scale_x)
        y = round(y * scale_y)
    params = {"tab_id": tab_id or None, "x": x, "y": y, "direction": direction, "amount": amount}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("scroll_at_point", params))
    await _capture_replay_frame("scroll_at_point")
    return _append_notifications(result)


@mcp.tool()
async def browser_grounded_scroll(
    description: str, direction: str = "down", amount: int = 500,
    tab_id: str = "",
    frame_id: int = 0,  # Kept for backwards compat; ignored — scroll_at_point auto-routes into iframes
) -> str:
    """Scroll at a page element described in natural language using VLM grounding.

    Takes a screenshot, sends it to a grounding VLM to find the described element,
    then dispatches native wheel events at that position. Scrolls whatever container
    is under the element — dropdowns, sub-menus, iframes, overflow areas, etc.

    Args:
        description: Natural language description of where to scroll
                     (e.g. "the dropdown menu", "the scrollable sidebar", "the chat messages area")
        direction: Scroll direction (up/down/left/right)
        amount: Scroll amount in pixels (default 500)
        tab_id: Optional tab ID (defaults to active tab)
        frame_id: Optional frame ID for iframe content
    """
    api_key = await _ensure_grounding_key()
    if not api_key:
        return "Error: OPENROUTER_API_KEY not set (provide via env var or it will be remembered from a previous session)"

    # Step 1: Take a screenshot
    result = await browser_command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if not data_url:
        return "Error: screenshot returned empty image"

    if data_url.startswith("data:") and "," in data_url:
        header, b64 = data_url.split(",", 1)
        media_type = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
    else:
        b64 = data_url
        media_type = "image/png"

    sw = result.get("width", 0)
    sh = result.get("height", 0)
    vw = result.get("viewport_width", sw)
    vh = result.get("viewport_height", sh)

    tab_key = tab_id or ""
    if sw and sh:
        _last_screenshot_dims[tab_key] = {"sw": sw, "sh": sh, "vw": vw, "vh": vh}

    # Step 2: Ask the grounding VLM for coordinates
    prompt = (
        f"This is a {sw}x{sh} pixel screenshot. Find the exact pixel coordinates "
        f"of {description}. Return ONLY the center point coordinates as (x, y). "
        f"Nothing else."
    )
    payload = {
        "model": _GROUNDING_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{b64}"
            }},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 100,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    max_retries = 3
    last_error = None
    vlm_text = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(
                    _GROUNDING_API_URL, headers=headers, json=payload,
                )
                resp.raise_for_status()
                try:
                    vlm_text = resp.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    return f"Error: unexpected VLM response format: {e}"
                break
            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code
                if status >= 500 or status == 429:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0 * (2**attempt))
                    continue
                return f"Error: VLM API returned {status}: {e.response.text[:200]}"
            except httpx.TransportError as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (2**attempt))
                continue

    if vlm_text is None:
        return f"Error: VLM request failed after {max_retries} attempts: {last_error}"

    # Step 3: Parse coordinates and validate bounds
    px, py = _parse_grounding_coordinates(vlm_text, sw, sh, _GROUNDING_COORD_MODE)
    if px is None or py is None:
        return f"Error: could not parse coordinates from VLM response: {vlm_text[:200]}"
    px = max(0, min(px, sw - 1)) if sw else px
    py = max(0, min(py, sh - 1)) if sh else py

    # Step 4: Scale from screenshot-space to viewport-space if needed
    scroll_x, scroll_y = px, py
    if sw and vw and sw != vw:
        scroll_x = round(px * vw / sw)
        scroll_y = round(py * vh / sh)

    # Step 5: Scroll using the scroll_at_point command
    scroll_result = text_result(await browser_command("scroll_at_point", {
        "tab_id": tab_id or None, "x": scroll_x, "y": scroll_y,
        "direction": direction, "amount": amount,
    }))

    await _capture_replay_frame("grounded_scroll")

    return _append_notifications(
        f"Grounded scroll: \"{description}\" -> VLM predicted ({px},{py}), "
        f"scrolled {direction} {amount}px at ({scroll_x},{scroll_y}). {scroll_result}"
    )


# ── Console / Eval ─────────────────────────────────────────────


@mcp.tool()
async def browser_console_setup(tab_id: str = "", frame_id: int = 0) -> str:
    """Start capturing console output (log/warn/error/info) and uncaught errors on a tab.
    Must be called before browser_console_logs or browser_console_errors will return data.
    Capture persists until the page navigates away."""
    params = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("console_setup", params))


@mcp.tool()
async def browser_console_teardown(tab_id: str = "", frame_id: int = 0) -> str:
    """Stop console capture and remove installed listeners/wrappers for a tab/frame."""
    params = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("console_teardown", params))


@mcp.tool()
async def browser_console_logs(tab_id: str = "", frame_id: int = 0) -> str:
    """Get captured console messages (log/warn/info/error) from the current page.
    Call browser_console_setup first to start capturing. Returns up to 500 most recent entries."""
    params = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    result = await browser_command("console_get_logs", params)
    if isinstance(result, dict) and "logs" in result:
        if not result["logs"]:
            return "(no console logs captured)"
        lines = []
        for log in result["logs"]:
            ts = log.get("timestamp", "")
            level = log.get("level", "log")
            msg = log.get("message", "")
            lines.append(f"[{level}] {ts} {msg}")
        return "\n".join(lines)
    return text_result(result)


@mcp.tool()
async def browser_console_errors(tab_id: str = "", frame_id: int = 0) -> str:
    """Get captured errors: console.error calls, uncaught exceptions, and unhandled promise rejections.
    Call browser_console_setup first to start capturing. Returns up to 100 most recent entries."""
    params = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    result = await browser_command("console_get_errors", params)
    if isinstance(result, dict) and "errors" in result:
        if not result["errors"]:
            return "(no errors captured)"
        lines = []
        for err in result["errors"]:
            ts = err.get("timestamp", "")
            etype = err.get("type", "error")
            msg = err.get("message", "")
            stack = err.get("stack", "")
            entry = f"[{etype}] {ts} {msg}"
            if stack:
                entry += "\n" + stack
            lines.append(entry)
        return "\n\n".join(lines)
    return text_result(result)


@mcp.tool()
async def browser_console_eval(expression: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Execute JavaScript in the current page and return the result.
    Runs in the page's global scope — can access page variables, DOM, etc.
    May be blocked by Content Security Policy on some pages."""
    params = {"tab_id": tab_id or None, "expression": expression}
    if frame_id:
        params["frame_id"] = frame_id
    result = await browser_command("console_evaluate", params)
    if isinstance(result, dict):
        if "error" in result:
            stack = result.get("stack", "")
            return _append_notifications(
                f"Error: {result['error']}" + (f"\n{stack}" if stack else "")
            )
        if "result" in result:
            return _append_notifications(str(result["result"]))
    return _append_notifications(text_result(result))


# ── Clipboard ───────────────────────────────────────────────────


@mcp.tool()
async def browser_clipboard_read() -> str:
    """Read the current text content from the system clipboard."""
    result = await browser_command("clipboard_read")
    return result.get("text", "")


@mcp.tool()
async def browser_clipboard_write(text: str) -> str:
    """Write text to the system clipboard. Can then be pasted into any element
    using browser_press_key with meta+v (macOS) or ctrl+v."""
    return text_result(await browser_command("clipboard_write", {"text": text}))


# ── Control ─────────────────────────────────────────────────────


@mcp.tool()
async def browser_wait(seconds: float = 2.0) -> str:
    """Wait for a specified number of seconds. Useful after navigation or clicks
    to let the page load or animations complete."""
    return text_result(await browser_command("wait", {"seconds": seconds}))


@mcp.tool()
async def browser_wait_for_element(
    selector: str, tab_id: str = "", frame_id: int = 0, timeout: int = 10
) -> str:
    """Wait for a CSS selector to match an element on the page.
    Polls every 250ms until the element appears or timeout (seconds) is reached.
    Returns the element's tag and text if found, or {found: false, timeout: true}."""
    params = {"tab_id": tab_id or None, "selector": selector, "timeout": timeout}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("wait_for_element", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_wait_for_text(
    text: str, tab_id: str = "", frame_id: int = 0, timeout: int = 10
) -> str:
    """Wait for specific text to appear on the page.
    Polls every 250ms until the text is found or timeout (seconds) is reached.
    Returns {found: true} or {found: false, timeout: true}."""
    params = {"tab_id": tab_id or None, "text": text, "timeout": timeout}
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("wait_for_text", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_wait_for_load(tab_id: str = "", timeout: int = 15) -> str:
    """Wait for the current page to finish loading (up to timeout seconds).
    More reliable than browser_wait for navigation — polls the browser's loading state.
    Returns the final URL and title once loaded."""
    result = text_result(
        await browser_command(
            "wait_for_load",
            {"tab_id": tab_id or None, "timeout": timeout},
        )
    )

    return _append_notifications(result)


@mcp.tool()
async def browser_save_screenshot(file_path: str, tab_id: str = "") -> str:
    """Take a screenshot and save it as an image file to the given path.
    Use this to save visual evidence of page state to disk.
    The file_path can be absolute or relative to the server's working directory."""
    result = await browser_command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if not data_url:
        raise Exception("Screenshot returned empty image data")
    if data_url.startswith("data:") and "," in data_url:
        b64 = data_url.split(",", 1)[1]
    else:
        b64 = data_url
    raw = base64.b64decode(b64)
    # Cache screenshot ↔ viewport dimensions (consistent with browser_screenshot)
    sw = result.get("width")
    sh = result.get("height")
    vw = result.get("viewport_width", sw)
    vh = result.get("viewport_height", sh)
    if sw and sh:
        _last_screenshot_dims[tab_id or ""] = {"sw": sw, "sh": sh, "vw": vw, "vh": vh}
    # Ensure parent directory exists
    parent = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(parent, exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(raw)
    return f"Screenshot saved to {file_path} ({len(raw)} bytes, {sw or '?'}x{sh or '?'})"


# ── Cookies (Phase 7) ──────────────────────────────────────────


@mcp.tool()
async def browser_get_cookies(url: str = "", name: str = "", tab_id: str = "") -> str:
    """Get cookies for the current tab's domain or a specific URL.
    Optionally filter by cookie name. Uses the tab's origin attributes
    to correctly handle Total Cookie Protection partitioning."""
    params: dict = {"tab_id": tab_id or None}
    if url:
        params["url"] = url
    if name:
        params["name"] = name
    return text_result(await browser_command("get_cookies", params))


@mcp.tool()
async def browser_set_cookie(
    name: str,
    value: str = "",
    path: str = "/",
    secure: bool = False,
    httpOnly: bool = False,
    sameSite: str = "",
    expires: str = "",
    tab_id: str = "",
    frame_id: int = 0,
) -> str:
    """Set a cookie on the current page via document.cookie.
    The tab must be navigated to the target domain first.
    sameSite: 'None', 'Lax', or 'Strict'. expires: ISO date string or empty for session cookie."""
    params: dict = {
        "tab_id": tab_id or None,
        "name": name,
        "value": value,
        "path": path,
        "secure": secure,
        "httpOnly": httpOnly,
    }
    if sameSite:
        params["sameSite"] = sameSite
    if expires:
        params["expires"] = expires
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("set_cookie", params))


@mcp.tool()
async def browser_delete_cookies(url: str = "", name: str = "", tab_id: str = "") -> str:
    """Delete cookies for the current tab's domain or a URL. If name provided,
    deletes only that cookie. Otherwise deletes all cookies for the domain."""
    params: dict = {"tab_id": tab_id or None}
    if url:
        params["url"] = url
    if name:
        params["name"] = name
    return text_result(await browser_command("delete_cookies", params))


# ── Storage (Phase 7) ─────────────────────────────────────────


@mcp.tool()
async def browser_get_storage(
    storage_type: str, key: str = "", tab_id: str = "", frame_id: int = 0
) -> str:
    """Get localStorage or sessionStorage data from the current page.
    storage_type: 'localStorage' or 'sessionStorage'.
    key: specific key to get, or empty to dump all entries."""
    params = {"tab_id": tab_id or None, "storage_type": storage_type}
    if key:
        params["key"] = key
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("get_storage", params))


@mcp.tool()
async def browser_set_storage(
    storage_type: str, key: str, value: str, tab_id: str = "", frame_id: int = 0
) -> str:
    """Set a key-value pair in localStorage or sessionStorage.
    storage_type: 'localStorage' or 'sessionStorage'."""
    params = {
        "tab_id": tab_id or None,
        "storage_type": storage_type,
        "key": key,
        "value": value,
    }
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("set_storage", params))


@mcp.tool()
async def browser_delete_storage(
    storage_type: str, key: str = "", tab_id: str = "", frame_id: int = 0
) -> str:
    """Delete a key from localStorage/sessionStorage, or clear all if no key provided.
    storage_type: 'localStorage' or 'sessionStorage'."""
    params = {"tab_id": tab_id or None, "storage_type": storage_type}
    if key:
        params["key"] = key
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("delete_storage", params))


# ── Network Monitoring (Phase 7) ──────────────────────────────


@mcp.tool()
async def browser_network_monitor_start() -> str:
    """Start monitoring network requests. Records HTTP requests and responses
    into a circular buffer (500 entries). Call browser_network_get_log to retrieve."""
    return text_result(await browser_command("network_monitor_start"))


@mcp.tool()
async def browser_network_monitor_stop() -> str:
    """Stop monitoring network requests. The log buffer is preserved."""
    return text_result(await browser_command("network_monitor_stop"))


@mcp.tool()
async def browser_network_get_log(
    url_filter: str = "",
    method_filter: str = "",
    status_filter: int = 0,
    limit: int = 50,
) -> str:
    """Get captured network log entries. Filters are optional.
    url_filter: regex to match URLs. method_filter: GET/POST/etc.
    status_filter: HTTP status code (e.g. 404). limit: max entries to return."""
    params: dict = {"limit": limit}
    if url_filter:
        params["url_filter"] = url_filter
    if method_filter:
        params["method_filter"] = method_filter
    if status_filter:
        params["status_filter"] = status_filter
    result = await browser_command("network_get_log", params)
    if isinstance(result, list):
        if not result:
            return "(no network entries captured)"
        lines = []
        for entry in result:
            status = entry.get("status", "")
            status_str = f" [{status}]" if status else ""
            ct = entry.get("content_type", "")
            ct_str = f" ({ct})" if ct else ""
            lines.append(
                f"{entry.get('method', '?')} {entry.get('url', '?')}{status_str}{ct_str}"
            )
        return "\n".join(lines)
    return text_result(result)


# ── Request Interception (Phase 7) ────────────────────────────


@mcp.tool()
async def browser_intercept_add_rule(
    pattern: str, action: str, headers: str = ""
) -> str:
    """Add a network interception rule. Matched requests are blocked or modified.
    pattern: regex to match URLs. action: 'block' or 'modify_headers'.
    headers: JSON object of headers to set (only for modify_headers action)."""
    params: dict = {"pattern": pattern, "action": action}
    if headers:
        try:
            params["headers"] = json.loads(headers)
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON in headers parameter: {e}"
    return text_result(await browser_command("intercept_add_rule", params))


@mcp.tool()
async def browser_intercept_remove_rule(rule_id: int) -> str:
    """Remove a network interception rule by its ID."""
    return text_result(
        await browser_command("intercept_remove_rule", {"rule_id": rule_id})
    )


@mcp.tool()
async def browser_intercept_list_rules() -> str:
    """List all active network interception rules."""
    return text_result(await browser_command("intercept_list_rules"))


# ── Session Persistence (Phase 7) ─────────────────────────────


@mcp.tool()
async def browser_session_save(file_path: str) -> str:
    """Save the current browser session (open tabs + cookies) to a JSON file.
    Can be restored later with browser_session_restore."""
    return text_result(await browser_command("session_save", {"file_path": file_path}))


@mcp.tool()
async def browser_session_restore(file_path: str) -> str:
    """Restore a previously saved browser session from a JSON file.
    Reopens saved tabs and restores cookies."""
    return text_result(
        await browser_command("session_restore", {"file_path": file_path})
    )


# ── Multi-Tab Coordination (Phase 9) ──────────────────────────


@mcp.tool()
async def browser_compare_tabs(tab_ids: str) -> str:
    """Compare content across multiple tabs. Pass comma-separated tab IDs.
    Returns URL, title, and text preview (500 chars) for each tab.
    Useful for comparing search results, A/B testing, or verifying data across pages."""
    ids = [t.strip() for t in tab_ids.split(",") if t.strip()]
    if len(ids) < 2:
        return "Error: provide at least 2 comma-separated tab IDs"
    return text_result(await browser_command("compare_tabs", {"tab_ids": ids}))


@mcp.tool()
async def browser_batch_navigate(urls: str, persist: bool = False) -> str:
    """Open multiple URLs in new tabs at once. Pass comma-separated URLs.
    All tabs are created in the ZenRipple workspace.
    Returns the tab IDs for all opened tabs.
    Set persist=true to keep all tabs alive after session close."""
    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    if not url_list:
        return "Error: provide at least 1 URL"
    return text_result(await browser_command("batch_navigate", {"urls": url_list, "persist": persist}))


# ── Visual Grounding (Phase 9) ────────────────────────────────


@mcp.tool()
async def browser_find_element_by_description(
    description: str, tab_id: str = "", frame_id: int = 0
) -> str:
    """Find interactive elements matching a natural language description.
    Fuzzy-matches description words against element text, tag, role, and attributes.
    Returns top 5 candidates with their indices. Use the index with browser_click etc.
    Example: 'login button', 'search input', 'navigation menu'."""
    params: dict = {"tab_id": tab_id or None}
    if frame_id:
        params["frame_id"] = frame_id
    result = await browser_command("get_dom", params)
    if not isinstance(result, dict) or "elements" not in result:
        return "Error: could not get DOM"

    elements = result["elements"]
    if not elements:
        return "(no interactive elements found)"

    # Tokenize description into search words
    words = [w.lower() for w in description.split() if len(w) > 1]
    if not words:
        return "Error: description is empty"

    # Score each element by how many description words match
    scored = []
    for el in elements:
        text = (el.get("text") or "").lower()
        tag = el.get("tag", "").lower()
        role = (el.get("role") or "").lower()
        attrs = el.get("attributes") or {}
        href = (attrs.get("href") or "").lower()
        name = (attrs.get("name") or "").lower()
        etype = (attrs.get("type") or "").lower()
        aria = text  # aria-label is already in text via #getVisibleText

        searchable = f"{text} {tag} {role} {href} {name} {etype} {aria}"
        score = sum(1 for w in words if w in searchable)
        if score > 0:
            scored.append((score, el))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:5]

    if not top:
        return f"No elements match '{description}'"

    lines = [f"Matches for '{description}':"]
    for score, el in top:
        attrs = el.get("attributes") or {}
        detail = ""
        if attrs.get("href"):
            detail = f" \u2192{attrs['href'][:60]}"
        elif attrs.get("type"):
            detail = f" type={attrs['type']}"
        text = (el.get("text") or "").strip()[:80]
        tag = el["tag"]
        role_str = f" role={el['role']}" if el.get("role") else ""
        lines.append(
            f"  [{el['index']}] <{tag}{role_str}>{text}</{tag}>{detail} (score: {score}/{len(words)})"
        )
    return "\n".join(lines)


# ── Action Recording (Phase 9) ────────────────────────────────


@mcp.tool()
async def browser_record_start() -> str:
    """Start recording browser actions. All subsequent commands (navigation, clicks,
    typing, etc.) are logged. Use browser_record_stop to stop and browser_record_save
    to save the recording to a file for later replay."""
    return text_result(await browser_command("record_start"))


@mcp.tool()
async def browser_record_stop() -> str:
    """Stop recording browser actions. Returns the number of actions recorded."""
    return text_result(await browser_command("record_stop"))


@mcp.tool()
async def browser_record_save(file_path: str) -> str:
    """Save the recorded browser actions to a JSON file.
    The file can be replayed later with browser_record_replay."""
    return text_result(await browser_command("record_save", {"file_path": file_path}))


@mcp.tool()
async def browser_record_replay(file_path: str, delay: float = 0.5) -> str:
    """Replay a previously recorded set of browser actions from a JSON file.
    delay: seconds to wait between each action (default 0.5)."""
    return text_result(
        await browser_command("record_replay", {"file_path": file_path, "delay": delay})
    )


# ── Session Replay (Tool Call Log) ───────────────────────────────


@mcp.tool()
async def browser_replay_status() -> str:
    """Get current tool call logging status: tool call count, directory path, session ID."""
    active = _load_replay_state()

    if not active:
        return text_result({"active": False})

    # Count tool calls from the JSONL log on disk
    tool_call_count = 0
    if _replay_dir:
        log_path = os.path.join(_replay_dir, "tool_log.jsonl")
        try:
            with open(log_path, "r") as f:
                tool_call_count = sum(1 for _ in f)
        except (FileNotFoundError, OSError):
            pass

    # Read manifest for metadata
    manifest = {}
    if _replay_dir:
        manifest_path = os.path.join(_replay_dir, "manifest.json")
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    return text_result({
        "active": True,
        "dir": _replay_dir,
        "tool_call_count": tool_call_count,
        "started_at": manifest.get("started_at", ""),
        "session_id": manifest.get("session_id", ""),
    })


# ── Drag-and-Drop (Phase 10) ───────────────────────────────────


@mcp.tool()
async def browser_drag(
    source_index: int, target_index: int, steps: int = 10, tab_id: str = "", frame_id: int = 0
) -> str:
    """Drag an element to another element by their indices from browser_get_dom.
    Uses native mouse events (mousedown/mousemove/mouseup) and HTML5 DragEvent API.
    steps: number of intermediate mousemove events (default 10)."""
    params = {
        "tab_id": tab_id or None,
        "sourceIndex": source_index,
        "targetIndex": target_index,
        "steps": steps,
    }
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("drag_element", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_drag_coordinates(
    start_x: int, start_y: int, end_x: int, end_y: int,
    steps: int = 10, tab_id: str = "", frame_id: int = 0
) -> str:
    """Drag from one coordinate to another on the page.
    Uses native mouse events and HTML5 DragEvent API.
    steps: number of intermediate mousemove events (default 10)."""
    params = {
        "tab_id": tab_id or None,
        "startX": start_x,
        "startY": start_y,
        "endX": end_x,
        "endY": end_y,
        "steps": steps,
    }
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("drag_coordinates", params))

    return _append_notifications(result)


# ── Chrome-Context Eval (Phase 10) ─────────────────────────────


@mcp.tool()
async def browser_eval_chrome(expression: str) -> str:
    """Execute JavaScript in the browser's chrome (privileged) context.
    Has access to Services, gBrowser, Cc, Ci, Cu, IOUtils — the full
    Firefox/Zen XPCOM API. Use for browser-level queries and automation
    that content-context eval cannot do (e.g. reading prefs, accessing
    internal browser state)."""
    result = await browser_command("eval_chrome", {"expression": expression})
    if "error" in result:
        stack = result.get("stack", "")
        return _append_notifications(
            f"Error: {result['error']}" + (f"\n{stack}" if stack else "")
        )
    return _append_notifications(
        json.dumps(result.get("result"), indent=2, default=str)
    )


# ── Reflection (Phase 10) ─────────────────────────────────────


@mcp.tool()
async def browser_reflect(goal: str = "", tab_id: str = "") -> list:
    """Get a comprehensive snapshot of the current page for reasoning.
    Returns a screenshot (as an image) plus page text and metadata.
    Use this to understand the full page state before making decisions.
    goal: optional description of what you're trying to accomplish."""
    blocks = []
    errors = []

    # 1. Screenshot (best-effort — don't fail reflect if screenshot fails)
    try:
        screenshot_result = await browser_command("screenshot", {"tab_id": tab_id or None})
        data_url = screenshot_result.get("image", "")
        if data_url:
            if data_url.startswith("data:") and "," in data_url:
                header, b64 = data_url.split(",", 1)
                fmt = "jpeg" if "jpeg" in header else "png"
            else:
                b64 = data_url
                fmt = "jpeg"
            raw_bytes = base64.b64decode(b64)
            blocks.append(Image(data=raw_bytes, format=fmt))
            # Cache dimensions for auto-scaling click coordinates
            sw = screenshot_result.get("width")
            sh = screenshot_result.get("height")
            vw = screenshot_result.get("viewport_width", sw)
            vh = screenshot_result.get("viewport_height", sh)
            if sw and sh:
                _last_screenshot_dims[tab_id or ""] = {
                    "sw": sw, "sh": sh, "vw": vw, "vh": vh,
                }
    except Exception as e:
        errors.append(f"screenshot: {e}")

    # 2. Page text (best-effort)
    text_result_data = {}
    try:
        text_result_data = await browser_command("get_page_text", {"tab_id": tab_id or None})
    except Exception as e:
        errors.append(f"page_text: {e}")

    # 3. Page info (best-effort)
    info_result = {}
    try:
        info_result = await browser_command("get_page_info", {"tab_id": tab_id or None})
    except Exception as e:
        errors.append(f"page_info: {e}")

    # Add text summary
    summary = f"URL: {info_result.get('url', '?')}\n"
    summary += f"Title: {info_result.get('title', '?')}\n"
    summary += f"Loading: {info_result.get('loading', False)}\n"
    if goal:
        summary += f"\nGoal: {goal}\n"
    page_text = (text_result_data.get("text") or "")[:50000]
    summary += f"\n--- Page Text (first 50K chars) ---\n{page_text}"
    if errors:
        summary += f"\n\n--- Partial failures ---\n" + "\n".join(errors)
    blocks.append(summary)

    notif_text = _drain_notifications()
    if notif_text:
        blocks.append(notif_text)
    return blocks


# ── File Upload & Download (Phase 11) ──────────────────────────


@mcp.tool()
async def browser_file_upload(
    file_path: str, index: int, tab_id: str = "", frame_id: int = 0
) -> str:
    """Upload a file to an <input type="file"> element by its index from browser_get_dom.
    file_path: absolute path to a file on disk (must exist on the same machine as the browser).
    index: element index (must be an <input type="file">)."""
    params = {
        "tab_id": tab_id or None,
        "index": index,
        "file_path": file_path,
    }
    if frame_id:
        params["frame_id"] = frame_id
    result = text_result(await browser_command("file_upload", params))

    return _append_notifications(result)


@mcp.tool()
async def browser_wait_for_download(timeout: int = 60, save_to: str = "") -> str:
    """Wait for the next file download to complete in the browser.
    Listens for any new download to finish, then returns its file path and metadata.
    timeout: max seconds to wait (default 60).
    save_to: optional path to copy the downloaded file to."""
    params: dict = {"timeout": timeout}
    if save_to:
        params["save_to"] = save_to
    return text_result(await browser_command("wait_for_download", params))


# ── Session Management (Phase 12) ──────────────────────────────


@mcp.tool()
async def browser_session_info() -> str:
    """Get current session info: session_id, workspace, connections, tabs."""
    return text_result(await browser_command("session_info"))


@mcp.tool()
async def browser_session_close() -> str:
    """Close session. Created tabs are closed; claimed tabs are released back
    to unclaimed status so they persist in the workspace. The shared Zen AI
    Agent workspace is never destroyed."""
    global _session_id, _ws_connection
    result = await browser_command("session_close")
    # Clean up in-memory state so next call creates a fresh session
    _session_id = None
    _ws_connection = None
    # Clean up session file so next call from this terminal creates a fresh session
    if not SESSION_ID:
        _delete_session_file()
    return text_result(result)


@mcp.tool()
async def browser_list_sessions() -> str:
    """List all active browser sessions (admin/debug).
    Each session includes: session_id, name, workspace_name, connection_count,
    tab_count, created_at. Use the name field to see what other sessions are called."""
    return text_result(await browser_command("list_sessions"))


@mcp.tool()
async def browser_set_session_name(name: str) -> str:
    """Set a human-readable name for the current session.
    The name is displayed as a sublabel under each tab title in the sidebar,
    making it easy to see which agent session owns which tabs.
    Returns the set name and a list of other active session names so you can
    pick a unique name. Max 32 characters. Pass an empty string to clear the name."""
    return text_result(await browser_command("set_session_name", {"name": name}))


# ── Tab Claiming (Phase 13) ─────────────────────────────────────


@mcp.tool()
async def browser_list_workspace_tabs() -> str:
    """List ALL tabs in the ZenRipple workspace, including user-opened tabs
    and tabs from other agent sessions. Each tab shows its ownership status:
    'unclaimed' (user-opened, available to claim), 'owned' (active agent session),
    or 'stale' (owner session inactive for 2+ minutes, available to claim).
    Use browser_claim_tab to take ownership of unclaimed or stale tabs."""
    return text_result(await browser_command("list_workspace_tabs"))


@mcp.tool()
async def browser_claim_tab(tab_id: str) -> str:
    """Claim an unclaimed or stale tab in the workspace into this session.
    After claiming, the tab becomes accessible to all session-scoped tools
    (screenshot, get_dom, click, etc.). Only unclaimed tabs (user-opened)
    and stale tabs (owner inactive 2+ min) can be claimed.
    Claimed tabs automatically persist — they survive session close and are
    released back to unclaimed status instead of being destroyed.
    tab_id: the tab_id from browser_list_workspace_tabs, or a URL."""
    return text_result(await browser_command("claim_tab", {"tab_id": tab_id}))


# ── Health Check ─────────────────────────────────────────────────

def _read_version() -> str:
    """Read version from pyproject.toml so it stays in sync automatically."""
    try:
        toml_path = os.path.join(os.path.dirname(__file__), "pyproject.toml")
        with open(toml_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("version") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key == "version":
                        return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


MCP_SERVER_VERSION = _read_version()


@mcp.tool()
async def browser_ping() -> str:
    """Check if the browser agent is alive and responsive.
    Returns version info for both the MCP server and the browser agent.
    Warns if their versions don't match."""
    result = await browser_command("ping")
    browser_version = result.get("version", "unknown")
    info = {
        "status": "ok",
        "browser_agent_version": browser_version,
        "mcp_server_version": MCP_SERVER_VERSION,
        "session_id": result.get("session_id", ""),
    }
    if browser_version != MCP_SERVER_VERSION:
        info["warning"] = (
            f"Version mismatch: MCP server is v{MCP_SERVER_VERSION} but "
            f"browser agent is v{browser_version}. "
            "Run ./install.sh to update the browser agent."
        )
    return text_result(info)


# ── Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
