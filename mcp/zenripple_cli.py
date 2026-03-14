#!/usr/bin/env python3
"""ZenRipple CLI — native command-line interface for Zen Browser control.

Replaces MCPorter with a direct WebSocket connection to the browser agent.
Faster startup, inline image support, and native session management.

Usage:
    zenripple <command> [args...]
    zenripple --help

Examples:
    zenripple ping
    zenripple create-tab https://example.com
    zenripple click 5
    zenripple screenshot
    zenripple screenshot --save page.jpg
    zenripple dom --viewport-only
    zenripple session new --name researcher
    zenripple session spawn --name sub-agent
"""

from __future__ import annotations

import asyncio
import base64
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows — file locking disabled
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import websockets

from zenripple_session_file import (
    read_session_file,
    write_session_file,
    delete_session_file,
)

# ── Constants ──────────────────────────────────────────────────

WS_URL = os.environ.get("ZENRIPPLE_WS_URL", "ws://localhost:9876")
SESSION_ID = os.environ.get("ZENRIPPLE_SESSION_ID", "")

# Track temp screenshot files for cleanup on exit.
# Only clean up when running interactively (stdout is a TTY).
# When invoked as a subprocess by the MCP server, the caller reads the temp
# files after the subprocess exits, so we must NOT delete them here.
_temp_files: list[str] = []


def _cleanup_temp_files():
    for f in _temp_files:
        try:
            os.unlink(f)
        except OSError:
            pass


if sys.stdout.isatty():
    import atexit
    atexit.register(_cleanup_temp_files)


def _read_auth_token() -> str:
    from_env = os.environ.get("ZENRIPPLE_AUTH_TOKEN", "").strip()
    if from_env:
        return from_env
    auth_file = Path.home() / ".zenripple" / "auth"
    try:
        return auth_file.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return ""


def _read_version() -> str:
    try:
        toml_path = Path(__file__).parent / "pyproject.toml"
        for line in toml_path.read_text().splitlines():
            s = line.strip()
            if s.startswith("version") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k == "version":
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


VERSION = _read_version()

# ── WebSocket Client ──────────────────────────────────────────


class BrowserClient:
    """Direct WebSocket client for browser commands."""

    def __init__(self, session_id: str | None = None):
        self._requested_session = session_id or SESSION_ID or ""
        self._ws: websockets.WebSocketClientProtocol | None = None
        self.session_id: str | None = None
        self.last_tab_url: str = ""
        self._pending_notifications: list[dict] = []

    async def connect(self):
        if self._ws is not None:
            return self

        reconnect_id = self._requested_session or read_session_file()
        if reconnect_id:
            url = f"{WS_URL}/session/{reconnect_id}"
        else:
            url = f"{WS_URL}/new"

        token = _read_auth_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        try:
            self._ws = await websockets.connect(
                url,
                max_size=10 * 1024 * 1024,
                additional_headers=headers,
            )
        except Exception as first_err:
            if reconnect_id and not SESSION_ID:
                # Session expired, create new
                url = f"{WS_URL}/new"
                try:
                    self._ws = await websockets.connect(
                        url,
                        max_size=10 * 1024 * 1024,
                        additional_headers=headers,
                    )
                except (OSError, websockets.WebSocketException):
                    raise ConnectionError(
                        f"Cannot connect to Zen Browser at {WS_URL}. "
                        "Is Zen Browser running with ZenRipple installed?"
                    ) from first_err
            elif isinstance(first_err, (OSError, websockets.WebSocketException)):
                raise ConnectionError(
                    f"Cannot connect to Zen Browser at {WS_URL}. "
                    "Is Zen Browser running with ZenRipple installed?"
                ) from first_err
            else:
                raise

        # Extract session ID from response headers
        resp_headers = None
        if hasattr(self._ws, "response") and self._ws.response:
            resp_headers = self._ws.response.headers
        elif hasattr(self._ws, "response_headers"):
            resp_headers = self._ws.response_headers
        if resp_headers:
            self.session_id = resp_headers.get("X-ZenRipple-Session")
            if self.session_id and not SESSION_ID:
                write_session_file(self.session_id)

        return self

    async def command(self, method: str, params: dict | None = None) -> dict:
        if not self._ws:
            await self.connect()

        msg_id = str(uuid4())
        await self._ws.send(json.dumps({
            "id": msg_id,
            "method": method,
            "params": params or {},
        }))

        deadline = time.monotonic() + 120  # Overall 2-minute timeout per command
        try:
            for _ in range(50):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"Timed out waiting for {method} response")
                per_msg_timeout = min(60.0, remaining)
                raw = await asyncio.wait_for(self._ws.recv(), timeout=per_msg_timeout)
                resp = json.loads(raw)
                if resp.get("id") == msg_id:
                    # Extract piggybacked notifications (dialog/popup events)
                    notifications = resp.get("_notifications")
                    if notifications:
                        self._pending_notifications.extend(notifications)
                    # Track the active tab URL
                    self.last_tab_url = resp.get("_tab_url", "") or ""
                    if "error" in resp:
                        err = resp["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        raise RuntimeError(msg)
                    return resp.get("result", {})
                # Non-matching message — still extract any notifications
                notifications = resp.get("_notifications")
                if notifications:
                    self._pending_notifications.extend(notifications)
        except websockets.ConnectionClosed:
            self._ws = None
            raise ConnectionError("Browser WebSocket closed unexpectedly")

        raise RuntimeError(f"No response for {method} after 50 messages")

    async def close(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


    def drain_notifications(self) -> str:
        """Drain accumulated notifications and format as text."""
        if not self._pending_notifications:
            return ""
        notifs = list(self._pending_notifications)
        self._pending_notifications.clear()
        parts = []
        for n in notifs:
            if n.get("type") == "dialog_opened":
                parts.append(
                    f'\n--- NOTIFICATION: A {n.get("dialog_type", "unknown")} dialog appeared: '
                    f'"{n.get("message", "")}" ---\n'
                    f'Handle with: zenripple handle-dialog --action accept (or dismiss)'
                )
            elif n.get("type") == "popup_blocked":
                urls = n.get("popup_urls") or []
                count = n.get("blocked_count", 1)
                parts.append(
                    f"\n--- NOTIFICATION: Browser blocked {count} popup(s)"
                    f'{" (" + ", ".join(urls) + ")" if urls else ""} ---\n'
                    f"Allow with: zenripple popup-allow"
                )
            else:
                parts.append(
                    f'\n--- NOTIFICATION ({n.get("type", "unknown")}): '
                    f'{json.dumps(n, default=str)} ---'
                )
        return "".join(parts)


# ── Inline Image Support ──────────────────────────────────────


def _terminal_supports_inline_images() -> bool:
    """Check if terminal supports iTerm2 inline image protocol."""
    term = os.environ.get("TERM_PROGRAM", "")
    lc_terminal = os.environ.get("LC_TERMINAL", "")
    if term in ("iTerm.app", "WezTerm", "mintty"):
        return True
    if lc_terminal == "iTerm2":
        return True
    if os.environ.get("KITTY_WINDOW_ID"):
        return True
    return False


def _print_inline_image(data: bytes, filename: str = "screenshot.jpg"):
    """Display image inline using iTerm2 protocol (works in iTerm2, WezTerm, Kitty)."""
    b64 = base64.b64encode(data).decode("ascii")
    # iTerm2 inline image protocol
    sys.stdout.write(
        f"\033]1337;File=inline=1;size={len(data)};name={base64.b64encode(filename.encode()).decode()}:{b64}\a\n"
    )
    sys.stdout.flush()


# ── Command Definitions ───────────────────────────────────────

# Maps CLI command names to (browser_method, [positional_arg_names])
# Commands not listed here can still be called with the full method name.
# Maps CLI command → (browser WebSocket method, [positional_arg_names])
# Method names must match the browser-side commandHandlers, NOT the MCP tool names.
COMMANDS: dict[str, tuple[str, list[str]]] = {
    # Navigation
    "create-tab":     ("create_tab", ["url"]),
    "close-tab":      ("close_tab", ["tab_id"]),
    "switch-tab":     ("switch_tab", ["tab_id"]),
    "list-tabs":      ("list_tabs", []),
    "navigate":       ("navigate", ["url"]),
    "nav":            ("navigate", ["url"]),
    "back":           ("go_back", []),
    "forward":        ("go_forward", []),
    "reload":         ("reload", []),
    # Page info
    "info":           ("get_page_info", []),
    "dom":            ("get_dom", []),
    "text":           ("get_page_text", []),
    "html":           ("get_page_html", []),
    "frames":         ("list_frames", []),
    "nav-status":     ("get_navigation_status", []),
    "tab-events":     ("get_tab_events", []),
    # Interaction — note: browser-side method names differ from MCP tool names
    "click":          ("click_element", ["index"]),
    "click-xy":       ("click_coordinates", ["x", "y"]),
    "fill":           ("fill_field", ["index", "value"]),
    "type":           ("type_text", ["text"]),
    "key":            ("press_key", ["key"]),
    "select":         ("select_option", ["index", "value"]),
    "scroll":         ("scroll", ["direction", "amount"]),
    "hover":          ("hover", ["index"]),
    "hover-xy":       ("hover_coordinates", ["x", "y"]),
    "scroll-xy":      ("scroll_at_point", ["x", "y", "direction", "amount"]),
    "drag":           ("drag_element", ["sourceIndex", "targetIndex"]),
    "drag-xy":        ("drag_coordinates", ["startX", "startY", "endX", "endY"]),
    # Wait
    "wait":           ("wait", ["seconds"]),
    "wait-load":      ("wait_for_load", []),
    "wait-el":        ("wait_for_element", ["selector"]),
    "wait-text":      ("wait_for_text", ["text"]),
    # Console — browser-side: console_evaluate, console_get_logs, console_get_errors
    "eval":           ("console_evaluate", ["expression"]),
    "console-setup":  ("console_setup", []),
    "console-teardown": ("console_teardown", []),
    # Clipboard
    "clip-read":      ("clipboard_read", []),
    "clip-write":     ("clipboard_write", ["text"]),
    # Cookies
    "cookies":        ("get_cookies", ["url", "name"]),
    "set-cookie":     ("set_cookie", ["name", "value"]),
    "delete-cookies": ("delete_cookies", ["url", "name"]),
    # Storage
    "storage":        ("get_storage", ["storage_type", "key"]),
    "set-storage":    ("set_storage", ["storage_type", "key", "value"]),
    "delete-storage": ("delete_storage", ["storage_type", "key"]),
    # Network
    "net-start":      ("network_monitor_start", []),
    "net-stop":       ("network_monitor_stop", []),
    # Intercept
    "intercept-remove": ("intercept_remove_rule", ["rule_id"]),
    "intercept-list": ("intercept_list_rules", []),
    # Record/Replay
    "record-start":   ("record_start", []),
    "record-stop":    ("record_stop", []),
    "record-save":    ("record_save", ["file_path"]),
    "record-replay":  ("record_replay", []),
    "replay-status":  ("replay_status", []),
    # Dialogs
    "dialogs":        ("get_dialogs", []),
    "handle-dialog":  ("handle_dialog", []),
    # File
    "upload":         ("file_upload", ["file_path", "index"]),
    "download":       ("wait_for_download", ["timeout"]),
    # Chrome
    "eval-chrome":    ("eval_chrome", ["expression"]),
    # Popup
    "popup-allow":    ("allow_blocked_popup", []),
    "popup-events":   ("get_popup_blocked_events", []),
    # Workspace
    "workspace-tabs": ("list_workspace_tabs", []),
    "claim-tab":      ("claim_tab", ["tab_id"]),
    # Session save/restore
    "session-save":   ("session_save", ["file_path"]),
    "session-restore": ("session_restore", ["file_path"]),
}

# Commands that need special handling (not just browser_command proxy)
SPECIAL_COMMANDS = {
    "ping", "screenshot", "ss", "save-screenshot",
    "session",
    "elements", "a11y",
    "find",
    "gclick", "ghover", "gscroll",
    "batch-nav",
    "compare",
    "reflect",
    "logs", "errors", "net-log",
    "intercept-add",
    "replay-status",
    "approve", "notify",
}

# ── Arg Parsing Helpers ───────────────────────────────────────


def _auto_type(value: str, hint: str = ""):
    """Auto-convert CLI string to appropriate Python type.

    Uses the parameter name (hint) to decide whether to coerce:
    - Known boolean params → bool
    - Known numeric params → int/float
    - Everything else stays as a string (preserving form values, URLs, etc.)
    """
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    # If we have a param name hint, only coerce numbers for known numeric params
    if hint and hint not in _NUMERIC_PARAMS:
        return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


# Parameter names that should be coerced to numeric types
_NUMERIC_PARAMS = frozenset({
    "index", "from_index", "to_index", "sourceIndex", "targetIndex",
    "amount", "x", "y", "startX", "startY", "endX", "endY",
    "timeout", "max_elements", "frame_id",
    "startLine", "endLine", "max_results",
    "width", "height", "delay",
    "rule_id", "seconds",
})


def _parse_tool_args(args: list[str], positional_names: list[str]) -> dict:
    """Parse CLI args into a parameter dict.

    Supports:
      positional: zenripple click 5
      named:      zenripple click --index 5
      json:       zenripple click -j '{"index": 5}'
    """
    params: dict = {}
    i = 0
    positional_idx = 0

    while i < len(args):
        arg = args[i]
        if arg in ("-j", "--json"):
            if i + 1 < len(args):
                params.update(json.loads(args[i + 1]))
                i += 2
            else:
                print("Error: -j/--json requires a JSON string argument", file=sys.stderr)
                sys.exit(1)
        elif arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                params[key] = _auto_type(args[i + 1], hint=key)
                i += 2
            else:
                params[key] = True
                i += 1
        else:
            hint = positional_names[positional_idx] if positional_idx < len(positional_names) else ""
            if positional_idx < len(positional_names):
                params[positional_names[positional_idx]] = _auto_type(arg, hint=hint)
                positional_idx += 1
            else:
                # Extra positional args collected in _extra
                params.setdefault("_extra", []).append(arg)
            i += 1

    return params


# ── Special Command Handlers ──────────────────────────────────


async def handle_ping(client: BrowserClient) -> int:
    result = await client.command("ping")
    browser_version = result.get("version", "unknown")
    info = {
        "status": "pong",
        "browser_agent_version": browser_version,
        "cli_version": VERSION,
        "session_id": result.get("session_id", ""),
    }
    if browser_version != VERSION:
        info["warning"] = (
            f"Version mismatch: CLI is v{VERSION} but browser agent "
            f"is v{browser_version}. Run ./install.sh to update."
        )
    print(json.dumps(info, indent=2))
    return 0


async def handle_screenshot(client: BrowserClient, params: dict) -> int:
    tab_id = params.get("tab_id", "")
    result = await client.command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if not data_url:
        print("Error: screenshot returned empty image data", file=sys.stderr)
        return 1

    b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
    raw = base64.b64decode(b64)
    w = result.get("width", "?")
    h = result.get("height", "?")

    save_path = params.get("save")
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(raw)
        print(json.dumps({
            "saved": save_path, "size_bytes": len(raw),
            "dimensions": f"{w}x{h}",
        }))
    else:
        # Always save to a temp file so agents can Read it.
        # Also print inline if terminal supports it (for human users).
        fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="zenripple_ss_")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        _temp_files.append(tmp)
        if _terminal_supports_inline_images():
            _print_inline_image(raw)
        print(json.dumps({
            "saved": tmp, "size_bytes": len(raw),
            "dimensions": f"{w}x{h}",
        }))
    return 0


async def handle_elements(client: BrowserClient, params: dict) -> int:
    """get_elements_compact: calls get_dom and formats compactly."""
    dom_params: dict = {"tab_id": params.get("tab_id") or None}
    if params.get("viewport_only"):
        dom_params["viewport_only"] = True
    if params.get("max_elements"):
        dom_params["max_elements"] = params["max_elements"]
    if params.get("frame_id"):
        dom_params["frame_id"] = params["frame_id"]

    result = await client.command("get_dom", dom_params)
    if isinstance(result, dict) and "elements" in result:
        print(f"URL: {result.get('url', '?')} | Title: {result.get('title', '?')}")
        for el in result["elements"]:
            tag = el["tag"]
            text = el.get("text", "").strip()
            attrs = el.get("attributes") or {}
            detail = ""
            if attrs.get("href"):
                detail = f" \u2192{attrs['href']}"
            elif attrs.get("value"):
                detail = f" ={attrs['value']}"
            elif attrs.get("type"):
                detail = f" type={attrs['type']}"
            role = f" role={el['role']}" if el.get("role") else ""
            print(f"[{el['index']}] {text} ({tag}{role}{detail})")
    else:
        print(json.dumps(result, indent=2))
    return 0


async def handle_a11y(client: BrowserClient, params: dict) -> int:
    """get_accessibility_tree: formats as indented tree."""
    a11y_params: dict = {"tab_id": params.get("tab_id") or None}
    if params.get("frame_id"):
        a11y_params["frame_id"] = params["frame_id"]

    result = await client.command("get_accessibility_tree", a11y_params)
    if isinstance(result, dict):
        if result.get("error"):
            print(f"Error: {result['error']}", file=sys.stderr)
            return 1
        nodes = result.get("nodes", [])
        if not nodes:
            print("(no accessibility nodes found)")
            return 0
        print(f"Accessibility tree ({result.get('total', len(nodes))} nodes):")
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
            print(entry)
    else:
        print(json.dumps(result, indent=2))
    return 0


async def handle_find(client: BrowserClient, params: dict) -> int:
    """find_element_by_description: fuzzy DOM search."""
    description = params.get("description", "")
    if not description:
        print("Error: description required", file=sys.stderr)
        return 1

    dom_params: dict = {"tab_id": params.get("tab_id") or None}
    if params.get("frame_id"):
        dom_params["frame_id"] = params["frame_id"]

    result = await client.command("get_dom", dom_params)
    if not isinstance(result, dict) or "elements" not in result:
        print("Error: could not get DOM", file=sys.stderr)
        return 1

    elements = result["elements"]
    if not elements:
        print("(no interactive elements found)")
        return 0

    words = [w.lower() for w in description.split() if len(w) > 1]
    if not words:
        print("Error: description is empty", file=sys.stderr)
        return 1

    scored = []
    for el in elements:
        text = (el.get("text") or "").lower()
        tag = el.get("tag", "").lower()
        role = (el.get("role") or "").lower()
        attrs = el.get("attributes") or {}
        searchable = f"{text} {tag} {role} {(attrs.get('href') or '').lower()} {(attrs.get('name') or '').lower()} {(attrs.get('type') or '').lower()}"
        score = sum(1 for w in words if w in searchable)
        if score > 0:
            scored.append((score, el))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:5]

    if not top:
        print(f"No elements match '{description}'")
        return 0

    print(f"Matches for '{description}':")
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
        print(f"  [{el['index']}] <{tag}{role_str}>{text}</{tag}>{detail} (score: {score}/{len(words)})")
    return 0


# ── Grounded VLM Interaction ──────────────────────────────────

_RE_QWEN_BOX = re.compile(r"<\|box_start\|>\((\d+),\s*(\d+)\)<\|box_end\|>")
_RE_POINT_TAG = re.compile(r"<point>(\d+)\s+(\d+)</point>")
_RE_BBOX = re.compile(r"[\(\[]\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*[\)\]]")
_RE_NORM_FLOAT = re.compile(r"[\(\[]\s*([01]\.\d+)\s*,\s*([01]\.\d+)\s*[\)\]]")
_RE_ABS_INT = re.compile(r"[\(\[]\s*(\d+)\s*,\s*(\d+)\s*[\)\]]")
_RE_ABS_DEC = re.compile(r"[\(\[]\s*(\d+\.\d+)\s*,\s*(\d+\.\d+)\s*[\)\]]")

_GROUNDING_MODEL = os.environ.get(
    "ZENRIPPLE_GROUNDING_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"
)
_GROUNDING_API_URL = os.environ.get(
    "ZENRIPPLE_GROUNDING_API_URL", "https://openrouter.ai/api/v1/chat/completions"
)
_GROUNDING_COORD_MODE = os.environ.get("ZENRIPPLE_GROUNDING_COORD_MODE", "norm1000")


def _parse_grounding_coords(text: str, img_w: int, img_h: int, coord_mode: str):
    def _denorm(x, y):
        if coord_mode == "norm1000":
            return round(x * img_w / 1000), round(y * img_h / 1000)
        return x, y

    m = _RE_QWEN_BOX.search(text)
    if m:
        return _denorm(int(m.group(1)), int(m.group(2)))
    m = _RE_POINT_TAG.search(text)
    if m:
        return _denorm(int(m.group(1)), int(m.group(2)))
    m = _RE_BBOX.search(text)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return _denorm((x1 + x2) // 2, (y1 + y2) // 2)
    m = _RE_NORM_FLOAT.search(text)
    if m:
        return round(float(m.group(1)) * img_w), round(float(m.group(2)) * img_h)
    m = _RE_ABS_INT.search(text)
    if m:
        return _denorm(int(m.group(1)), int(m.group(2)))
    m = _RE_ABS_DEC.search(text)
    if m:
        return _denorm(round(float(m.group(1))), round(float(m.group(2))))
    return None, None


async def _get_grounding_key(client: BrowserClient) -> str:
    """Get API key from env var or browser prefs."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        result = await client.command("get_config", {"key": "openrouter_api_key"})
        return result.get("value", "")
    except Exception:
        return ""


async def _vlm_locate(
    description: str, b64: str, media_type: str,
    sw: int, sh: int, api_key: str,
) -> tuple[int | None, int | None, str | None]:
    """Send screenshot to VLM, parse coordinates. Returns (px, py, error)."""
    prompt = (
        f"This is a {sw}x{sh} pixel screenshot. Find the exact pixel coordinates "
        f"of {description}. Return ONLY the center point coordinates as (x, y). "
        f"Nothing else."
    )
    payload = {
        "model": _GROUNDING_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 100,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    vlm_text = None
    last_error = None
    async with httpx.AsyncClient(timeout=30.0) as http:
        for attempt in range(3):
            try:
                resp = await http.post(_GROUNDING_API_URL, headers=headers, json=payload)
                resp.raise_for_status()
                vlm_text = resp.json()["choices"][0]["message"]["content"]
                break
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code >= 500 or e.response.status_code == 429:
                    if attempt < 2:
                        await asyncio.sleep(1.0 * (2 ** attempt))
                    continue
                return None, None, f"VLM API returned {e.response.status_code}"
            except httpx.TransportError as e:
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(1.0 * (2 ** attempt))

    if vlm_text is None:
        return None, None, f"VLM request failed: {last_error}"

    px, py = _parse_grounding_coords(vlm_text, sw, sh, _GROUNDING_COORD_MODE)
    if px is None:
        return None, None, f"Could not parse coordinates from: {vlm_text[:200]}"

    px = max(0, min(px, sw - 1)) if sw else px
    py = max(0, min(py, sh - 1)) if sh else py
    return px, py, None


async def _grounded_action(
    client: BrowserClient, description: str, action: str,
    params: dict,
) -> int:
    """Common logic for grounded click/hover/scroll."""
    api_key = await _get_grounding_key(client)
    if not api_key:
        print(
            "Error: No OpenRouter API key found. Set OPENROUTER_API_KEY env var or "
            "store it in browser prefs (see SKILL.md § OpenRouter API Key).",
            file=sys.stderr,
        )
        return 1

    tab_id = params.get("tab_id", "")
    result = await client.command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if not data_url:
        print("Error: screenshot returned empty image", file=sys.stderr)
        return 1

    if "," in data_url:
        header, b64 = data_url.split(",", 1)
        media_type = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
    else:
        b64 = data_url
        media_type = "image/png"

    sw = result.get("width", 0)
    sh = result.get("height", 0)
    vw = result.get("viewport_width", sw)
    vh = result.get("viewport_height", sh)

    px, py, err = await _vlm_locate(description, b64, media_type, sw, sh, api_key)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    # Scale to viewport
    if sw and sh and vw and sw != vw:
        click_x, click_y = round(px * vw / sw), round(py * vh / sh)
    else:
        click_x, click_y = px, py

    if action == "click":
        r = await client.command("click_native", {"tab_id": tab_id or None, "x": click_x, "y": click_y})
    elif action == "hover":
        r = await client.command("hover_coordinates", {"tab_id": tab_id or None, "x": click_x, "y": click_y})
    elif action == "scroll":
        direction = params.get("direction", "down")
        amount = params.get("amount", 500)
        r = await client.command("scroll_at_point", {
            "tab_id": tab_id or None, "x": click_x, "y": click_y,
            "direction": direction, "amount": amount,
        })
    else:
        r = {}

    print(json.dumps({
        "action": action,
        "description": description,
        "vlm_coords": [px, py],
        "viewport_coords": [click_x, click_y],
        "result": r,
    }, indent=2))
    return 0


async def handle_reflect(client: BrowserClient, params: dict) -> int:
    """Reflect: screenshot + page text + info combined. Outputs JSON."""
    tab_id = params.get("tab_id", "")
    result: dict = {}

    # Screenshot
    try:
        ss_result = await client.command("screenshot", {"tab_id": tab_id or None})
        data_url = ss_result.get("image", "")
        if data_url:
            b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
            raw = base64.b64decode(b64)
            fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="zenripple_reflect_")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            _temp_files.append(tmp)
            result["screenshot_path"] = tmp
            result["screenshot_dimensions"] = f"{ss_result.get('width', '?')}x{ss_result.get('height', '?')}"
            if _terminal_supports_inline_images():
                _print_inline_image(raw)
    except ConnectionError:
        raise  # Let connection failures propagate
    except Exception as e:
        result["screenshot_error"] = str(e)

    # Page info
    try:
        info = await client.command("get_page_info", {"tab_id": tab_id or None})
        result["url"] = info.get("url", "?")
        result["title"] = info.get("title", "?")
        result["loading"] = info.get("loading", False)
    except ConnectionError:
        raise
    except Exception as e:
        result["info_error"] = str(e)

    goal = params.get("goal", "")
    if goal:
        result["goal"] = goal

    # Page text
    try:
        text_result_data = await client.command("get_page_text", {"tab_id": tab_id or None})
        result["page_text"] = (text_result_data.get("text") or "")[:50000]
    except ConnectionError:
        raise
    except Exception as e:
        result["text_error"] = str(e)

    # Notifications
    notif = client.drain_notifications()
    if notif:
        result["notifications"] = notif

    print(json.dumps(result, indent=2))
    return 0


# ── Formatted Output Handlers ────────────────────────────────


async def handle_console_logs(client: BrowserClient, params: dict) -> int:
    """Get captured console logs with formatted output."""
    cmd_params = {"tab_id": params.get("tab_id") or None}
    if params.get("frame_id"):
        cmd_params["frame_id"] = params["frame_id"]
    result = await client.command("console_get_logs", cmd_params)
    if isinstance(result, dict) and "logs" in result:
        if not result["logs"]:
            print("(no console logs captured)")
        else:
            for log in result["logs"]:
                ts = log.get("timestamp", "")
                level = log.get("level", "log")
                msg = log.get("message", "")
                print(f"[{level}] {ts} {msg}")
    else:
        print(json.dumps(result, indent=2))
    notif = client.drain_notifications()
    if notif:
        print(notif, file=sys.stderr)
    return 0


async def handle_console_errors(client: BrowserClient, params: dict) -> int:
    """Get captured console errors with formatted output."""
    cmd_params = {"tab_id": params.get("tab_id") or None}
    if params.get("frame_id"):
        cmd_params["frame_id"] = params["frame_id"]
    result = await client.command("console_get_errors", cmd_params)
    if isinstance(result, dict) and "errors" in result:
        if not result["errors"]:
            print("(no errors captured)")
        else:
            entries = []
            for err in result["errors"]:
                ts = err.get("timestamp", "")
                etype = err.get("type", "error")
                msg = err.get("message", "")
                stack = err.get("stack", "")
                entry = f"[{etype}] {ts} {msg}"
                if stack:
                    entry += "\n" + stack
                entries.append(entry)
            print("\n\n".join(entries))
    else:
        print(json.dumps(result, indent=2))
    notif = client.drain_notifications()
    if notif:
        print(notif, file=sys.stderr)
    return 0


async def handle_network_log(client: BrowserClient, params: dict) -> int:
    """Get network log with formatted output."""
    cmd_params: dict = {"limit": params.get("limit", 50)}
    if params.get("url_filter"):
        cmd_params["url_filter"] = params["url_filter"]
    if params.get("method_filter"):
        cmd_params["method_filter"] = params["method_filter"]
    if params.get("status_filter"):
        cmd_params["status_filter"] = params["status_filter"]
    result = await client.command("network_get_log", cmd_params)
    if isinstance(result, list):
        if not result:
            print("(no network entries captured)")
        else:
            for entry in result:
                status = entry.get("status", "")
                status_str = f" [{status}]" if status else ""
                ct = entry.get("content_type", "")
                ct_str = f" ({ct})" if ct else ""
                print(f"{entry.get('method', '?')} {entry.get('url', '?')}{status_str}{ct_str}")
    else:
        print(json.dumps(result, indent=2))
    return 0


async def handle_intercept_add(client: BrowserClient, params: dict) -> int:
    """Add intercept rule, parsing headers JSON if provided."""
    cmd_params: dict = {
        "pattern": params.get("pattern", ""),
        "action": params.get("action", ""),
    }
    headers = params.get("headers", "")
    if headers and isinstance(headers, str):
        try:
            cmd_params["headers"] = json.loads(headers)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in headers: {e}", file=sys.stderr)
            return 1
    elif headers:
        cmd_params["headers"] = headers
    result = await client.command("intercept_add_rule", cmd_params)
    print(json.dumps(result, indent=2))
    return 0


# ── Conversation Linking ──────────────────────────────────────


def _find_claude_code_conversation() -> str | None:
    """Walk up PID tree to find the Claude Code process and its conversation JSONL.

    Returns the absolute path to the conversation file, or None if not found.
    """
    try:
        pid = os.getpid()
        # Walk up the process tree (max 20 levels)
        for _ in range(20):
            pid = _get_parent_pid(pid)
            if pid <= 1:
                break
            cmdline = _get_process_cmdline(pid)
            if not cmdline:
                continue
            # Claude Code's binary is named "claude" — match the executable name,
            # not just any command line containing "claude" (which would match
            # shell snapshots like /Users/.../.claude/shell-snapshots/...)
            exe_name = os.path.basename(cmdline[0]).lower()
            if exe_name == "claude":
                # Found Claude Code process — resolve its working directory
                cwd = _get_process_cwd(pid)
                if cwd:
                    result = _find_conversation_jsonl(cwd)
                    if result:
                        return result
                    # CWD found but no JSONL — log for debugging and continue
                    print(f"zenripple: found Claude at PID {pid} cwd={cwd} but no JSONL",
                          file=sys.stderr)
    except Exception as e:
        print(f"zenripple: conversation linking error: {e}", file=sys.stderr)

    # Fallback: check CLAUDE_PROJECT_DIR env var if set
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir:
        return _find_conversation_jsonl(project_dir)

    return None


def _get_parent_pid(pid: int) -> int:
    """Get parent PID of a process."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split(":")[1].strip())
    except (FileNotFoundError, PermissionError, OSError):
        pass
    # macOS fallback: use ps
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return 0


def _get_process_cmdline(pid: int) -> list[str] | None:
    """Get command line of a process."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
            if data:
                return [s.decode("utf-8", errors="replace")
                        for s in data.rstrip(b"\x00").split(b"\x00")]
    except (FileNotFoundError, PermissionError, OSError):
        pass
    # macOS fallback
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()
    except Exception:
        pass
    return None


def _get_process_cwd(pid: int) -> str | None:
    """Get working directory of a process."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    # macOS fallback: use lsof
    try:
        import subprocess
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("n") and line[1:].startswith("/"):
                    return line[1:]
    except Exception:
        pass
    return None


def _find_conversation_jsonl(project_cwd: str) -> str | None:
    """Find the most recent Claude Code conversation JSONL for a project directory."""
    home = Path.home()
    # Claude Code stores conversations at ~/.claude/projects/<path-hash>/<uuid>.jsonl
    # The path hash replaces all non-alphanumeric-non-dash chars with dashes.
    # e.g. /Users/foo/my_project → -Users-foo-my-project
    path_hash = re.sub(r"[^a-zA-Z0-9-]", "-", project_cwd)
    projects_dir = home / ".claude" / "projects" / path_hash

    if not projects_dir.is_dir():
        print(f"zenripple: conversation linking: project dir not found: {projects_dir}",
              file=sys.stderr)
        return None

    # Find the most recently modified .jsonl file
    best_path = None
    best_mtime = 0.0
    try:
        for f in projects_dir.iterdir():
            if f.suffix == ".jsonl" and f.is_file():
                mtime = f.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_path = str(f)
    except OSError:
        pass

    # Only return if modified within last 5 minutes (active conversation)
    if best_path and (time.time() - best_mtime) < 300:
        return best_path

    if best_path:
        age = int(time.time() - best_mtime)
        print(f"zenripple: conversation JSONL found but too old ({age}s): {best_path}",
              file=sys.stderr)

    return None


def _write_conversation_link(session_id: str, conversation_path: str) -> None:
    """Write conversation.link file to the replay directory."""
    safe_id = _sanitize_session_id(session_id)
    if not safe_id:
        return
    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    os.makedirs(replay_dir, exist_ok=True)
    link_path = os.path.join(replay_dir, "conversation.link")
    try:
        with open(link_path, "w") as f:
            f.write(conversation_path)
    except OSError:
        pass


def _try_link_conversation(session_id: str) -> None:
    """Attempt to link the current session to a Claude Code conversation."""
    if not session_id:
        return
    safe_id = _sanitize_session_id(session_id)
    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    link_path = os.path.join(replay_dir, "conversation.link")
    # Don't re-link if already linked
    if os.path.exists(link_path):
        try:
            with open(link_path) as f:
                existing = f.read().strip()
            if existing and os.path.exists(existing):
                return
        except OSError:
            pass
    conversation_path = _find_claude_code_conversation()
    if conversation_path:
        _write_conversation_link(session_id, conversation_path)
        print(f"zenripple: linked conversation: {conversation_path}", file=sys.stderr)
    else:
        print("zenripple: conversation linking failed — no Claude Code conversation found",
              file=sys.stderr)


# ── Approval Gates ────────────────────────────────────────────


async def handle_approve(client: BrowserClient, params: dict) -> int:
    """Request human approval. Blocks until approved/denied or timeout."""
    description = params.get("description", "")
    if not description:
        print("Usage: zenripple approve <description> [--tab-id ID] [--timeout SECONDS]",
              file=sys.stderr)
        return 1

    session_id = client.session_id or SESSION_ID
    if not session_id:
        print("Error: no active session for approval", file=sys.stderr)
        return 1

    safe_id = _sanitize_session_id(session_id)
    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    os.makedirs(replay_dir, exist_ok=True)

    approval_id = f"appr_{uuid4().hex[:12]}"
    timeout_secs = params.get("timeout", 300)  # Default 5 minutes

    # Take a screenshot for context
    screenshot_file = None
    tab_url = ""
    try:
        tab_id = params.get("tab_id", "")
        ss_result = await client.command("screenshot", {"tab_id": tab_id or None})
        data_url = ss_result.get("image", "")
        if data_url:
            b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
            raw_bytes = base64.b64decode(b64)
            screenshot_file = f"approval_{approval_id}.jpg"
            with open(os.path.join(replay_dir, screenshot_file), "wb") as f:
                f.write(raw_bytes)
        # Get current tab URL
        try:
            info = await client.command("get_page_info", {"tab_id": tab_id or None})
            tab_url = info.get("url", "")
        except Exception:
            pass
    except Exception:
        pass

    # Write approval request
    request = {
        "id": approval_id,
        "description": description,
        "screenshot": screenshot_file,
        "tab_url": tab_url,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    approvals_path = os.path.join(replay_dir, "approvals.jsonl")
    _append_jsonl(approvals_path, request)

    print(json.dumps({
        "approval_id": approval_id,
        "status": "pending",
        "message": f"Waiting for human approval: {description}",
    }), file=sys.stderr)

    # Poll for resolution
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        resolution = _check_approval_status(approvals_path, approval_id)
        if resolution:
            result = {
                "approved": resolution["status"] == "approved",
                "approval_id": approval_id,
            }
            if resolution.get("message"):
                result["message"] = resolution["message"]
            print(json.dumps(result))
            return 0

    # Timeout
    timeout_entry = {
        "id": approval_id,
        "status": "denied",
        "message": "Timed out waiting for human approval",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_jsonl(approvals_path, timeout_entry)
    print(json.dumps({
        "approved": False,
        "approval_id": approval_id,
        "message": "Timed out waiting for human approval",
    }))
    return 0


def _check_approval_status(approvals_path: str, approval_id: str) -> dict | None:
    """Check if an approval has been resolved."""
    try:
        with open(approvals_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("id") == approval_id and entry.get("status") in ("approved", "denied"):
                        return entry
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        pass
    return None


# ── Agent Messages ────────────────────────────────────────────


async def handle_notify(client: BrowserClient, params: dict) -> int:
    """Send a non-blocking message from agent to human."""
    text = params.get("text", "")
    if not text:
        print("Usage: zenripple notify <message>", file=sys.stderr)
        return 1

    session_id = client.session_id or SESSION_ID
    if not session_id:
        print("Error: no active session for notification", file=sys.stderr)
        return 1

    safe_id = _sanitize_session_id(session_id)
    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    os.makedirs(replay_dir, exist_ok=True)

    message = {
        "id": f"msg_{uuid4().hex[:12]}",
        "direction": "agent_to_human",
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    messages_path = os.path.join(replay_dir, "messages.jsonl")
    _append_jsonl(messages_path, message)
    print(json.dumps({"sent": True, "message_id": message["id"]}))
    return 0


def _append_jsonl(path: str, entry: dict) -> None:
    """Append a JSON line to a JSONL file under file lock."""
    lock_path = path + ".lock"
    line = json.dumps(entry, default=str) + "\n"
    try:
        if fcntl:
            with open(lock_path, "a") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    with open(path, "a") as f:
                        f.write(line)
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        else:
            with open(path, "a") as f:
                f.write(line)
    except Exception as e:
        print(f"Warning: failed to write to {path}: {e}", file=sys.stderr)


def _read_undelivered_messages(session_id: str) -> list[dict]:
    """Read human→agent messages that haven't been delivered yet.

    Uses append-only delivery tracking: delivery records are separate lines
    with {"delivered": msg_id}. No file rewriting needed.
    """
    safe_id = _sanitize_session_id(session_id)
    if not safe_id:
        return []
    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    messages_path = os.path.join(replay_dir, "messages.jsonl")
    delivered_ids: set[str] = set()
    human_messages: list[dict] = []
    try:
        with open(messages_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("delivered"):
                        delivered_ids.add(entry["delivered"])
                    elif entry.get("direction") == "human_to_agent":
                        human_messages.append(entry)
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        pass
    return [m for m in human_messages if m.get("id") and m["id"] not in delivered_ids]


def _mark_messages_delivered(session_id: str, message_ids: list[str]) -> None:
    """Mark messages as delivered by appending delivery records (append-only, no rewrite)."""
    if not message_ids:
        return
    safe_id = _sanitize_session_id(session_id)
    if not safe_id:
        return
    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    messages_path = os.path.join(replay_dir, "messages.jsonl")
    now = datetime.now(timezone.utc).isoformat()

    for msg_id in message_ids:
        _append_jsonl(messages_path, {"delivered": msg_id, "at": now})


async def handle_replay_status(client: BrowserClient) -> int:
    """Get replay/tool-call logging status."""
    session_id = client.session_id or SESSION_ID
    if REPLAY_DISABLED or not session_id:
        print(json.dumps({"active": False}))
        return 0

    safe_id = _sanitize_session_id(session_id)
    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")

    tool_call_count = 0
    manifest = {}
    log_path = os.path.join(replay_dir, "tool_log.jsonl")
    manifest_path = os.path.join(replay_dir, "manifest.json")
    try:
        with open(log_path, "r") as f:
            tool_call_count = sum(1 for _ in f)
    except (FileNotFoundError, OSError):
        pass
    try:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    print(json.dumps({
        "active": True,
        "dir": replay_dir,
        "tool_call_count": tool_call_count,
        "started_at": manifest.get("started_at", ""),
        "session_id": manifest.get("session_id", ""),
    }, indent=2))
    return 0


# ── Session Replay (Tool Call Logging) ────────────────────────

# Auto-on when ZENRIPPLE_SESSION_ID env var is set, opt-out with ZENRIPPLE_NO_REPLAY=1
REPLAY_DISABLED = os.environ.get("ZENRIPPLE_NO_REPLAY", "").strip().lower() not in ("", "0", "false", "no")
try:
    REPLAY_KEEP = int(os.environ.get("ZENRIPPLE_REPLAY_KEEP", "50"))
except (ValueError, TypeError):
    REPLAY_KEEP = 50

# Navigation tools need a brief delay before screenshot so the page has loaded.
_NAVIGATION_COMMANDS = frozenset({
    "create-tab", "navigate", "nav", "back", "forward", "reload", "batch-nav",
})


def _sanitize_session_id(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)


def _prune_old_replays(current_dir: str | None) -> None:
    """Delete oldest replay directories when count exceeds REPLAY_KEEP."""
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
                ts = datetime.fromtimestamp(os.path.getmtime(d), tz=timezone.utc).isoformat()
            except OSError:
                ts = ""
        dated.append((ts, d))

    dated.sort(key=lambda x: x[0])
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
        except Exception:
            pass


def _init_replay_dir(session_id: str) -> str | None:
    """Initialize replay directory and manifest. Returns dir path or None."""
    if REPLAY_DISABLED or not session_id:
        return None

    safe_id = _sanitize_session_id(session_id)
    if not safe_id:
        return None

    replay_dir = os.path.join(tempfile.gettempdir(), f"zenripple_replay_{safe_id}")
    os.makedirs(replay_dir, exist_ok=True)

    manifest_path = os.path.join(replay_dir, "manifest.json")
    lock_path = os.path.join(replay_dir, "replay.lock")

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
    except Exception:
        pass

    _prune_old_replays(replay_dir)
    return replay_dir


def _claim_next_seq(replay_dir: str) -> int:
    """Atomically claim the next sequence number. Returns -1 on error."""
    manifest_path = os.path.join(replay_dir, "manifest.json")
    lock_path = os.path.join(replay_dir, "replay.lock")

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
        if fcntl:
            with open(lock_path, "a") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    return _do_claim()
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        else:
            return _do_claim()
    except Exception:
        return -1


def _append_log_entry(replay_dir: str, entry: dict) -> None:
    """Append a JSON line to tool_log.jsonl under file lock."""
    log_path = os.path.join(replay_dir, "tool_log.jsonl")
    lock_path = os.path.join(replay_dir, "replay.lock")
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
    except Exception:
        pass


async def _save_replay_screenshot(
    client: BrowserClient, replay_dir: str, tool_name: str, seq: int,
    tab_id: str = "",
) -> str | None:
    """Capture and save a screenshot for a replay log entry."""
    try:
        ss_result = await client.command("screenshot", {"tab_id": tab_id or None})
        data_url = ss_result.get("image", "")
        if data_url:
            b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
            raw_bytes = base64.b64decode(b64)
            filename = f"{seq:05d}_{tool_name}.jpg"
            with open(os.path.join(replay_dir, filename), "wb") as f:
                f.write(raw_bytes)
            return filename
    except Exception:
        pass
    return None


async def _record_replay(
    client: BrowserClient, replay_dir: str, command: str,
    params: dict, timestamp: str, duration_ms: float,
    error: bool = False,
    pre_seq: int = -1, pre_screenshot: str | None = None,
) -> None:
    """Log a tool call with screenshot to the session replay directory."""
    seq = pre_seq if pre_seq >= 0 else _claim_next_seq(replay_dir)
    if seq < 0:
        return

    screenshot_file = pre_screenshot
    if screenshot_file is None:
        tab_id = params.get("tab_id", "") if isinstance(params, dict) else ""
        if command in _NAVIGATION_COMMANDS:
            try:
                await asyncio.sleep(0.3)
                await client.command("wait_for_load", {"tab_id": tab_id or None, "timeout": 5})
            except Exception:
                pass
        screenshot_file = await _save_replay_screenshot(client, replay_dir, command, seq, tab_id)

    entry = {
        "seq": seq,
        "tool": command,
        "args": params,
        "timestamp": timestamp,
        "duration_ms": round(duration_ms, 1),
        "screenshot": screenshot_file,
        "error": error,
    }
    if client.last_tab_url:
        entry["tab_url"] = client.last_tab_url
    _append_log_entry(replay_dir, entry)


# ── Session Management ────────────────────────────────────────


async def _create_session(name: str | None = None) -> str | None:
    """Connect to /new to create a session, optionally name it."""
    token = _read_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = await websockets.connect(
        f"{WS_URL}/new",
        max_size=10 * 1024 * 1024,
        additional_headers=headers,
    )
    try:
        session_id = None
        resp_headers = None
        if hasattr(ws, "response") and ws.response:
            resp_headers = ws.response.headers
        elif hasattr(ws, "response_headers"):
            resp_headers = ws.response_headers
        if resp_headers:
            session_id = resp_headers.get("X-ZenRipple-Session")

        if name and session_id:
            msg_id = str(uuid4())
            await ws.send(json.dumps({
                "id": msg_id, "method": "set_session_name",
                "params": {"name": name},
            }))
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except Exception:
                pass

        return session_id
    finally:
        await ws.close()


async def handle_session(client: BrowserClient, args: list[str]) -> int:
    if not args:
        print("Usage: zenripple session <new|close|list|info|name|spawn>", file=sys.stderr)
        return 1

    subcmd = args[0]
    rest = args[1:]
    params = _parse_tool_args(rest, ["name"] if subcmd in ("new", "spawn", "name") else [])

    if subcmd == "new":
        session_id = await _create_session(params.get("name"))
        if session_id:
            print(json.dumps({"session_id": session_id}))
        else:
            print("Error: could not create session", file=sys.stderr)
            return 1

    elif subcmd == "spawn":
        session_id = await _create_session(params.get("name"))
        if session_id:
            # Print export line that can be eval'd (shell-quoted for safety)
            from shlex import quote as _shquote
            print(f"export ZENRIPPLE_SESSION_ID={_shquote(session_id)}")
        else:
            print("Error: could not create session", file=sys.stderr)
            return 1

    elif subcmd == "close":
        await client.connect()
        result = await client.command("session_close")
        # Clean up session file
        if not SESSION_ID:
            delete_session_file()
        print(json.dumps(result, indent=2))

    elif subcmd == "list":
        await client.connect()
        result = await client.command("list_sessions")
        print(json.dumps(result, indent=2))

    elif subcmd == "info":
        await client.connect()
        result = await client.command("session_info")
        print(json.dumps(result, indent=2))

    elif subcmd == "name":
        name = params.get("name", "")
        if not name:
            print("Usage: zenripple session name <name>", file=sys.stderr)
            return 1
        await client.connect()
        result = await client.command("set_session_name", {"name": name})
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown session subcommand: {subcmd}", file=sys.stderr)
        print("Available: new, spawn, close, list, info, name", file=sys.stderr)
        return 1

    return 0


# ── Main Dispatch ─────────────────────────────────────────────


HELP_TEXT = """\
zenripple — native CLI for Zen Browser control

Usage: zenripple [-s SESSION] <command> [args...]

Navigation:
  create-tab [url]          Open a new tab
  nav <url>                 Navigate to URL
  back / forward / reload   History navigation
  list-tabs                 List all session tabs
  close-tab [tab_id]        Close a tab
  switch-tab <tab_id>       Switch to a tab
  batch-nav <url1> <url2>   Open multiple URLs at once

Page Info:
  dom [--viewport-only]     Get interactive elements
  elements [--viewport-only] Compact element list
  a11y                      Accessibility tree
  text                      Page text content
  html                      Full page HTML
  info                      Page URL, title, status
  screenshot [--save PATH]  Screenshot (inline or save)
  ss [--save PATH]          Alias for screenshot

Interaction:
  click <index>             Click element by index
  click-xy <x> <y>         Click at coordinates
  gclick <description>      VLM-grounded click
  fill <index> <value>      Fill form field
  type <text>               Type text
  key <key> [--ctrl] [--shift] [--alt] [--meta]
  select <index> <value>    Select dropdown option
  scroll [direction] [amount]
  hover <index>             Hover element
  find <description>        Fuzzy-find elements by description

Wait:
  wait [seconds]            Wait N seconds
  wait-load [--timeout N]   Wait for page load
  wait-el <selector>        Wait for element
  wait-text <text>          Wait for text

Session:
  session new [--name N]    Create new session
  session spawn [--name N]  Create session, print export line
  session close             Close current session
  session list              List all sessions
  session info              Current session info
  session name <name>       Set session display name

Console:
  eval <expression>         Evaluate JavaScript
  logs                      Get console logs (formatted)
  errors                    Get console errors (formatted)
  console-setup / -teardown Enable/disable console capture

Cookies & Storage:
  cookies [url] [name]      Get cookies
  set-cookie <n> <v>        Set a cookie (+ --path, --secure, etc.)
  delete-cookies [url] [n]  Delete cookies
  storage <type> [key]      Get localStorage/sessionStorage
  set-storage <type> <k> <v> Set storage entry
  delete-storage <type> [k] Delete storage entry

Network:
  net-start / net-stop      Start/stop network monitoring
  net-log [--url-filter R]  Get captured network log (formatted)
  intercept-add <pat> <act> Add request interception rule
  intercept-remove <id>     Remove interception rule
  intercept-list            List interception rules

Dashboard:
  approve <description>     Request human approval (blocks until response)
  notify <message>          Send message to human (non-blocking)

Other:
  ping                      Health check
  reflect [--goal TEXT]     Full page snapshot (JSON output)
  eval-chrome <expr>        Evaluate in chrome context
  upload <path> <index>     Upload file
  download [timeout]        Wait for download
  replay-status             Get replay/logging status
  workspace-tabs            List all workspace tabs
  claim-tab <tab_id>        Claim an unclaimed tab
  drag <src> <tgt>          Drag element to element
  drag-xy <x1> <y1> <x2> <y2>  Drag between coordinates

Flags:
  -s, --session ID          Use specific session ID
  -j, --json '{...}'        Pass params as JSON
  --help                    Show this help

Examples:
  zenripple ping
  zenripple create-tab https://example.com
  zenripple click 5
  zenripple fill 3 "hello@example.com"
  zenripple ss --save page.jpg
  zenripple session spawn --name researcher
  eval "$(zenripple session spawn --name sub-agent)"
"""


async def _dispatch(command: str, args: list[str], client: BrowserClient) -> int:
    """Dispatch a command to the appropriate handler. Returns exit code."""

    # ── Special commands ──
    if command == "ping":
        await client.connect()
        return await handle_ping(client)

    if command in ("screenshot", "ss"):
        params = _parse_tool_args(args, [])
        await client.connect()
        return await handle_screenshot(client, params)

    if command == "save-screenshot":
        params = _parse_tool_args(args, ["file_path"])
        path = params.pop("file_path", None)
        if not path:
            print("Usage: zenripple save-screenshot <path>", file=sys.stderr)
            return 1
        params["save"] = path
        await client.connect()
        return await handle_screenshot(client, params)

    if command == "session":
        return await handle_session(client, args)

    if command == "elements":
        params = _parse_tool_args(args, [])
        await client.connect()
        return await handle_elements(client, params)

    if command == "a11y":
        params = _parse_tool_args(args, [])
        await client.connect()
        return await handle_a11y(client, params)

    if command == "find":
        params = _parse_tool_args(args, ["description"])
        await client.connect()
        return await handle_find(client, params)

    if command == "gclick":
        params = _parse_tool_args(args, ["description"])
        desc = params.pop("description", "")
        if not desc:
            print("Usage: zenripple gclick <description>", file=sys.stderr)
            return 1
        await client.connect()
        return await _grounded_action(client, desc, "click", params)

    if command == "ghover":
        params = _parse_tool_args(args, ["description"])
        desc = params.pop("description", "")
        if not desc:
            print("Usage: zenripple ghover <description>", file=sys.stderr)
            return 1
        await client.connect()
        return await _grounded_action(client, desc, "hover", params)

    if command == "gscroll":
        params = _parse_tool_args(args, ["description", "direction", "amount"])
        desc = params.pop("description", "")
        if not desc:
            print("Usage: zenripple gscroll <description> [direction] [amount]", file=sys.stderr)
            return 1
        await client.connect()
        return await _grounded_action(client, desc, "scroll", params)

    if command == "batch-nav":
        _sentinel = object()
        json_param = _sentinel
        for idx, a in enumerate(args):
            if a in ("-j", "--json"):
                if idx + 1 >= len(args):
                    print("Error: -j/--json requires a JSON string argument", file=sys.stderr)
                    return 1
                json_param = args[idx + 1]
                break
        if json_param is not _sentinel:
            data = json.loads(json_param)
            urls_val = data.get("urls", "")
            # Accept both list and comma-separated string
            url_str = ",".join(urls_val) if isinstance(urls_val, list) else urls_val
            persist = data.get("persist", True)
        else:
            persist = True
            clean_urls = []
            skip_next = False
            for idx, a in enumerate(args):
                if skip_next:
                    skip_next = False
                    continue
                if a == "--persist":
                    if idx + 1 < len(args):
                        persist = _auto_type(args[idx + 1], hint="persist")
                        skip_next = True
                    continue
                clean_urls.append(a)
            url_str = ",".join(clean_urls)
        await client.connect()
        result = await client.command("batch_navigate", {
            "urls": [u.strip() for u in url_str.split(",") if u.strip()],
            "persist": persist,
        })
        print(json.dumps(result, indent=2))
        return 0

    if command == "compare":
        params = _parse_tool_args(args, ["tab_ids"])
        tab_ids_str = params.get("tab_ids", "")
        if not tab_ids_str:
            print("Usage: zenripple compare <tab_id1,tab_id2,...>", file=sys.stderr)
            return 1
        ids = [t.strip() for t in tab_ids_str.split(",") if t.strip()]
        await client.connect()
        result = await client.command("compare_tabs", {"tab_ids": ids})
        print(json.dumps(result, indent=2))
        return 0

    if command == "reflect":
        params = _parse_tool_args(args, ["goal"])
        await client.connect()
        return await handle_reflect(client, params)

    if command == "logs":
        params = _parse_tool_args(args, [])
        await client.connect()
        return await handle_console_logs(client, params)

    if command == "errors":
        params = _parse_tool_args(args, [])
        await client.connect()
        return await handle_console_errors(client, params)

    if command == "net-log":
        params = _parse_tool_args(args, [])
        await client.connect()
        return await handle_network_log(client, params)

    if command == "intercept-add":
        params = _parse_tool_args(args, ["pattern", "action"])
        await client.connect()
        return await handle_intercept_add(client, params)

    if command == "replay-status":
        await client.connect()
        return await handle_replay_status(client)

    if command == "approve":
        params = _parse_tool_args(args, ["description"])
        await client.connect()
        return await handle_approve(client, params)

    if command == "notify":
        params = _parse_tool_args(args, ["text"])
        await client.connect()
        return await handle_notify(client, params)

    # ── Generic command dispatch ──
    if command in COMMANDS:
        method, positional_names = COMMANDS[command]
    else:
        # Try matching by method name directly (with hyphens → underscores)
        method = command.replace("-", "_")
        positional_names = []

    params = _parse_tool_args(args, positional_names)
    params.pop("_extra", None)

    # Handle modifiers for press_key
    if method == "press_key":
        modifiers = {}
        for mod in ("ctrl", "shift", "alt", "meta"):
            if params.pop(mod, False):
                modifiers[mod] = True
        if modifiers:
            params["modifiers"] = modifiers

    # Normalize tab_id/frame_id
    tab_id = params.get("tab_id")
    if tab_id is not None:
        params["tab_id"] = tab_id or None

    await client.connect()
    result = await client.command(method, params if params else None)
    print(json.dumps(result, indent=2))

    # Output any pending notifications
    notif = client.drain_notifications()
    if notif:
        print(notif, file=sys.stderr)

    return 0


async def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Parse global flags
    session_override = None
    filtered_argv: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in ("-s", "--session") and i + 1 < len(argv):
            session_override = argv[i + 1]
            i += 2
        elif argv[i] in ("-h", "--help"):
            print(HELP_TEXT)
            return 0
        else:
            filtered_argv.append(argv[i])
            i += 1

    if not filtered_argv:
        print(HELP_TEXT)
        return 0

    command = filtered_argv[0]
    args = filtered_argv[1:]
    client = BrowserClient(session_id=session_override)

    # Replay state
    replay_dir: str | None = None
    pre_seq = -1
    pre_screenshot: str | None = None
    timestamp = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    result_code = 0
    had_error = False

    try:
        # Initialize replay after connecting (need session ID)
        # For commands that don't connect, replay is skipped
        result_code = await _dispatch(command, args, client)

    except ConnectionError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        result_code = 1
        had_error = True
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        result_code = 1
        had_error = True
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        result_code = 1
        had_error = True
    finally:
        duration_ms = (time.monotonic() - start) * 1000

        # Record replay if session is active and replay is enabled
        session_id = client.session_id or SESSION_ID

        # Attempt conversation linking on first tool call
        if session_id and command not in ("ping", "session", "replay-status"):
            try:
                _try_link_conversation(session_id)
            except Exception:
                pass

        if session_id and not REPLAY_DISABLED and command not in ("ping", "session", "replay-status"):
            try:
                replay_dir = _init_replay_dir(session_id)
                if replay_dir and client._ws:
                    parsed_params = _parse_tool_args(args, COMMANDS.get(command, ("", []))[1] if command in COMMANDS else [])
                    await _record_replay(
                        client, replay_dir, command, parsed_params,
                        timestamp, duration_ms, error=had_error,
                    )
            except Exception as e:
                print(f"Warning: replay recording failed: {e}", file=sys.stderr)

        await client.close()

    return result_code


def entry():
    """Console script entry point."""
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    entry()
