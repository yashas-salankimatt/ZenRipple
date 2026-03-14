#!/usr/bin/env python3
"""ZenRipple MCP Server — thin wrapper around the zenripple CLI.

Each MCP tool shells out to `zenripple <command>` with the appropriate args.
The session ID is tracked per MCP instance and passed via ZENRIPPLE_SESSION_ID env var.
Tool docstrings include the equivalent CLI command for direct use by agents.
"""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from shlex import quote as shlex_quote

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

mcp = FastMCP(
    "zenripple-browser",
    instructions=(
        "Full browser control for Zen Browser — navigate pages, click elements, fill forms, "
        "take screenshots, read content, execute JavaScript, and more. "
        "All tab operations are scoped to the 'ZenRipple' workspace.\n\n"
        "Each tool wraps the `zenripple` CLI. Agents can also call the CLI directly in bash "
        "for composable multi-step workflows. The CLI command is shown in each tool's description."
    ),
)

# ── CLI Runner ─────────────────────────────────────────────────

CLI_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zenripple_cli.py")

_session_id: str = os.environ.get("ZENRIPPLE_SESSION_ID", "")
_session_initialized: bool = bool(_session_id)
_session_lock: asyncio.Lock | None = None  # Lazy init (no running loop at import time)


def _get_session_lock() -> asyncio.Lock:
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


async def _run_cli(*args: str, timeout: float = 120) -> str:
    """Run a zenripple CLI command and return stdout."""
    env = {**os.environ}
    if _session_id:
        env["ZENRIPPLE_SESSION_ID"] = _session_id

    proc = await asyncio.create_subprocess_exec(
        sys.executable, CLI_SCRIPT, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise Exception(f"zenripple {args[0] if args else ''} timed out after {timeout}s")

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        if not error_msg:
            error_msg = f"zenripple exited with code {proc.returncode}"
        raise Exception(error_msg)

    output = stdout.decode()

    # Inject undelivered human→agent messages into tool output
    if _session_id and args and args[0] not in ("approve", "notify", "ping", "session"):
        human_prefix = _collect_human_messages()
        if human_prefix:
            output = human_prefix + output

    return output


def _collect_human_messages() -> str:
    """Check for undelivered human→agent messages and return them as a prefix string.

    Uses append-only delivery tracking: appends {"delivered": msg_id} lines instead
    of rewriting the file, so browser-side appends are never lost to TOCTOU races.
    The read-and-mark is done under a single lock to prevent duplicate delivery.
    """
    if not _session_id:
        return ""
    import re as _re
    safe_id = _re.sub(r"[^a-zA-Z0-9_-]", "", _session_id)
    if not safe_id:
        return ""

    import tempfile as _tmp
    try:
        import fcntl as _fcntl
    except ImportError:
        _fcntl = None

    replay_dir = os.path.join(_tmp.gettempdir(), f"zenripple_replay_{safe_id}")
    messages_path = os.path.join(replay_dir, "messages.jsonl")

    if not os.path.exists(messages_path):
        return ""

    lock_path = messages_path + ".lock"
    undelivered = []

    def _read_and_mark():
        # Collect all human→agent messages and all delivery records
        delivered_ids = set()
        human_messages = []
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
                    except (json.JSONDecodeError, KeyError):
                        continue
        except (FileNotFoundError, OSError):
            return

        # Find undelivered messages
        for msg in human_messages:
            if msg.get("id") and msg["id"] not in delivered_ids:
                undelivered.append(msg)

        # Append delivery records (append-only, no rewrite)
        if undelivered:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            with open(messages_path, "a") as f:
                for msg in undelivered:
                    f.write(json.dumps({"delivered": msg["id"], "at": now}) + "\n")

    try:
        if _fcntl:
            with open(lock_path, "a") as lock_f:
                _fcntl.flock(lock_f, _fcntl.LOCK_EX)
                try:
                    _read_and_mark()
                finally:
                    _fcntl.flock(lock_f, _fcntl.LOCK_UN)
        else:
            _read_and_mark()
    except OSError:
        return ""

    if not undelivered:
        return ""

    # Format as prefix
    parts = []
    for msg in undelivered:
        ts = msg.get("timestamp", "")
        time_str = ts
        if "T" in ts:
            time_str = ts.split("T")[1][:8]
        parts.append(f"[HUMAN_MESSAGE at {time_str}] {msg.get('text', '')}")
    return "\n".join(parts) + "\n---\n"


async def _ensure_session():
    """Ensure we have a session ID by running ping if needed.

    Sets _session_initialized = True even on failure to avoid hammering a
    dead browser on every subsequent tool call. The CLI will create its own
    session on each invocation if no ZENRIPPLE_SESSION_ID is provided.
    """
    global _session_id, _session_initialized
    if _session_initialized:
        return
    async with _get_session_lock():
        if _session_initialized:  # Double-check after acquiring lock
            return
        try:
            output = await _run_cli("ping")
            data = json.loads(output)
            sid = data.get("session_id", "")
            if sid:
                _session_id = sid
        except Exception as e:
            import sys
            print(f"zenripple: session init failed: {e}", file=sys.stderr)
        _session_initialized = True


async def _cmd(*args: str, timeout: float = 120) -> str:
    """Ensure session, then run CLI command and return stdout."""
    await _ensure_session()
    return await _run_cli(*args, timeout=timeout)


def _tab_args(tab_id: str = "", frame_id: int = 0) -> list[str]:
    """Build --tab-id and --frame-id args if non-default."""
    args = []
    if tab_id:
        args += ["--tab-id", tab_id]
    if frame_id:
        args += ["--frame-id", str(frame_id)]
    return args


# ── Tab Management ──────────────────────────────────────────────


@mcp.tool()
async def browser_create_tab(url: str = "about:blank", persist: bool = True) -> str:
    """Create a new browser tab in the ZenRipple workspace and navigate to a URL.
    Set persist=true to keep the tab alive after session close.

    CLI: zenripple create-tab [url] [--persist false]"""
    args = ["create-tab", url]
    if not persist:
        args += ["--persist", "false"]
    return await _cmd(*args)


@mcp.tool()
async def browser_close_tab(tab_id: str = "") -> str:
    """Close a browser tab. If no tab_id, closes the active tab.

    CLI: zenripple close-tab [tab_id]"""
    args = ["close-tab"]
    if tab_id:
        args.append(tab_id)
    return await _cmd(*args)


@mcp.tool()
async def browser_switch_tab(tab_id: str) -> str:
    """Switch to a different tab in the ZenRipple workspace.

    CLI: zenripple switch-tab <tab_id>"""
    return await _cmd("switch-tab", tab_id)


@mcp.tool()
async def browser_list_tabs() -> str:
    """List all open tabs in the ZenRipple workspace with IDs, titles, and URLs.

    CLI: zenripple list-tabs"""
    return await _cmd("list-tabs")


# ── Navigation ──────────────────────────────────────────────────


@mcp.tool()
async def browser_navigate(url: str, tab_id: str = "") -> str:
    """Navigate a tab to a URL. If no tab_id, navigates the active tab.

    CLI: zenripple nav <url> [--tab-id ID]"""
    return await _cmd("nav", url, *_tab_args(tab_id))


@mcp.tool()
async def browser_go_back(tab_id: str = "") -> str:
    """Navigate back in a tab's history.

    CLI: zenripple back [--tab-id ID]"""
    return await _cmd("back", *_tab_args(tab_id))


@mcp.tool()
async def browser_go_forward(tab_id: str = "") -> str:
    """Navigate forward in a tab's history.

    CLI: zenripple forward [--tab-id ID]"""
    return await _cmd("forward", *_tab_args(tab_id))


@mcp.tool()
async def browser_reload(tab_id: str = "") -> str:
    """Reload a tab.

    CLI: zenripple reload [--tab-id ID]"""
    return await _cmd("reload", *_tab_args(tab_id))


# ── Tab Events ──────────────────────────────────────────────────


@mcp.tool()
async def browser_get_tab_events() -> str:
    """Get and drain the queue of tab open/close events since the last call.
    Useful for detecting popups, new tabs opened by links (target=_blank), etc.

    CLI: zenripple tab-events"""
    return await _cmd("tab-events")


# ── Dialogs ─────────────────────────────────────────────────────


@mcp.tool()
async def browser_get_dialogs() -> str:
    """Get any pending alert/confirm/prompt dialogs.

    CLI: zenripple dialogs"""
    return await _cmd("dialogs")


@mcp.tool()
async def browser_handle_dialog(action: str, text: str = "") -> str:
    """Handle (accept or dismiss) the oldest pending dialog.
    action: 'accept' or 'dismiss'. text: optional text for prompt dialogs.

    CLI: zenripple handle-dialog --action accept [--text "..."]"""
    args = ["handle-dialog", "--action", action]
    if text:
        args += ["--text", text]
    return await _cmd(*args)


# ── Popup Blocked ──────────────────────────────────────────────


@mcp.tool()
async def browser_get_popup_blocked_events() -> str:
    """Get and drain the queue of popup-blocked events.

    CLI: zenripple popup-events"""
    return await _cmd("popup-events")


@mcp.tool()
async def browser_allow_blocked_popup(tab_id: str = "", index: int = -1) -> str:
    """Allow blocked popups for a tab, opening them as new tabs.

    CLI: zenripple popup-allow [--tab-id ID] [--index N]"""
    args = ["popup-allow"]
    if tab_id:
        args += ["--tab-id", tab_id]
    if index >= 0:
        args += ["--index", str(index)]
    return await _cmd(*args)


# ── Navigation Status ───────────────────────────────────────────


@mcp.tool()
async def browser_get_navigation_status(tab_id: str = "") -> str:
    """Get the HTTP status and error code for the last navigation.

    CLI: zenripple nav-status [--tab-id ID]"""
    return await _cmd("nav-status", *_tab_args(tab_id))


# ── Frames ──────────────────────────────────────────────────────


@mcp.tool()
async def browser_list_frames(tab_id: str = "") -> str:
    """List all frames (iframes) in a tab.

    CLI: zenripple frames [--tab-id ID]"""
    return await _cmd("frames", *_tab_args(tab_id))


# ── Observation ─────────────────────────────────────────────────


@mcp.tool()
async def browser_get_page_info(tab_id: str = "") -> str:
    """Get info about a tab: URL, title, loading state, navigation history.

    CLI: zenripple info [--tab-id ID]"""
    return await _cmd("info", *_tab_args(tab_id))


@mcp.tool()
async def browser_screenshot(tab_id: str = "") -> list:
    """Take a screenshot of a browser tab. Returns the image and viewport dimensions.

    CLI: zenripple screenshot [--tab-id ID]"""
    args = ["screenshot"]
    if tab_id:
        args += ["--tab-id", tab_id]
    output = await _cmd(*args)
    data = json.loads(output)
    path = data.get("saved", "")
    if not path:
        raise Exception("Screenshot returned no file path")
    p = Path(path)
    raw = p.read_bytes()
    try:
        p.unlink()
    except OSError:
        pass
    blocks: list = [Image(data=raw, format="jpeg")]
    dims = data.get("dimensions", "")
    if dims:
        blocks.append(f"Screenshot: {dims}")
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
    Use these indices with click/fill tools.
    viewport_only: only elements in current viewport.
    max_elements: limit count (0 = unlimited).
    incremental: return diff against previous call.

    CLI: zenripple dom [--viewport-only] [--max-elements N] [--incremental] [--tab-id ID] [--frame-id N]"""
    args = ["dom"]
    if viewport_only:
        args.append("--viewport-only")
    if max_elements:
        args += ["--max-elements", str(max_elements)]
    if incremental:
        args.append("--incremental")
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


@mcp.tool()
async def browser_get_page_text(tab_id: str = "", frame_id: int = 0) -> str:
    """Get the full visible text content of the current page or a specific iframe.

    CLI: zenripple text [--tab-id ID] [--frame-id N]"""
    return await _cmd("text", *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_get_page_html(tab_id: str = "", frame_id: int = 0) -> str:
    """Get the full HTML source of the current page or a specific iframe.

    CLI: zenripple html [--tab-id ID] [--frame-id N]"""
    return await _cmd("html", *_tab_args(tab_id, frame_id))


# ── Compact DOM / Accessibility ─────────────────────────────────


@mcp.tool()
async def browser_get_elements_compact(
    tab_id: str = "",
    frame_id: int = 0,
    viewport_only: bool = False,
    max_elements: int = 0,
) -> str:
    """Get a compact, token-efficient representation of interactive elements.
    5-10x fewer tokens than browser_get_dom.

    CLI: zenripple elements [--viewport-only] [--max-elements N] [--tab-id ID] [--frame-id N]"""
    args = ["elements"]
    if viewport_only:
        args.append("--viewport-only")
    if max_elements:
        args += ["--max-elements", str(max_elements)]
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


@mcp.tool()
async def browser_get_accessibility_tree(tab_id: str = "", frame_id: int = 0) -> str:
    """Get the accessibility tree for the current page.

    CLI: zenripple a11y [--tab-id ID] [--frame-id N]"""
    return await _cmd("a11y", *_tab_args(tab_id, frame_id))


# ── Interaction ────────────────────────────────────────────────


@mcp.tool()
async def browser_click(index: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Click an interactive element by its index from browser_get_dom.

    CLI: zenripple click <index> [--tab-id ID] [--frame-id N]"""
    return await _cmd("click", str(index), *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_click_coordinates(x: int, y: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Click at specific x,y coordinates on the page.

    CLI: zenripple click-xy <x> <y> [--tab-id ID] [--frame-id N]"""
    return await _cmd("click-xy", str(x), str(y), *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_grounded_click(description: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Click on a page element described in natural language using VLM grounding.
    Takes a screenshot, sends it to a grounding VLM which returns pixel coordinates,
    then clicks at that position.

    CLI: zenripple gclick "<description>" [--tab-id ID]"""
    args = ["gclick", description]
    if tab_id:
        args += ["--tab-id", tab_id]
    return await _cmd(*args, timeout=60)


@mcp.tool()
async def browser_fill(index: int, value: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Fill a form field with a value by its index from browser_get_dom.

    CLI: zenripple fill <index> <value> [--tab-id ID] [--frame-id N]"""
    return await _cmd("fill", str(index), value, *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_select_option(index: int, value: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Select an option in a <select> dropdown by its index.

    CLI: zenripple select <index> <value> [--tab-id ID] [--frame-id N]"""
    return await _cmd("select", str(index), value, *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_type(text: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Type text character-by-character into the currently focused element.

    CLI: zenripple type <text> [--tab-id ID] [--frame-id N]"""
    return await _cmd("type", text, *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_press_key(
    key: str, ctrl: bool = False, shift: bool = False,
    alt: bool = False, meta: bool = False,
    tab_id: str = "", frame_id: int = 0,
) -> str:
    """Press a keyboard key with optional modifiers.

    CLI: zenripple key <key> [--ctrl] [--shift] [--alt] [--meta] [--tab-id ID] [--frame-id N]"""
    args = ["key", key]
    if ctrl:
        args.append("--ctrl")
    if shift:
        args.append("--shift")
    if alt:
        args.append("--alt")
    if meta:
        args.append("--meta")
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


@mcp.tool()
async def browser_scroll(
    direction: str = "down", amount: int = 500,
    tab_id: str = "", frame_id: int = 0,
) -> str:
    """Scroll the page in a direction (up/down/left/right) by a pixel amount.

    CLI: zenripple scroll [direction] [amount] [--tab-id ID] [--frame-id N]"""
    return await _cmd("scroll", direction, str(amount), *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_hover(index: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Hover over an interactive element by its index.

    CLI: zenripple hover <index> [--tab-id ID] [--frame-id N]"""
    return await _cmd("hover", str(index), *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_hover_coordinates(x: int, y: int, tab_id: str = "", frame_id: int = 0) -> str:
    """Hover at specific x,y coordinates on the page.

    CLI: zenripple hover-xy <x> <y> [--tab-id ID] [--frame-id N]"""
    return await _cmd("hover-xy", str(x), str(y), *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_grounded_hover(description: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Hover on a page element described in natural language using VLM grounding.

    CLI: zenripple ghover "<description>" [--tab-id ID]"""
    args = ["ghover", description]
    if tab_id:
        args += ["--tab-id", tab_id]
    return await _cmd(*args, timeout=60)


@mcp.tool()
async def browser_scroll_at_point(
    x: int, y: int, direction: str = "down", amount: int = 500,
    tab_id: str = "", frame_id: int = 0,
) -> str:
    """Scroll at specific x,y coordinates using native wheel events.
    Scrolls whatever scrollable container is under the given coordinates.

    CLI: zenripple scroll-xy <x> <y> <direction> <amount> [--tab-id ID] [--frame-id N]"""
    return await _cmd(
        "scroll-xy", str(x), str(y), direction, str(amount),
        *_tab_args(tab_id, frame_id),
    )


@mcp.tool()
async def browser_grounded_scroll(
    description: str, direction: str = "down", amount: int = 500,
    tab_id: str = "", frame_id: int = 0,
) -> str:
    """Scroll at a page element described in natural language using VLM grounding.

    CLI: zenripple gscroll "<description>" [direction] [amount] [--tab-id ID]"""
    args = ["gscroll", description, direction, str(amount)]
    if tab_id:
        args += ["--tab-id", tab_id]
    return await _cmd(*args, timeout=60)


# ── Console / Eval ─────────────────────────────────────────────


@mcp.tool()
async def browser_console_setup(tab_id: str = "", frame_id: int = 0) -> str:
    """Start capturing console output on a tab. Must call before console_logs/errors.

    CLI: zenripple console-setup [--tab-id ID] [--frame-id N]"""
    return await _cmd("console-setup", *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_console_teardown(tab_id: str = "", frame_id: int = 0) -> str:
    """Stop console capture and remove listeners.

    CLI: zenripple console-teardown [--tab-id ID] [--frame-id N]"""
    return await _cmd("console-teardown", *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_console_logs(tab_id: str = "", frame_id: int = 0) -> str:
    """Get captured console messages. Call browser_console_setup first.

    CLI: zenripple logs [--tab-id ID] [--frame-id N]"""
    return await _cmd("logs", *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_console_errors(tab_id: str = "", frame_id: int = 0) -> str:
    """Get captured errors: console.error, uncaught exceptions, unhandled rejections.

    CLI: zenripple errors [--tab-id ID] [--frame-id N]"""
    return await _cmd("errors", *_tab_args(tab_id, frame_id))


@mcp.tool()
async def browser_console_eval(expression: str, tab_id: str = "", frame_id: int = 0) -> str:
    """Execute JavaScript in the page's global scope.

    CLI: zenripple eval "<expression>" [--tab-id ID] [--frame-id N]"""
    return await _cmd("eval", expression, *_tab_args(tab_id, frame_id))


# ── Clipboard ───────────────────────────────────────────────────


@mcp.tool()
async def browser_clipboard_read() -> str:
    """Read the current text content from the system clipboard.

    CLI: zenripple clip-read"""
    return await _cmd("clip-read")


@mcp.tool()
async def browser_clipboard_write(text: str) -> str:
    """Write text to the system clipboard.

    CLI: zenripple clip-write <text>"""
    return await _cmd("clip-write", text)


# ── Control ─────────────────────────────────────────────────────


@mcp.tool()
async def browser_wait(seconds: float = 2.0) -> str:
    """Wait for a specified number of seconds.

    CLI: zenripple wait [seconds]"""
    return await _cmd("wait", str(seconds))


@mcp.tool()
async def browser_wait_for_element(
    selector: str, tab_id: str = "", frame_id: int = 0, timeout: int = 10,
) -> str:
    """Wait for a CSS selector to match an element on the page.

    CLI: zenripple wait-el <selector> [--timeout N] [--tab-id ID] [--frame-id N]"""
    args = ["wait-el", selector]
    if timeout != 10:
        args += ["--timeout", str(timeout)]
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args, timeout=timeout + 5)


@mcp.tool()
async def browser_wait_for_text(
    text: str, tab_id: str = "", frame_id: int = 0, timeout: int = 10,
) -> str:
    """Wait for specific text to appear on the page.

    CLI: zenripple wait-text <text> [--timeout N] [--tab-id ID] [--frame-id N]"""
    args = ["wait-text", text]
    if timeout != 10:
        args += ["--timeout", str(timeout)]
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args, timeout=timeout + 5)


@mcp.tool()
async def browser_wait_for_load(tab_id: str = "", timeout: int = 15) -> str:
    """Wait for the current page to finish loading.

    CLI: zenripple wait-load [--timeout N] [--tab-id ID]"""
    args = ["wait-load"]
    if timeout != 15:
        args += ["--timeout", str(timeout)]
    if tab_id:
        args += ["--tab-id", tab_id]
    return await _cmd(*args, timeout=timeout + 5)


@mcp.tool()
async def browser_wait_for_download(timeout: int = 60, save_to: str = "") -> str:
    """Wait for the next file download to complete.

    CLI: zenripple download [timeout] [--save-to PATH]"""
    args = ["download", str(timeout)]
    if save_to:
        args += ["--save-to", save_to]
    return await _cmd(*args, timeout=timeout + 10)


@mcp.tool()
async def browser_save_screenshot(file_path: str, tab_id: str = "") -> str:
    """Take a screenshot and save it as an image file.

    CLI: zenripple save-screenshot <path> [--tab-id ID]"""
    args = ["save-screenshot", file_path]
    if tab_id:
        args += ["--tab-id", tab_id]
    return await _cmd(*args)


# ── Cookies ─────────────────────────────────────────────────────


@mcp.tool()
async def browser_get_cookies(url: str = "", name: str = "", tab_id: str = "") -> str:
    """Get cookies for the current tab's domain or a specific URL.

    CLI: zenripple cookies [url] [name] [--tab-id ID]"""
    args = ["cookies"]
    if url:
        args.append(url)
        if name:
            args.append(name)
    elif name:
        args += ["--name", name]
    if tab_id:
        args += ["--tab-id", tab_id]
    return await _cmd(*args)


@mcp.tool()
async def browser_set_cookie(
    name: str, value: str = "", path: str = "/",
    secure: bool = False, httpOnly: bool = False,
    sameSite: str = "", expires: str = "",
    tab_id: str = "", frame_id: int = 0,
) -> str:
    """Set a cookie on the current page via document.cookie.

    CLI: zenripple set-cookie <name> <value> [--path /] [--secure] [--httpOnly] [--sameSite Lax] [--expires ISO]"""
    params = {"name": name, "value": value, "path": path}
    if secure:
        params["secure"] = True
    if httpOnly:
        params["httpOnly"] = True
    if sameSite:
        params["sameSite"] = sameSite
    if expires:
        params["expires"] = expires
    if tab_id:
        params["tab_id"] = tab_id
    if frame_id:
        params["frame_id"] = frame_id
    return await _cmd("set-cookie", "-j", json.dumps(params))


@mcp.tool()
async def browser_delete_cookies(url: str = "", name: str = "", tab_id: str = "") -> str:
    """Delete cookies for the current tab's domain.

    CLI: zenripple delete-cookies [url] [name] [--tab-id ID]"""
    args = ["delete-cookies"]
    if url:
        args.append(url)
        if name:
            args.append(name)
    elif name:
        args += ["--name", name]
    if tab_id:
        args += ["--tab-id", tab_id]
    return await _cmd(*args)


# ── Storage ────────────────────────────────────────────────────


@mcp.tool()
async def browser_get_storage(
    storage_type: str, key: str = "", tab_id: str = "", frame_id: int = 0,
) -> str:
    """Get localStorage or sessionStorage data.
    storage_type: 'localStorage' or 'sessionStorage'.

    CLI: zenripple storage <storage_type> [key] [--tab-id ID] [--frame-id N]"""
    args = ["storage", storage_type]
    if key:
        args.append(key)
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


@mcp.tool()
async def browser_set_storage(
    storage_type: str, key: str, value: str,
    tab_id: str = "", frame_id: int = 0,
) -> str:
    """Set a key-value pair in localStorage or sessionStorage.

    CLI: zenripple set-storage <storage_type> <key> <value> [--tab-id ID] [--frame-id N]"""
    return await _cmd(
        "set-storage", storage_type, key, value,
        *_tab_args(tab_id, frame_id),
    )


@mcp.tool()
async def browser_delete_storage(
    storage_type: str, key: str = "", tab_id: str = "", frame_id: int = 0,
) -> str:
    """Delete a key from localStorage/sessionStorage, or clear all.

    CLI: zenripple delete-storage <storage_type> [key] [--tab-id ID] [--frame-id N]"""
    args = ["delete-storage", storage_type]
    if key:
        args.append(key)
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


# ── Network Monitoring ──────────────────────────────────────────


@mcp.tool()
async def browser_network_monitor_start() -> str:
    """Start monitoring network requests.

    CLI: zenripple net-start"""
    return await _cmd("net-start")


@mcp.tool()
async def browser_network_monitor_stop() -> str:
    """Stop monitoring network requests.

    CLI: zenripple net-stop"""
    return await _cmd("net-stop")


@mcp.tool()
async def browser_network_get_log(
    url_filter: str = "", method_filter: str = "",
    status_filter: int = 0, limit: int = 50,
) -> str:
    """Get captured network log entries.

    CLI: zenripple net-log [--url-filter REGEX] [--method-filter GET] [--status-filter 404] [--limit 50]"""
    args = ["net-log"]
    if url_filter:
        args += ["--url-filter", url_filter]
    if method_filter:
        args += ["--method-filter", method_filter]
    if status_filter:
        args += ["--status-filter", str(status_filter)]
    if limit != 50:
        args += ["--limit", str(limit)]
    return await _cmd(*args)


# ── Request Interception ────────────────────────────────────────


@mcp.tool()
async def browser_intercept_add_rule(pattern: str, action: str, headers: str = "") -> str:
    """Add a network interception rule.
    pattern: regex to match URLs. action: 'block' or 'modify_headers'.

    CLI: zenripple intercept-add <pattern> <action> [--headers '{"..."}']"""
    args = ["intercept-add", pattern, action]
    if headers:
        args += ["--headers", headers]
    return await _cmd(*args)


@mcp.tool()
async def browser_intercept_remove_rule(rule_id: int) -> str:
    """Remove a network interception rule by its ID.

    CLI: zenripple intercept-remove <rule_id>"""
    return await _cmd("intercept-remove", str(rule_id))


@mcp.tool()
async def browser_intercept_list_rules() -> str:
    """List all active network interception rules.

    CLI: zenripple intercept-list"""
    return await _cmd("intercept-list")


# ── Session Persistence ─────────────────────────────────────────


@mcp.tool()
async def browser_session_save(file_path: str) -> str:
    """Save the current browser session (tabs + cookies) to a JSON file.

    CLI: zenripple session-save <file_path>"""
    return await _cmd("session-save", file_path)


@mcp.tool()
async def browser_session_restore(file_path: str) -> str:
    """Restore a previously saved browser session from a JSON file.

    CLI: zenripple session-restore <file_path>"""
    return await _cmd("session-restore", file_path)


# ── Multi-Tab Coordination ──────────────────────────────────────


@mcp.tool()
async def browser_compare_tabs(tab_ids: str) -> str:
    """Compare content across multiple tabs. Pass comma-separated tab IDs.

    CLI: zenripple compare <tab_id1,tab_id2,...>"""
    return await _cmd("compare", tab_ids)


@mcp.tool()
async def browser_batch_navigate(urls: str, persist: bool = True) -> str:
    """Open multiple URLs in new tabs at once. Pass comma-separated URLs.

    CLI: zenripple batch-nav <url1> <url2> ... [--persist false]"""
    args = ["batch-nav"] + [u.strip() for u in urls.split(",") if u.strip()]
    if not persist:
        args += ["--persist", "false"]
    return await _cmd(*args)


# ── Visual Grounding ────────────────────────────────────────────


@mcp.tool()
async def browser_find_element_by_description(
    description: str, tab_id: str = "", frame_id: int = 0,
) -> str:
    """Find interactive elements matching a natural language description.
    Returns top 5 candidates with indices.

    CLI: zenripple find "<description>" [--tab-id ID] [--frame-id N]"""
    args = ["find", description]
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


# ── Action Recording ────────────────────────────────────────────


@mcp.tool()
async def browser_record_start() -> str:
    """Start recording browser actions.

    CLI: zenripple record-start"""
    return await _cmd("record-start")


@mcp.tool()
async def browser_record_stop() -> str:
    """Stop recording browser actions.

    CLI: zenripple record-stop"""
    return await _cmd("record-stop")


@mcp.tool()
async def browser_record_save(file_path: str) -> str:
    """Save recorded actions to a JSON file.

    CLI: zenripple record-save <file_path>"""
    return await _cmd("record-save", file_path)


@mcp.tool()
async def browser_record_replay(file_path: str, delay: float = 0.5) -> str:
    """Replay recorded actions from a JSON file.

    CLI: zenripple record-replay -j '{"file_path": "...", "delay": 0.5}'"""
    return await _cmd("record-replay", "-j", json.dumps({"file_path": file_path, "delay": delay}))


# ── Session Replay Status ──────────────────────────────────────


@mcp.tool()
async def browser_replay_status() -> str:
    """Get tool call logging status: count, directory, session ID.

    CLI: zenripple replay-status"""
    return await _cmd("replay-status")


# ── Drag-and-Drop ───────────────────────────────────────────────


@mcp.tool()
async def browser_drag(
    source_index: int, target_index: int, steps: int = 10,
    tab_id: str = "", frame_id: int = 0,
) -> str:
    """Drag an element to another element by their indices.

    CLI: zenripple drag <sourceIndex> <targetIndex> [--steps 10] [--tab-id ID] [--frame-id N]"""
    args = ["drag", str(source_index), str(target_index)]
    if steps != 10:
        args += ["--steps", str(steps)]
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


@mcp.tool()
async def browser_drag_coordinates(
    start_x: int, start_y: int, end_x: int, end_y: int,
    steps: int = 10, tab_id: str = "", frame_id: int = 0,
) -> str:
    """Drag from one coordinate to another.

    CLI: zenripple drag-xy <startX> <startY> <endX> <endY> [--steps 10] [--tab-id ID]"""
    args = ["drag-xy", str(start_x), str(start_y), str(end_x), str(end_y)]
    if steps != 10:
        args += ["--steps", str(steps)]
    args += _tab_args(tab_id, frame_id)
    return await _cmd(*args)


# ── Chrome-Context Eval ─────────────────────────────────────────


@mcp.tool()
async def browser_eval_chrome(expression: str) -> str:
    """Execute JavaScript in the browser's chrome (privileged) context.
    Has access to Services, gBrowser, Cc, Ci, Cu, IOUtils.

    CLI: zenripple eval-chrome "<expression>" """
    return await _cmd("eval-chrome", expression)


# ── Reflection ─────────────────────────────────────────────────


@mcp.tool()
async def browser_reflect(goal: str = "", tab_id: str = "") -> list:
    """Get a comprehensive snapshot: screenshot + page text + metadata.

    CLI: zenripple reflect [--goal TEXT] [--tab-id ID]"""
    args = ["reflect"]
    if goal:
        args += ["--goal", goal]
    if tab_id:
        args += ["--tab-id", tab_id]
    output = await _cmd(*args)
    data = json.loads(output)

    blocks: list = []

    # Screenshot
    ss_path = data.get("screenshot_path", "")
    if ss_path:
        try:
            p = Path(ss_path)
            raw = p.read_bytes()
            blocks.append(Image(data=raw, format="jpeg"))
            try:
                p.unlink()
            except OSError:
                pass
        except Exception as e:
            blocks.append(f"Screenshot error: {e}")

    # Text summary
    summary = f"URL: {data.get('url', '?')}\n"
    summary += f"Title: {data.get('title', '?')}\n"
    summary += f"Loading: {data.get('loading', False)}\n"
    if goal:
        summary += f"\nGoal: {goal}\n"
    page_text = data.get("page_text", "")
    if page_text:
        summary += f"\n--- Page Text (first 50K chars) ---\n{page_text}"
    if data.get("notifications"):
        summary += f"\n{data['notifications']}"
    blocks.append(summary)

    return blocks


# ── File Upload ─────────────────────────────────────────────────


@mcp.tool()
async def browser_file_upload(
    file_path: str, index: int, tab_id: str = "", frame_id: int = 0,
) -> str:
    """Upload a file to an <input type="file"> element by its index.

    CLI: zenripple upload <file_path> <index> [--tab-id ID] [--frame-id N]"""
    return await _cmd("upload", file_path, str(index), *_tab_args(tab_id, frame_id))


# ── Session Management ──────────────────────────────────────────


@mcp.tool()
async def browser_session_info() -> str:
    """Get current session info: session_id, workspace, connections, tabs.

    CLI: zenripple session info"""
    return await _cmd("session", "info")


@mcp.tool()
async def browser_session_close() -> str:
    """Close session. Created tabs are closed; claimed tabs are released.

    CLI: zenripple session close"""
    global _session_id, _session_initialized
    result = await _cmd("session", "close")
    async with _get_session_lock():
        _session_id = ""
        _session_initialized = False
    return result


@mcp.tool()
async def browser_list_sessions() -> str:
    """List all active browser sessions.

    CLI: zenripple session list"""
    return await _cmd("session", "list")


@mcp.tool()
async def browser_set_session_name(name: str) -> str:
    """Set a human-readable name for the current session.

    CLI: zenripple session name <name>"""
    return await _cmd("session", "name", name)


# ── Tab Claiming ────────────────────────────────────────────────


@mcp.tool()
async def browser_list_workspace_tabs() -> str:
    """List ALL tabs in the ZenRipple workspace, including from other sessions.

    CLI: zenripple workspace-tabs"""
    return await _cmd("workspace-tabs")


@mcp.tool()
async def browser_claim_tab(tab_id: str) -> str:
    """Claim an unclaimed or stale tab into this session.

    CLI: zenripple claim-tab <tab_id>"""
    return await _cmd("claim-tab", tab_id)


# ── Dashboard: Approvals & Messages ──────────────────────────────


@mcp.tool()
async def browser_approve(description: str, tab_id: str = "", timeout: int = 300) -> str:
    """Request human approval before proceeding with a sensitive action.
    Blocks until the human approves or denies (or timeout expires).

    CLI: zenripple approve <description> [--tab-id ID] [--timeout SECONDS]"""
    args = ["approve", description]
    if tab_id:
        args += ["--tab-id", tab_id]
    if timeout != 300:
        args += ["--timeout", str(timeout)]
    return await _cmd(*args, timeout=timeout + 10)


@mcp.tool()
async def browser_notify(message: str) -> str:
    """Send a non-blocking message to the human operator.
    The message appears in the Live Agent Dashboard.

    CLI: zenripple notify <message>"""
    return await _cmd("notify", message)


# ── Health Check ─────────────────────────────────────────────────


@mcp.tool()
async def browser_ping() -> str:
    """Check if the browser agent is alive and responsive.

    CLI: zenripple ping"""
    return await _cmd("ping")


# ── Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
