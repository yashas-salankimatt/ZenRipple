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
import json
import os
import re
import sys
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
                except OSError:
                    raise ConnectionError(
                        f"Cannot connect to Zen Browser at {WS_URL}. "
                        "Is Zen Browser running with ZenRipple installed?"
                    ) from first_err
            elif isinstance(first_err, OSError):
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

        for _ in range(20):
            raw = await asyncio.wait_for(self._ws.recv(), timeout=60)
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                if "error" in resp:
                    err = resp["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(msg)
                return resp.get("result", {})

        raise RuntimeError(f"No response for {method} after 20 messages")

    async def close(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


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
    "drag":           ("drag_element", ["from_index", "to_index"]),
    "drag-xy":        ("drag_coordinates", ["from_x", "from_y", "to_x", "to_y"]),
    # Wait
    "wait":           ("wait", ["seconds"]),
    "wait-load":      ("wait_for_load", []),
    "wait-el":        ("wait_for_element", ["selector"]),
    "wait-text":      ("wait_for_text", ["text"]),
    # Console — browser-side: console_evaluate, console_get_logs, console_get_errors
    "eval":           ("console_evaluate", ["expression"]),
    "logs":           ("console_get_logs", []),
    "errors":         ("console_get_errors", []),
    "console-setup":  ("console_setup", []),
    "console-teardown": ("console_teardown", []),
    # Clipboard
    "clip-read":      ("clipboard_read", []),
    "clip-write":     ("clipboard_write", ["text"]),
    # Cookies
    "cookies":        ("get_cookies", []),
    "set-cookie":     ("set_cookie", ["name", "value"]),
    "delete-cookies": ("delete_cookies", []),
    # Storage
    "storage":        ("get_storage", []),
    "set-storage":    ("set_storage", ["key", "value"]),
    "delete-storage": ("delete_storage", []),
    # Network
    "net-start":      ("network_monitor_start", []),
    "net-stop":       ("network_monitor_stop", []),
    "net-log":        ("network_get_log", []),
    # Intercept
    "intercept-add":  ("intercept_add_rule", []),
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
    "download":       ("wait_for_download", []),
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
}

# ── Arg Parsing Helpers ───────────────────────────────────────


def _auto_type(value: str):
    """Auto-convert CLI string to appropriate Python type."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


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
                params[key] = _auto_type(args[i + 1])
                i += 2
            else:
                params[key] = True
                i += 1
        else:
            if positional_idx < len(positional_names):
                params[positional_names[positional_idx]] = _auto_type(arg)
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
    elif _terminal_supports_inline_images():
        _print_inline_image(raw)
        print(f"Screenshot: {w}x{h}px ({len(raw)} bytes)", file=sys.stderr)
    else:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        print(json.dumps({
            "saved": tmp, "size_bytes": len(raw),
            "dimensions": f"{w}x{h}",
        }))
        print(f"Saved to: {tmp}", file=sys.stderr)
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
        print("Error: OPENROUTER_API_KEY not set", file=sys.stderr)
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
    if sw and vw and sw != vw:
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
    """Reflect: screenshot + page text + info combined."""
    tab_id = params.get("tab_id", "")

    # Screenshot
    ss_ok = False
    try:
        ss_result = await client.command("screenshot", {"tab_id": tab_id or None})
        data_url = ss_result.get("image", "")
        if data_url:
            b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
            raw = base64.b64decode(b64)
            if _terminal_supports_inline_images():
                _print_inline_image(raw)
            else:
                import tempfile
                fd, tmp = tempfile.mkstemp(suffix=".jpg")
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                print(f"Screenshot saved: {tmp}", file=sys.stderr)
            ss_ok = True
    except Exception as e:
        print(f"Screenshot failed: {e}", file=sys.stderr)

    # Page info
    try:
        info = await client.command("get_page_info", {"tab_id": tab_id or None})
        print(f"URL: {info.get('url', '?')}")
        print(f"Title: {info.get('title', '?')}")
        print(f"Loading: {info.get('loading', False)}")
    except Exception as e:
        print(f"Page info failed: {e}", file=sys.stderr)

    goal = params.get("goal", "")
    if goal:
        print(f"\nGoal: {goal}")

    # Page text
    try:
        text_result = await client.command("get_page_text", {"tab_id": tab_id or None})
        page_text = (text_result.get("text") or "")[:50000]
        print(f"\n--- Page Text ---\n{page_text}")
    except Exception as e:
        print(f"Page text failed: {e}", file=sys.stderr)

    return 0


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

    await ws.close()
    return session_id


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
            # Print export line that can be eval'd
            print(f"export ZENRIPPLE_SESSION_ID={session_id}")
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
  logs / errors             Get console output

Other:
  ping                      Health check
  reflect [--goal TEXT]     Full page snapshot
  cookies / storage         View cookies/storage
  net-start / net-stop      Network monitoring
  eval-chrome <expr>        Evaluate in chrome context
  upload <path> <index>     Upload file

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

    try:
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
            urls = args
            json_param = None
            for idx, a in enumerate(args):
                if a in ("-j", "--json"):
                    json_param = args[idx + 1] if idx + 1 < len(args) else None
                    urls = []
                    break
            if json_param:
                data = json.loads(json_param)
                url_str = data.get("urls", "")
                persist = data.get("persist", True)
            else:
                url_str = ",".join(urls)
                persist = True
                # Check for --persist flag
                clean_urls = []
                skip_next = False
                for idx, a in enumerate(args):
                    if skip_next:
                        skip_next = False
                        continue
                    if a == "--persist":
                        if idx + 1 < len(args):
                            persist = _auto_type(args[idx + 1])
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
        return 0

    except ConnectionError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        await client.close()


def entry():
    """Console script entry point."""
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    entry()
