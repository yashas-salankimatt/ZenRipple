#!/usr/bin/env python3
"""
ZenLeap Browser MCP Server
Exposes Zen Browser control tools to Claude Code via Model Context Protocol.
Connects to the ZenLeap Agent WebSocket server running in the browser.
"""

import asyncio
import base64
import json
import os
from uuid import uuid4

import websockets

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

BROWSER_WS_URL = os.environ.get("ZENLEAP_WS_URL", "ws://localhost:9876")
SESSION_ID = os.environ.get("ZENLEAP_SESSION_ID", "")

mcp = FastMCP(
    "zenleap-browser",
    instructions=(
        "Browser control tools for Zen Browser via ZenLeap Agent. "
        "All tab operations are scoped to the 'ZenLeap AI' workspace."
    ),
)

_ws_connection = None
_ws_lock = asyncio.Lock()
_ws_command_lock = asyncio.Lock()
_session_id: str | None = None  # Populated from X-ZenLeap-Session after connect


async def get_ws():
    """Get or create WebSocket connection to browser.

    Reconnection strategy:
    1. If ZENLEAP_SESSION_ID env var is set, always join that session
    2. If we previously connected and have a saved _session_id, rejoin it
    3. Otherwise create a new session via /new
    This prevents tab loss when the WebSocket connection drops mid-operation.
    """
    global _ws_connection, _session_id
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

        # Route: env var > saved session from previous connection > new
        reconnect_id = SESSION_ID or _session_id
        if reconnect_id:
            url = f"{BROWSER_WS_URL}/session/{reconnect_id}"
        else:
            url = f"{BROWSER_WS_URL}/new"

        try:
            _ws_connection = await websockets.connect(
                url,
                max_size=10 * 1024 * 1024,  # 10MB — screenshots can exceed 1MB
                ping_interval=30,  # Send keepalive every 30s
                ping_timeout=120,  # Wait up to 120s for pong (browser may be busy)
            )
        except Exception:
            if reconnect_id and not SESSION_ID:
                # Session was destroyed (grace timer expired) — create a new one
                _session_id = None
                url = f"{BROWSER_WS_URL}/new"
                _ws_connection = await websockets.connect(
                    url,
                    max_size=10 * 1024 * 1024,
                    ping_interval=30,
                    ping_timeout=120,
                )
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
            _session_id = headers.get("X-ZenLeap-Session")

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
            except Exception:
                # Connection-level error (send/recv failed, timeout, etc.)
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
            resp = json.loads(raw)
            if "error" in resp:
                raise Exception(resp["error"].get("message", "Unknown browser error"))
            return resp.get("result", {})
    raise RuntimeError("browser_command: unreachable")


def text_result(data) -> str:
    """Format result as string for MCP tool return."""
    if isinstance(data, (dict, list)):
        return json.dumps(data, indent=2)
    return str(data)


# ── Tab Management ──────────────────────────────────────────────


@mcp.tool()
async def browser_create_tab(url: str = "about:blank") -> str:
    """Create a new browser tab in the ZenLeap AI workspace and navigate to a URL."""
    return text_result(await browser_command("create_tab", {"url": url}))


@mcp.tool()
async def browser_close_tab(tab_id: str = "") -> str:
    """Close a browser tab. If no tab_id, closes the active tab."""
    return text_result(
        await browser_command("close_tab", {"tab_id": tab_id or None})
    )


@mcp.tool()
async def browser_switch_tab(tab_id: str) -> str:
    """Switch to a different tab in the ZenLeap AI workspace."""
    return text_result(await browser_command("switch_tab", {"tab_id": tab_id}))


@mcp.tool()
async def browser_list_tabs() -> str:
    """List all open tabs in the ZenLeap AI workspace with IDs, titles, and URLs."""
    return text_result(await browser_command("list_tabs"))


# ── Navigation ──────────────────────────────────────────────────


@mcp.tool()
async def browser_navigate(url: str, tab_id: str = "") -> str:
    """Navigate a tab to a URL. If no tab_id, navigates the active tab."""
    return text_result(
        await browser_command("navigate", {"url": url, "tab_id": tab_id or None})
    )


@mcp.tool()
async def browser_go_back(tab_id: str = "") -> str:
    """Navigate back in a tab's history."""
    return text_result(
        await browser_command("go_back", {"tab_id": tab_id or None})
    )


@mcp.tool()
async def browser_go_forward(tab_id: str = "") -> str:
    """Navigate forward in a tab's history."""
    return text_result(
        await browser_command("go_forward", {"tab_id": tab_id or None})
    )


@mcp.tool()
async def browser_reload(tab_id: str = "") -> str:
    """Reload a tab."""
    return text_result(
        await browser_command("reload", {"tab_id": tab_id or None})
    )


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
    return text_result(await browser_command("handle_dialog", params))


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
async def browser_screenshot(tab_id: str = "") -> Image:
    """Take a screenshot of a browser tab. Returns the image so you can see the page.
    Use this to verify page state, understand layouts, or see visual content."""
    result = await browser_command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    # Strip data URL prefix: "data:image/jpeg;base64,..." or "data:image/png;base64,..."
    if data_url.startswith("data:"):
        header, b64 = data_url.split(",", 1)
        fmt = "jpeg" if "jpeg" in header else "png"
    else:
        b64 = data_url
        fmt = "jpeg"
    raw_bytes = base64.b64decode(b64)
    return Image(data=raw_bytes, format=fmt)


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
    return text_result(await browser_command("click_element", params))


@mcp.tool()
async def browser_click_coordinates(x: int, y: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Click at specific x,y coordinates on the page.
    Use browser_screenshot + browser_get_dom to identify coordinates."""
    params = {"tab_id": tab_id or None, "x": x, "y": y}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("click_coordinates", params))


@mcp.tool()
async def browser_fill(index: int, value: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Fill a form field (input/textarea) with a value by its index from browser_get_dom.
    Clears existing content and sets the new value, dispatching input/change events."""
    params = {"tab_id": tab_id or None, "index": index, "value": value}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("fill_field", params))


@mcp.tool()
async def browser_select_option(index: int, value: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Select an option in a <select> dropdown by its index from browser_get_dom.
    The value can be the option's value attribute or visible text."""
    params = {"tab_id": tab_id or None, "index": index, "value": value}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("select_option", params))


@mcp.tool()
async def browser_type(text: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Type text character-by-character into the currently focused element.
    Dispatches keydown/keypress/keyup and input events for each character.
    Focus an element first with browser_click."""
    params = {"tab_id": tab_id or None, "text": text}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("type_text", params))


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
    return text_result(await browser_command("press_key", params))


@mcp.tool()
async def browser_scroll(
    direction: str = "down", amount: int = 500, tab_id: str = "", frame_id: int = 0
) -> str:
    """Scroll the page in a direction (up/down/left/right) by a pixel amount.
    Default is 500 pixels down."""
    params = {"tab_id": tab_id or None, "direction": direction, "amount": amount}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("scroll", params))


@mcp.tool()
async def browser_hover(index: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Hover over an interactive element by its index from browser_get_dom.
    Dispatches mouseenter/mouseover/mousemove events. Useful for revealing tooltips or dropdown menus."""
    params = {"tab_id": tab_id or None, "index": index}
    if frame_id:
        params["frame_id"] = frame_id
    return text_result(await browser_command("hover", params))


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
            return f"Error: {result['error']}" + (f"\n{stack}" if stack else "")
        if "result" in result:
            return str(result["result"])
    return text_result(result)


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
    return text_result(await browser_command("wait_for_element", params))


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
    return text_result(await browser_command("wait_for_text", params))


@mcp.tool()
async def browser_wait_for_load(tab_id: str = "", timeout: int = 15) -> str:
    """Wait for the current page to finish loading (up to timeout seconds).
    More reliable than browser_wait for navigation — polls the browser's loading state.
    Returns the final URL and title once loaded."""
    return text_result(
        await browser_command(
            "wait_for_load",
            {"tab_id": tab_id or None, "timeout": timeout},
        )
    )


@mcp.tool()
async def browser_save_screenshot(file_path: str, tab_id: str = "") -> str:
    """Take a screenshot and save it as an image file to the given path.
    Use this to save visual evidence of page state to disk.
    The file_path can be absolute or relative to the server's working directory."""
    result = await browser_command("screenshot", {"tab_id": tab_id or None})
    data_url = result.get("image", "")
    if data_url.startswith("data:"):
        b64 = data_url.split(",", 1)[1]
    else:
        b64 = data_url
    raw = base64.b64decode(b64)
    # Ensure parent directory exists
    parent = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(parent, exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(raw)
    width = result.get("width", "?")
    height = result.get("height", "?")
    return f"Screenshot saved to {file_path} ({len(raw)} bytes, {width}x{height})"


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
        params["headers"] = json.loads(headers)
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
async def browser_batch_navigate(urls: str) -> str:
    """Open multiple URLs in new tabs at once. Pass comma-separated URLs.
    All tabs are created in the ZenLeap AI workspace.
    Returns the tab IDs for all opened tabs."""
    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    if not url_list:
        return "Error: provide at least 1 URL"
    return text_result(await browser_command("batch_navigate", {"urls": url_list}))


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
    return text_result(await browser_command("drag_element", params))


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
    return text_result(await browser_command("drag_coordinates", params))


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
        return f"Error: {result['error']}" + (f"\n{stack}" if stack else "")
    return json.dumps(result.get("result"), indent=2, default=str)


# ── Reflection (Phase 10) ─────────────────────────────────────


@mcp.tool()
async def browser_reflect(goal: str = "", tab_id: str = "") -> list:
    """Get a comprehensive snapshot of the current page for reasoning.
    Returns a screenshot (as an image) plus page text and metadata.
    Use this to understand the full page state before making decisions.
    goal: optional description of what you're trying to accomplish."""
    # 1. Screenshot
    screenshot_result = await browser_command("screenshot", {"tab_id": tab_id or None})
    # 2. Page text
    text_result_data = await browser_command("get_page_text", {"tab_id": tab_id or None})
    # 3. Page info
    info_result = await browser_command("get_page_info", {"tab_id": tab_id or None})

    blocks = []

    # Add screenshot as Image block
    data_url = screenshot_result.get("image", "")
    if data_url:
        if data_url.startswith("data:"):
            header, b64 = data_url.split(",", 1)
            fmt = "jpeg" if "jpeg" in header else "png"
        else:
            b64 = data_url
            fmt = "jpeg"
        raw_bytes = base64.b64decode(b64)
        blocks.append(Image(data=raw_bytes, format=fmt))

    # Add text summary
    summary = f"URL: {info_result.get('url', '?')}\n"
    summary += f"Title: {info_result.get('title', '?')}\n"
    summary += f"Loading: {info_result.get('loading', False)}\n"
    if goal:
        summary += f"\nGoal: {goal}\n"
    page_text = (text_result_data.get("text") or "")[:50000]
    summary += f"\n--- Page Text (first 50K chars) ---\n{page_text}"
    blocks.append(summary)

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
    return text_result(await browser_command("file_upload", params))


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
    """Close session, destroying all tabs and the workspace.
    Closes all tabs owned by this session. The shared ZenLeap AI workspace
    is never destroyed."""
    return text_result(await browser_command("session_close"))


@mcp.tool()
async def browser_list_sessions() -> str:
    """List all active browser sessions (admin/debug)."""
    return text_result(await browser_command("list_sessions"))


# ── Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
