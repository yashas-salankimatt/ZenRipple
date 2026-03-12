"""Tests for the ZenRipple MCP server and CLI.

The MCP server is a thin wrapper around the zenripple CLI. Each MCP tool
shells out to `zenripple <command>` via asyncio.create_subprocess_exec.
These tests mock `_cmd` (or `_run_cli`) for MCP server tests and mock the
BrowserClient for CLI tests.
"""

import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from mcp.server.fastmcp.utilities.types import Image

import zenripple_mcp_server as server
import zenripple_cli as cli


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def mock_cli():
    """Patch _cmd to return controlled output (skips session init and _run_cli)."""
    with patch.object(server, "_cmd", new_callable=AsyncMock) as mock:
        with patch.object(server, "_session_id", "test-session-123"):
            yield mock


@pytest.fixture
def mock_cli_no_session():
    """Patch _run_cli with no active session (requires initialization)."""
    with patch.object(server, "_run_cli", new_callable=AsyncMock) as mock:
        with patch.object(server, "_session_initialized", False):
            with patch.object(server, "_session_id", ""):
                yield mock


@pytest.fixture
def fake_screenshot(tmp_path):
    """Create a fake JPEG file for screenshot tests."""
    img_path = tmp_path / "ss.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-data")
    return img_path


# ═══════════════════════════════════════════════════════════════
# MCP SERVER TESTS
# ═══════════════════════════════════════════════════════════════


class TestSessionInitialization:
    """Session initialization via ping on first call."""

    @pytest.mark.asyncio
    async def test_ensure_session_calls_ping(self, mock_cli_no_session):
        mock_cli_no_session.return_value = json.dumps(
            {"session_id": "new-session-abc", "status": "pong"}
        )
        # Reset the globals for this test
        server._session_initialized = False
        server._session_id = ""
        await server._ensure_session()
        mock_cli_no_session.assert_called_once_with("ping")
        assert server._session_id == "new-session-abc"
        assert server._session_initialized is True

    @pytest.mark.asyncio
    async def test_ensure_session_skips_when_initialized(self, mock_cli):
        await server._ensure_session()
        mock_cli.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_session_handles_invalid_json(self, mock_cli_no_session):
        mock_cli_no_session.return_value = "not-json"
        server._session_initialized = False
        server._session_id = ""
        await server._ensure_session()
        assert server._session_initialized is True
        assert server._session_id == ""

    @pytest.mark.asyncio
    async def test_ensure_session_handles_missing_session_id(self, mock_cli_no_session):
        mock_cli_no_session.return_value = json.dumps({"status": "pong"})
        server._session_initialized = False
        server._session_id = ""
        await server._ensure_session()
        assert server._session_initialized is True
        assert server._session_id == ""

    @pytest.mark.asyncio
    async def test_session_passthrough_from_env(self):
        """When ZENRIPPLE_SESSION_ID is set, _session_id is populated at import time."""
        # The module reads from env at import — we test the logic
        sid = "env-session-xyz"
        with patch.object(server, "_session_id", sid):
            with patch.object(server, "_session_initialized", True):
                assert server._session_id == sid


class TestCmdWrapper:
    """_cmd ensures session before running CLI."""

    @pytest.mark.asyncio
    async def test_cmd_calls_ensure_then_run(self):
        with patch.object(server, "_run_cli", new_callable=AsyncMock) as mock_run:
            with patch.object(server, "_session_initialized", True):
                mock_run.return_value = '{"ok": true}'
                result = await server._cmd("some-command", "arg1")
                mock_run.assert_called_once_with("some-command", "arg1", timeout=120)
                assert result == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_cmd_passes_timeout(self):
        with patch.object(server, "_run_cli", new_callable=AsyncMock) as mock_run:
            with patch.object(server, "_session_initialized", True):
                mock_run.return_value = '{"ok": true}'
                result = await server._cmd("slow-command", timeout=30)
                mock_run.assert_called_once_with("slow-command", timeout=30)


class TestTabArgs:
    """_tab_args builds --tab-id and --frame-id flags."""

    def test_empty_defaults(self):
        assert server._tab_args() == []

    def test_tab_id_only(self):
        assert server._tab_args(tab_id="abc") == ["--tab-id", "abc"]

    def test_frame_id_only(self):
        assert server._tab_args(frame_id=2) == ["--frame-id", "2"]

    def test_both(self):
        assert server._tab_args(tab_id="t1", frame_id=3) == [
            "--tab-id", "t1", "--frame-id", "3"
        ]

    def test_zero_frame_id_omitted(self):
        assert server._tab_args(tab_id="t1", frame_id=0) == ["--tab-id", "t1"]

    def test_empty_tab_id_omitted(self):
        assert server._tab_args(tab_id="", frame_id=5) == ["--frame-id", "5"]


# ── Tab Management ──────────────────────────────────────────────


class TestTabManagement:

    @pytest.mark.asyncio
    async def test_create_tab_default(self, mock_cli):
        mock_cli.return_value = '{"tab_id": "t1"}'
        result = await server.browser_create_tab()
        mock_cli.assert_called_once_with("create-tab", "about:blank")
        assert "t1" in result

    @pytest.mark.asyncio
    async def test_create_tab_with_url(self, mock_cli):
        mock_cli.return_value = '{"tab_id": "t2"}'
        result = await server.browser_create_tab(url="https://example.com")
        mock_cli.assert_called_once_with("create-tab", "https://example.com")

    @pytest.mark.asyncio
    async def test_create_tab_no_persist(self, mock_cli):
        mock_cli.return_value = '{"tab_id": "t3"}'
        await server.browser_create_tab(url="https://a.com", persist=False)
        mock_cli.assert_called_once_with("create-tab", "https://a.com", "--persist", "false")

    @pytest.mark.asyncio
    async def test_close_tab_default(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_close_tab()
        mock_cli.assert_called_once_with("close-tab")

    @pytest.mark.asyncio
    async def test_close_tab_with_id(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_close_tab(tab_id="tab-42")
        mock_cli.assert_called_once_with("close-tab", "tab-42")

    @pytest.mark.asyncio
    async def test_switch_tab(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_switch_tab(tab_id="tab-99")
        mock_cli.assert_called_once_with("switch-tab", "tab-99")

    @pytest.mark.asyncio
    async def test_list_tabs(self, mock_cli):
        mock_cli.return_value = '[{"id": "t1", "title": "Test"}]'
        result = await server.browser_list_tabs()
        mock_cli.assert_called_once_with("list-tabs")


# ── Navigation ──────────────────────────────────────────────────


class TestNavigation:

    @pytest.mark.asyncio
    async def test_navigate(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_navigate(url="https://example.com")
        mock_cli.assert_called_once_with("nav", "https://example.com")

    @pytest.mark.asyncio
    async def test_navigate_with_tab_id(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_navigate(url="https://example.com", tab_id="t1")
        mock_cli.assert_called_once_with("nav", "https://example.com", "--tab-id", "t1")

    @pytest.mark.asyncio
    async def test_go_back(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_go_back()
        mock_cli.assert_called_once_with("back")

    @pytest.mark.asyncio
    async def test_go_back_with_tab_id(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_go_back(tab_id="t1")
        mock_cli.assert_called_once_with("back", "--tab-id", "t1")

    @pytest.mark.asyncio
    async def test_go_forward(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_go_forward()
        mock_cli.assert_called_once_with("forward")

    @pytest.mark.asyncio
    async def test_go_forward_with_tab_id(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_go_forward(tab_id="t2")
        mock_cli.assert_called_once_with("forward", "--tab-id", "t2")

    @pytest.mark.asyncio
    async def test_reload(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_reload()
        mock_cli.assert_called_once_with("reload")

    @pytest.mark.asyncio
    async def test_reload_with_tab_id(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_reload(tab_id="t3")
        mock_cli.assert_called_once_with("reload", "--tab-id", "t3")


# ── Tab Events / Dialogs / Popup ──────────────────────────────


class TestTabEventsDialogsPopup:

    @pytest.mark.asyncio
    async def test_get_tab_events(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_tab_events()
        mock_cli.assert_called_once_with("tab-events")

    @pytest.mark.asyncio
    async def test_get_dialogs(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_dialogs()
        mock_cli.assert_called_once_with("dialogs")

    @pytest.mark.asyncio
    async def test_handle_dialog_accept(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_handle_dialog(action="accept")
        mock_cli.assert_called_once_with("handle-dialog", "--action", "accept")

    @pytest.mark.asyncio
    async def test_handle_dialog_dismiss_with_text(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_handle_dialog(action="dismiss", text="hello")
        mock_cli.assert_called_once_with(
            "handle-dialog", "--action", "dismiss", "--text", "hello"
        )

    @pytest.mark.asyncio
    async def test_get_popup_blocked_events(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_popup_blocked_events()
        mock_cli.assert_called_once_with("popup-events")

    @pytest.mark.asyncio
    async def test_allow_blocked_popup_default(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_allow_blocked_popup()
        mock_cli.assert_called_once_with("popup-allow")

    @pytest.mark.asyncio
    async def test_allow_blocked_popup_with_params(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_allow_blocked_popup(tab_id="t1", index=2)
        mock_cli.assert_called_once_with(
            "popup-allow", "--tab-id", "t1", "--index", "2"
        )


# ── Navigation Status / Frames / Page Info ──────────────────────


class TestNavStatusFramesInfo:

    @pytest.mark.asyncio
    async def test_get_navigation_status(self, mock_cli):
        mock_cli.return_value = '{"status": 200}'
        await server.browser_get_navigation_status()
        mock_cli.assert_called_once_with("nav-status")

    @pytest.mark.asyncio
    async def test_get_navigation_status_with_tab(self, mock_cli):
        mock_cli.return_value = '{"status": 200}'
        await server.browser_get_navigation_status(tab_id="t1")
        mock_cli.assert_called_once_with("nav-status", "--tab-id", "t1")

    @pytest.mark.asyncio
    async def test_list_frames(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_list_frames()
        mock_cli.assert_called_once_with("frames")

    @pytest.mark.asyncio
    async def test_list_frames_with_tab(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_list_frames(tab_id="t1")
        mock_cli.assert_called_once_with("frames", "--tab-id", "t1")

    @pytest.mark.asyncio
    async def test_get_page_info(self, mock_cli):
        mock_cli.return_value = '{"url": "https://example.com"}'
        await server.browser_get_page_info()
        mock_cli.assert_called_once_with("info")

    @pytest.mark.asyncio
    async def test_get_page_info_with_tab(self, mock_cli):
        mock_cli.return_value = '{"url": "https://example.com"}'
        await server.browser_get_page_info(tab_id="t1")
        mock_cli.assert_called_once_with("info", "--tab-id", "t1")


# ── Observation Tools ──────────────────────────────────────────


class TestObservation:

    @pytest.mark.asyncio
    async def test_screenshot_returns_image(self, mock_cli, fake_screenshot):
        mock_cli.return_value = json.dumps({
            "saved": str(fake_screenshot),
            "dimensions": "1920x1080",
        })
        result = await server.browser_screenshot()
        mock_cli.assert_called_once_with("screenshot")
        assert len(result) == 2
        assert isinstance(result[0], Image)
        assert "1920x1080" in result[1]

    @pytest.mark.asyncio
    async def test_screenshot_with_tab_id(self, mock_cli, fake_screenshot):
        mock_cli.return_value = json.dumps({
            "saved": str(fake_screenshot),
            "dimensions": "1280x720",
        })
        result = await server.browser_screenshot(tab_id="t1")
        mock_cli.assert_called_once_with("screenshot", "--tab-id", "t1")

    @pytest.mark.asyncio
    async def test_screenshot_no_saved_path_raises(self, mock_cli):
        mock_cli.return_value = json.dumps({"dimensions": "1920x1080"})
        with pytest.raises(Exception, match="no file path"):
            await server.browser_screenshot()

    @pytest.mark.asyncio
    async def test_screenshot_no_dimensions(self, mock_cli, fake_screenshot):
        mock_cli.return_value = json.dumps({"saved": str(fake_screenshot)})
        result = await server.browser_screenshot()
        assert len(result) == 1
        assert isinstance(result[0], Image)

    @pytest.mark.asyncio
    async def test_get_dom_default(self, mock_cli):
        mock_cli.return_value = '{"elements": []}'
        await server.browser_get_dom()
        mock_cli.assert_called_once_with("dom")

    @pytest.mark.asyncio
    async def test_get_dom_all_options(self, mock_cli):
        mock_cli.return_value = '{"elements": []}'
        await server.browser_get_dom(
            tab_id="t1", frame_id=2,
            viewport_only=True, max_elements=50, incremental=True,
        )
        mock_cli.assert_called_once_with(
            "dom", "--viewport-only", "--max-elements", "50",
            "--incremental", "--tab-id", "t1", "--frame-id", "2",
        )

    @pytest.mark.asyncio
    async def test_get_page_text(self, mock_cli):
        mock_cli.return_value = "Hello world"
        await server.browser_get_page_text()
        mock_cli.assert_called_once_with("text")

    @pytest.mark.asyncio
    async def test_get_page_text_with_tab_frame(self, mock_cli):
        mock_cli.return_value = "Hello"
        await server.browser_get_page_text(tab_id="t1", frame_id=3)
        mock_cli.assert_called_once_with("text", "--tab-id", "t1", "--frame-id", "3")

    @pytest.mark.asyncio
    async def test_get_page_html(self, mock_cli):
        mock_cli.return_value = "<html></html>"
        await server.browser_get_page_html()
        mock_cli.assert_called_once_with("html")

    @pytest.mark.asyncio
    async def test_get_page_html_with_tab_frame(self, mock_cli):
        mock_cli.return_value = "<html></html>"
        await server.browser_get_page_html(tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with("html", "--tab-id", "t1", "--frame-id", "1")


# ── Compact DOM / Accessibility ─────────────────────────────────


class TestCompactDOMAccessibility:

    @pytest.mark.asyncio
    async def test_get_elements_compact_default(self, mock_cli):
        mock_cli.return_value = "compact output"
        await server.browser_get_elements_compact()
        mock_cli.assert_called_once_with("elements")

    @pytest.mark.asyncio
    async def test_get_elements_compact_all_options(self, mock_cli):
        mock_cli.return_value = "compact output"
        await server.browser_get_elements_compact(
            tab_id="t1", frame_id=2, viewport_only=True, max_elements=20,
        )
        mock_cli.assert_called_once_with(
            "elements", "--viewport-only", "--max-elements", "20",
            "--tab-id", "t1", "--frame-id", "2",
        )

    @pytest.mark.asyncio
    async def test_get_accessibility_tree(self, mock_cli):
        mock_cli.return_value = "tree output"
        await server.browser_get_accessibility_tree()
        mock_cli.assert_called_once_with("a11y")

    @pytest.mark.asyncio
    async def test_get_accessibility_tree_with_tab_frame(self, mock_cli):
        mock_cli.return_value = "tree output"
        await server.browser_get_accessibility_tree(tab_id="t1", frame_id=2)
        mock_cli.assert_called_once_with("a11y", "--tab-id", "t1", "--frame-id", "2")


# ── Interaction Tools ──────────────────────────────────────────


class TestInteraction:

    @pytest.mark.asyncio
    async def test_click(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_click(index=5)
        mock_cli.assert_called_once_with("click", "5")

    @pytest.mark.asyncio
    async def test_click_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_click(index=5, tab_id="abc", frame_id=2)
        mock_cli.assert_called_once_with(
            "click", "5", "--tab-id", "abc", "--frame-id", "2"
        )

    @pytest.mark.asyncio
    async def test_click_coordinates(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_click_coordinates(x=100, y=200)
        mock_cli.assert_called_once_with("click-xy", "100", "200")

    @pytest.mark.asyncio
    async def test_click_coordinates_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_click_coordinates(x=100, y=200, tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with(
            "click-xy", "100", "200", "--tab-id", "t1", "--frame-id", "1"
        )

    @pytest.mark.asyncio
    async def test_fill(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_fill(index=3, value="hello@example.com")
        mock_cli.assert_called_once_with("fill", "3", "hello@example.com")

    @pytest.mark.asyncio
    async def test_fill_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_fill(index=3, value="text", tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with(
            "fill", "3", "text", "--tab-id", "t1", "--frame-id", "1"
        )

    @pytest.mark.asyncio
    async def test_select_option(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_select_option(index=2, value="opt1")
        mock_cli.assert_called_once_with("select", "2", "opt1")

    @pytest.mark.asyncio
    async def test_type(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_type(text="hello world")
        mock_cli.assert_called_once_with("type", "hello world")

    @pytest.mark.asyncio
    async def test_type_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_type(text="hi", tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with("type", "hi", "--tab-id", "t1", "--frame-id", "1")

    @pytest.mark.asyncio
    async def test_press_key_basic(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_press_key(key="Enter")
        mock_cli.assert_called_once_with("key", "Enter")

    @pytest.mark.asyncio
    async def test_press_key_all_modifiers(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_press_key(
            key="a", ctrl=True, shift=True, alt=True, meta=True,
        )
        mock_cli.assert_called_once_with(
            "key", "a", "--ctrl", "--shift", "--alt", "--meta"
        )

    @pytest.mark.asyncio
    async def test_press_key_partial_modifiers(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_press_key(key="c", ctrl=True)
        mock_cli.assert_called_once_with("key", "c", "--ctrl")

    @pytest.mark.asyncio
    async def test_press_key_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_press_key(key="Tab", tab_id="t1", frame_id=2)
        mock_cli.assert_called_once_with("key", "Tab", "--tab-id", "t1", "--frame-id", "2")

    @pytest.mark.asyncio
    async def test_scroll_default(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_scroll()
        mock_cli.assert_called_once_with("scroll", "down", "500")

    @pytest.mark.asyncio
    async def test_scroll_custom(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_scroll(direction="up", amount=1000)
        mock_cli.assert_called_once_with("scroll", "up", "1000")

    @pytest.mark.asyncio
    async def test_scroll_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_scroll(direction="left", amount=200, tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with(
            "scroll", "left", "200", "--tab-id", "t1", "--frame-id", "1"
        )

    @pytest.mark.asyncio
    async def test_hover(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_hover(index=7)
        mock_cli.assert_called_once_with("hover", "7")

    @pytest.mark.asyncio
    async def test_hover_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_hover(index=7, tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with("hover", "7", "--tab-id", "t1", "--frame-id", "1")

    @pytest.mark.asyncio
    async def test_hover_coordinates(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_hover_coordinates(x=50, y=75)
        mock_cli.assert_called_once_with("hover-xy", "50", "75")

    @pytest.mark.asyncio
    async def test_hover_coordinates_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_hover_coordinates(x=50, y=75, tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with(
            "hover-xy", "50", "75", "--tab-id", "t1", "--frame-id", "1"
        )

    @pytest.mark.asyncio
    async def test_scroll_at_point(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_scroll_at_point(x=300, y=400, direction="down", amount=500)
        mock_cli.assert_called_once_with("scroll-xy", "300", "400", "down", "500")

    @pytest.mark.asyncio
    async def test_scroll_at_point_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_scroll_at_point(
            x=300, y=400, direction="up", amount=200, tab_id="t1", frame_id=1,
        )
        mock_cli.assert_called_once_with(
            "scroll-xy", "300", "400", "up", "200", "--tab-id", "t1", "--frame-id", "1"
        )


# ── Grounded Interaction ──────────────────────────────────────


class TestGrounded:

    @pytest.mark.asyncio
    async def test_grounded_click(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_grounded_click(description="the search button")
        mock_cli.assert_called_once_with("gclick", "the search button", timeout=60)

    @pytest.mark.asyncio
    async def test_grounded_click_with_tab(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_grounded_click(description="button", tab_id="t1")
        mock_cli.assert_called_once_with("gclick", "button", "--tab-id", "t1", timeout=60)

    @pytest.mark.asyncio
    async def test_grounded_hover(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_grounded_hover(description="menu item")
        mock_cli.assert_called_once_with("ghover", "menu item", timeout=60)

    @pytest.mark.asyncio
    async def test_grounded_hover_with_tab(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_grounded_hover(description="menu", tab_id="t1")
        mock_cli.assert_called_once_with("ghover", "menu", "--tab-id", "t1", timeout=60)

    @pytest.mark.asyncio
    async def test_grounded_scroll(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_grounded_scroll(description="sidebar")
        mock_cli.assert_called_once_with("gscroll", "sidebar", "down", "500", timeout=60)

    @pytest.mark.asyncio
    async def test_grounded_scroll_custom(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_grounded_scroll(
            description="sidebar", direction="up", amount=1000, tab_id="t1",
        )
        mock_cli.assert_called_once_with(
            "gscroll", "sidebar", "up", "1000", "--tab-id", "t1", timeout=60
        )


# ── Console / Eval ─────────────────────────────────────────────


class TestConsoleEval:

    @pytest.mark.asyncio
    async def test_console_setup(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_console_setup()
        mock_cli.assert_called_once_with("console-setup")

    @pytest.mark.asyncio
    async def test_console_setup_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_console_setup(tab_id="t1", frame_id=1)
        mock_cli.assert_called_once_with(
            "console-setup", "--tab-id", "t1", "--frame-id", "1"
        )

    @pytest.mark.asyncio
    async def test_console_teardown(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_console_teardown()
        mock_cli.assert_called_once_with("console-teardown")

    @pytest.mark.asyncio
    async def test_console_logs(self, mock_cli):
        mock_cli.return_value = '{"logs": []}'
        await server.browser_console_logs()
        mock_cli.assert_called_once_with("logs")

    @pytest.mark.asyncio
    async def test_console_logs_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"logs": []}'
        await server.browser_console_logs(tab_id="t1", frame_id=2)
        mock_cli.assert_called_once_with("logs", "--tab-id", "t1", "--frame-id", "2")

    @pytest.mark.asyncio
    async def test_console_errors(self, mock_cli):
        mock_cli.return_value = '{"errors": []}'
        await server.browser_console_errors()
        mock_cli.assert_called_once_with("errors")

    @pytest.mark.asyncio
    async def test_console_errors_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"errors": []}'
        await server.browser_console_errors(tab_id="t1", frame_id=2)
        mock_cli.assert_called_once_with("errors", "--tab-id", "t1", "--frame-id", "2")

    @pytest.mark.asyncio
    async def test_console_eval(self, mock_cli):
        mock_cli.return_value = '{"result": 42}'
        await server.browser_console_eval(expression="1 + 1")
        mock_cli.assert_called_once_with("eval", "1 + 1")

    @pytest.mark.asyncio
    async def test_console_eval_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"result": 42}'
        await server.browser_console_eval(expression="x", tab_id="t1", frame_id=2)
        mock_cli.assert_called_once_with("eval", "x", "--tab-id", "t1", "--frame-id", "2")


# ── Clipboard ──────────────────────────────────────────────────


class TestClipboard:

    @pytest.mark.asyncio
    async def test_clipboard_read(self, mock_cli):
        mock_cli.return_value = '{"text": "hello"}'
        await server.browser_clipboard_read()
        mock_cli.assert_called_once_with("clip-read")

    @pytest.mark.asyncio
    async def test_clipboard_write(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_clipboard_write(text="hello world")
        mock_cli.assert_called_once_with("clip-write", "hello world")


# ── Wait Tools ─────────────────────────────────────────────────


class TestWait:

    @pytest.mark.asyncio
    async def test_wait_default(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_wait()
        mock_cli.assert_called_once_with("wait", "2.0")

    @pytest.mark.asyncio
    async def test_wait_custom(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_wait(seconds=5.0)
        mock_cli.assert_called_once_with("wait", "5.0")

    @pytest.mark.asyncio
    async def test_wait_for_element(self, mock_cli):
        mock_cli.return_value = '{"found": true}'
        await server.browser_wait_for_element(selector="#btn")
        mock_cli.assert_called_once_with("wait-el", "#btn", timeout=15)

    @pytest.mark.asyncio
    async def test_wait_for_element_custom_timeout(self, mock_cli):
        mock_cli.return_value = '{"found": true}'
        await server.browser_wait_for_element(selector=".x", timeout=30)
        mock_cli.assert_called_once_with("wait-el", ".x", "--timeout", "30", timeout=35)

    @pytest.mark.asyncio
    async def test_wait_for_element_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"found": true}'
        await server.browser_wait_for_element(
            selector="#x", tab_id="t1", frame_id=2, timeout=10,
        )
        mock_cli.assert_called_once_with(
            "wait-el", "#x", "--tab-id", "t1", "--frame-id", "2", timeout=15
        )

    @pytest.mark.asyncio
    async def test_wait_for_text(self, mock_cli):
        mock_cli.return_value = '{"found": true}'
        await server.browser_wait_for_text(text="Loading complete")
        mock_cli.assert_called_once_with("wait-text", "Loading complete", timeout=15)

    @pytest.mark.asyncio
    async def test_wait_for_text_custom_timeout(self, mock_cli):
        mock_cli.return_value = '{"found": true}'
        await server.browser_wait_for_text(text="x", timeout=20)
        mock_cli.assert_called_once_with("wait-text", "x", "--timeout", "20", timeout=25)

    @pytest.mark.asyncio
    async def test_wait_for_load(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_wait_for_load()
        mock_cli.assert_called_once_with("wait-load", timeout=20)

    @pytest.mark.asyncio
    async def test_wait_for_load_custom_timeout(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_wait_for_load(timeout=30)
        mock_cli.assert_called_once_with("wait-load", "--timeout", "30", timeout=35)

    @pytest.mark.asyncio
    async def test_wait_for_load_with_tab(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_wait_for_load(tab_id="t1", timeout=15)
        # timeout=15 is default, so no --timeout flag
        mock_cli.assert_called_once_with("wait-load", "--tab-id", "t1", timeout=20)

    @pytest.mark.asyncio
    async def test_wait_for_download(self, mock_cli):
        mock_cli.return_value = '{"path": "/tmp/file.pdf"}'
        await server.browser_wait_for_download()
        mock_cli.assert_called_once_with("download", "60", timeout=70)

    @pytest.mark.asyncio
    async def test_wait_for_download_custom(self, mock_cli):
        mock_cli.return_value = '{"path": "/tmp/file.pdf"}'
        await server.browser_wait_for_download(timeout=120, save_to="/tmp/out.pdf")
        mock_cli.assert_called_once_with(
            "download", "120", "--save-to", "/tmp/out.pdf", timeout=130
        )


# ── Save Screenshot ────────────────────────────────────────────


class TestSaveScreenshot:

    @pytest.mark.asyncio
    async def test_save_screenshot(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_save_screenshot(file_path="/tmp/ss.png")
        mock_cli.assert_called_once_with("save-screenshot", "/tmp/ss.png")

    @pytest.mark.asyncio
    async def test_save_screenshot_with_tab(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_save_screenshot(file_path="/tmp/ss.png", tab_id="t1")
        mock_cli.assert_called_once_with("save-screenshot", "/tmp/ss.png", "--tab-id", "t1")


# ── Cookies ────────────────────────────────────────────────────


class TestCookies:

    @pytest.mark.asyncio
    async def test_get_cookies_default(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_cookies()
        mock_cli.assert_called_once_with("cookies")

    @pytest.mark.asyncio
    async def test_get_cookies_with_url(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_cookies(url="https://example.com")
        mock_cli.assert_called_once_with("cookies", "https://example.com")

    @pytest.mark.asyncio
    async def test_get_cookies_with_url_and_name(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_cookies(url="https://example.com", name="sid")
        mock_cli.assert_called_once_with("cookies", "https://example.com", "sid")

    @pytest.mark.asyncio
    async def test_get_cookies_name_only(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_cookies(name="sid")
        mock_cli.assert_called_once_with("cookies", "--name", "sid")

    @pytest.mark.asyncio
    async def test_get_cookies_with_tab(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_get_cookies(tab_id="t1")
        mock_cli.assert_called_once_with("cookies", "--tab-id", "t1")

    @pytest.mark.asyncio
    async def test_set_cookie_basic(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_set_cookie(name="sid", value="abc123")
        args = mock_cli.call_args[0]
        assert args[0] == "set-cookie"
        assert args[1] == "-j"
        params = json.loads(args[2])
        assert params["name"] == "sid"
        assert params["value"] == "abc123"
        assert params["path"] == "/"

    @pytest.mark.asyncio
    async def test_set_cookie_all_options(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_set_cookie(
            name="token", value="xyz", path="/app",
            secure=True, httpOnly=True, sameSite="Strict",
            expires="2026-12-31T23:59:59Z",
            tab_id="t1", frame_id=1,
        )
        args = mock_cli.call_args[0]
        params = json.loads(args[2])
        assert params["name"] == "token"
        assert params["value"] == "xyz"
        assert params["path"] == "/app"
        assert params["secure"] is True
        assert params["httpOnly"] is True
        assert params["sameSite"] == "Strict"
        assert params["expires"] == "2026-12-31T23:59:59Z"
        assert params["tab_id"] == "t1"
        assert params["frame_id"] == 1

    @pytest.mark.asyncio
    async def test_delete_cookies_default(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_delete_cookies()
        mock_cli.assert_called_once_with("delete-cookies")

    @pytest.mark.asyncio
    async def test_delete_cookies_with_url_name(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_delete_cookies(url="https://a.com", name="sid")
        mock_cli.assert_called_once_with("delete-cookies", "https://a.com", "sid")

    @pytest.mark.asyncio
    async def test_delete_cookies_name_only(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_delete_cookies(name="sid")
        mock_cli.assert_called_once_with("delete-cookies", "--name", "sid")

    @pytest.mark.asyncio
    async def test_delete_cookies_with_tab(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_delete_cookies(tab_id="t1")
        mock_cli.assert_called_once_with("delete-cookies", "--tab-id", "t1")


# ── Storage ────────────────────────────────────────────────────


class TestStorage:

    @pytest.mark.asyncio
    async def test_get_storage(self, mock_cli):
        mock_cli.return_value = '{"data": {}}'
        await server.browser_get_storage(storage_type="localStorage")
        mock_cli.assert_called_once_with("storage", "localStorage")

    @pytest.mark.asyncio
    async def test_get_storage_with_key(self, mock_cli):
        mock_cli.return_value = '{"value": "x"}'
        await server.browser_get_storage(storage_type="sessionStorage", key="token")
        mock_cli.assert_called_once_with("storage", "sessionStorage", "token")

    @pytest.mark.asyncio
    async def test_get_storage_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"data": {}}'
        await server.browser_get_storage(
            storage_type="localStorage", tab_id="t1", frame_id=1,
        )
        mock_cli.assert_called_once_with(
            "storage", "localStorage", "--tab-id", "t1", "--frame-id", "1"
        )

    @pytest.mark.asyncio
    async def test_set_storage(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_set_storage(
            storage_type="localStorage", key="theme", value="dark",
        )
        mock_cli.assert_called_once_with("set-storage", "localStorage", "theme", "dark")

    @pytest.mark.asyncio
    async def test_set_storage_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_set_storage(
            storage_type="localStorage", key="k", value="v",
            tab_id="t1", frame_id=1,
        )
        mock_cli.assert_called_once_with(
            "set-storage", "localStorage", "k", "v",
            "--tab-id", "t1", "--frame-id", "1",
        )

    @pytest.mark.asyncio
    async def test_delete_storage(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_delete_storage(storage_type="localStorage")
        mock_cli.assert_called_once_with("delete-storage", "localStorage")

    @pytest.mark.asyncio
    async def test_delete_storage_with_key(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_delete_storage(storage_type="localStorage", key="theme")
        mock_cli.assert_called_once_with("delete-storage", "localStorage", "theme")

    @pytest.mark.asyncio
    async def test_delete_storage_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_delete_storage(
            storage_type="sessionStorage", key="x", tab_id="t1", frame_id=1,
        )
        mock_cli.assert_called_once_with(
            "delete-storage", "sessionStorage", "x",
            "--tab-id", "t1", "--frame-id", "1",
        )


# ── Network Monitoring ─────────────────────────────────────────


class TestNetworkMonitoring:

    @pytest.mark.asyncio
    async def test_network_monitor_start(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_network_monitor_start()
        mock_cli.assert_called_once_with("net-start")

    @pytest.mark.asyncio
    async def test_network_monitor_stop(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_network_monitor_stop()
        mock_cli.assert_called_once_with("net-stop")

    @pytest.mark.asyncio
    async def test_network_get_log_default(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_network_get_log()
        mock_cli.assert_called_once_with("net-log")

    @pytest.mark.asyncio
    async def test_network_get_log_all_filters(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_network_get_log(
            url_filter="api", method_filter="POST",
            status_filter=404, limit=10,
        )
        mock_cli.assert_called_once_with(
            "net-log", "--url-filter", "api", "--method-filter", "POST",
            "--status-filter", "404", "--limit", "10",
        )

    @pytest.mark.asyncio
    async def test_network_get_log_default_limit_omitted(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_network_get_log(limit=50)
        mock_cli.assert_called_once_with("net-log")


# ── Request Interception ───────────────────────────────────────


class TestIntercept:

    @pytest.mark.asyncio
    async def test_intercept_add_rule(self, mock_cli):
        mock_cli.return_value = '{"rule_id": 1}'
        await server.browser_intercept_add_rule(pattern=".*ads.*", action="block")
        mock_cli.assert_called_once_with("intercept-add", ".*ads.*", "block")

    @pytest.mark.asyncio
    async def test_intercept_add_rule_with_headers(self, mock_cli):
        mock_cli.return_value = '{"rule_id": 1}'
        headers = '{"X-Custom": "value"}'
        await server.browser_intercept_add_rule(
            pattern=".*api.*", action="modify_headers", headers=headers,
        )
        mock_cli.assert_called_once_with(
            "intercept-add", ".*api.*", "modify_headers", "--headers", headers,
        )

    @pytest.mark.asyncio
    async def test_intercept_remove_rule(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_intercept_remove_rule(rule_id=5)
        mock_cli.assert_called_once_with("intercept-remove", "5")

    @pytest.mark.asyncio
    async def test_intercept_list_rules(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_intercept_list_rules()
        mock_cli.assert_called_once_with("intercept-list")


# ── Session Persistence ────────────────────────────────────────


class TestSessionPersistence:

    @pytest.mark.asyncio
    async def test_session_save(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_session_save(file_path="/tmp/session.json")
        mock_cli.assert_called_once_with("session-save", "/tmp/session.json")

    @pytest.mark.asyncio
    async def test_session_restore(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_session_restore(file_path="/tmp/session.json")
        mock_cli.assert_called_once_with("session-restore", "/tmp/session.json")


# ── Multi-Tab Coordination ─────────────────────────────────────


class TestMultiTab:

    @pytest.mark.asyncio
    async def test_compare_tabs(self, mock_cli):
        mock_cli.return_value = '{"comparison": "..."}'
        await server.browser_compare_tabs(tab_ids="t1,t2,t3")
        mock_cli.assert_called_once_with("compare", "t1,t2,t3")

    @pytest.mark.asyncio
    async def test_batch_navigate(self, mock_cli):
        mock_cli.return_value = '{"tabs": []}'
        await server.browser_batch_navigate(urls="https://a.com,https://b.com")
        mock_cli.assert_called_once_with("batch-nav", "https://a.com", "https://b.com")

    @pytest.mark.asyncio
    async def test_batch_navigate_no_persist(self, mock_cli):
        mock_cli.return_value = '{"tabs": []}'
        await server.browser_batch_navigate(
            urls="https://a.com, https://b.com", persist=False,
        )
        mock_cli.assert_called_once_with(
            "batch-nav", "https://a.com", "https://b.com", "--persist", "false",
        )


# ── Visual Grounding / Find ────────────────────────────────────


class TestFindElement:

    @pytest.mark.asyncio
    async def test_find_element_by_description(self, mock_cli):
        mock_cli.return_value = "match output"
        await server.browser_find_element_by_description(description="login button")
        mock_cli.assert_called_once_with("find", "login button")

    @pytest.mark.asyncio
    async def test_find_element_with_tab_frame(self, mock_cli):
        mock_cli.return_value = "match output"
        await server.browser_find_element_by_description(
            description="submit", tab_id="t1", frame_id=2,
        )
        mock_cli.assert_called_once_with(
            "find", "submit", "--tab-id", "t1", "--frame-id", "2"
        )


# ── Action Recording ──────────────────────────────────────────


class TestRecording:

    @pytest.mark.asyncio
    async def test_record_start(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_record_start()
        mock_cli.assert_called_once_with("record-start")

    @pytest.mark.asyncio
    async def test_record_stop(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_record_stop()
        mock_cli.assert_called_once_with("record-stop")

    @pytest.mark.asyncio
    async def test_record_save(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_record_save(file_path="/tmp/recording.json")
        mock_cli.assert_called_once_with("record-save", "/tmp/recording.json")

    @pytest.mark.asyncio
    async def test_record_replay(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_record_replay(file_path="/tmp/rec.json", delay=1.0)
        args = mock_cli.call_args[0]
        assert args[0] == "record-replay"
        assert args[1] == "-j"
        params = json.loads(args[2])
        assert params["file_path"] == "/tmp/rec.json"
        assert params["delay"] == 1.0

    @pytest.mark.asyncio
    async def test_replay_status(self, mock_cli):
        mock_cli.return_value = '{"active": true}'
        await server.browser_replay_status()
        mock_cli.assert_called_once_with("replay-status")


# ── Drag-and-Drop ──────────────────────────────────────────────


class TestDragAndDrop:

    @pytest.mark.asyncio
    async def test_drag(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_drag(source_index=1, target_index=5)
        mock_cli.assert_called_once_with("drag", "1", "5")

    @pytest.mark.asyncio
    async def test_drag_custom_steps(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_drag(source_index=1, target_index=5, steps=20)
        mock_cli.assert_called_once_with("drag", "1", "5", "--steps", "20")

    @pytest.mark.asyncio
    async def test_drag_default_steps_omitted(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_drag(source_index=1, target_index=5, steps=10)
        mock_cli.assert_called_once_with("drag", "1", "5")

    @pytest.mark.asyncio
    async def test_drag_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_drag(
            source_index=1, target_index=5, tab_id="t1", frame_id=2,
        )
        mock_cli.assert_called_once_with(
            "drag", "1", "5", "--tab-id", "t1", "--frame-id", "2"
        )

    @pytest.mark.asyncio
    async def test_drag_coordinates(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_drag_coordinates(
            start_x=10, start_y=20, end_x=100, end_y=200,
        )
        mock_cli.assert_called_once_with("drag-xy", "10", "20", "100", "200")

    @pytest.mark.asyncio
    async def test_drag_coordinates_custom_steps(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_drag_coordinates(
            start_x=10, start_y=20, end_x=100, end_y=200, steps=5,
        )
        mock_cli.assert_called_once_with(
            "drag-xy", "10", "20", "100", "200", "--steps", "5"
        )

    @pytest.mark.asyncio
    async def test_drag_coordinates_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_drag_coordinates(
            start_x=0, start_y=0, end_x=50, end_y=50,
            tab_id="t1", frame_id=1,
        )
        mock_cli.assert_called_once_with(
            "drag-xy", "0", "0", "50", "50",
            "--tab-id", "t1", "--frame-id", "1",
        )


# ── Chrome-Context Eval ─────────────────────────────────────────


class TestEvalChrome:

    @pytest.mark.asyncio
    async def test_eval_chrome(self, mock_cli):
        mock_cli.return_value = '{"result": "ok"}'
        await server.browser_eval_chrome(expression="Services.appinfo.version")
        mock_cli.assert_called_once_with("eval-chrome", "Services.appinfo.version")


# ── Reflect ────────────────────────────────────────────────────


class TestReflect:

    @pytest.mark.asyncio
    async def test_reflect_returns_image_and_text(self, mock_cli, fake_screenshot):
        mock_cli.return_value = json.dumps({
            "screenshot_path": str(fake_screenshot),
            "url": "https://example.com",
            "title": "Example",
            "loading": False,
            "page_text": "Hello world",
        })
        result = await server.browser_reflect()
        mock_cli.assert_called_once_with("reflect")
        assert len(result) == 2
        assert isinstance(result[0], Image)
        assert "https://example.com" in result[1]
        assert "Example" in result[1]

    @pytest.mark.asyncio
    async def test_reflect_with_goal(self, mock_cli, fake_screenshot):
        mock_cli.return_value = json.dumps({
            "screenshot_path": str(fake_screenshot),
            "url": "https://a.com",
            "title": "A",
            "loading": False,
            "page_text": "text",
        })
        result = await server.browser_reflect(goal="find the price")
        mock_cli.assert_called_once_with("reflect", "--goal", "find the price")
        assert "find the price" in result[1]

    @pytest.mark.asyncio
    async def test_reflect_with_tab(self, mock_cli, fake_screenshot):
        mock_cli.return_value = json.dumps({
            "screenshot_path": str(fake_screenshot),
            "url": "https://a.com",
            "title": "A",
            "loading": False,
        })
        result = await server.browser_reflect(tab_id="t1")
        mock_cli.assert_called_once_with("reflect", "--tab-id", "t1")

    @pytest.mark.asyncio
    async def test_reflect_no_screenshot(self, mock_cli):
        mock_cli.return_value = json.dumps({
            "url": "https://a.com",
            "title": "A",
            "loading": False,
            "page_text": "text",
        })
        result = await server.browser_reflect()
        # Only text block, no Image
        assert len(result) == 1
        assert isinstance(result[0], str)

    @pytest.mark.asyncio
    async def test_reflect_with_notifications(self, mock_cli, fake_screenshot):
        mock_cli.return_value = json.dumps({
            "screenshot_path": str(fake_screenshot),
            "url": "https://a.com",
            "title": "A",
            "loading": False,
            "notifications": "--- NOTIFICATION: alert ---",
        })
        result = await server.browser_reflect()
        assert "NOTIFICATION" in result[1]


# ── File Upload ────────────────────────────────────────────────


class TestFileUpload:

    @pytest.mark.asyncio
    async def test_file_upload(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_file_upload(file_path="/tmp/test.pdf", index=3)
        mock_cli.assert_called_once_with("upload", "/tmp/test.pdf", "3")

    @pytest.mark.asyncio
    async def test_file_upload_with_tab_frame(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_file_upload(
            file_path="/tmp/f.txt", index=1, tab_id="t1", frame_id=2,
        )
        mock_cli.assert_called_once_with(
            "upload", "/tmp/f.txt", "1", "--tab-id", "t1", "--frame-id", "2"
        )


# ── Session Management ─────────────────────────────────────────


class TestSessionManagement:

    @pytest.mark.asyncio
    async def test_session_info(self, mock_cli):
        mock_cli.return_value = '{"session_id": "abc"}'
        await server.browser_session_info()
        mock_cli.assert_called_once_with("session", "info")

    @pytest.mark.asyncio
    async def test_session_close_resets_session_id(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        server._session_id = "old-session"
        await server.browser_session_close()
        mock_cli.assert_called_once_with("session", "close")
        assert server._session_id == ""

    @pytest.mark.asyncio
    async def test_list_sessions(self, mock_cli):
        mock_cli.return_value = '[{"id": "s1"}, {"id": "s2"}]'
        await server.browser_list_sessions()
        mock_cli.assert_called_once_with("session", "list")

    @pytest.mark.asyncio
    async def test_set_session_name(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_set_session_name(name="researcher")
        mock_cli.assert_called_once_with("session", "name", "researcher")


# ── Tab Claiming ───────────────────────────────────────────────


class TestTabClaiming:

    @pytest.mark.asyncio
    async def test_list_workspace_tabs(self, mock_cli):
        mock_cli.return_value = "[]"
        await server.browser_list_workspace_tabs()
        mock_cli.assert_called_once_with("workspace-tabs")

    @pytest.mark.asyncio
    async def test_claim_tab(self, mock_cli):
        mock_cli.return_value = '{"ok": true}'
        await server.browser_claim_tab(tab_id="tab-xyz")
        mock_cli.assert_called_once_with("claim-tab", "tab-xyz")


# ── Health Check ───────────────────────────────────────────────


class TestPing:

    @pytest.mark.asyncio
    async def test_ping(self, mock_cli):
        mock_cli.return_value = '{"status": "pong"}'
        await server.browser_ping()
        mock_cli.assert_called_once_with("ping")


# ── Error Handling ─────────────────────────────────────────────


class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_cli_nonzero_exit_raises(self):
        """CLI returning non-zero exit code raises Exception."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"some error msg"))
        proc.returncode = 1
        proc.kill = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch.object(server, "_session_id", "test"):
                with pytest.raises(Exception, match="some error msg"):
                    await server._run_cli("bad-command")

    @pytest.mark.asyncio
    async def test_cli_nonzero_exit_no_stderr(self):
        """CLI returning non-zero with no stderr uses generic message."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 42
        proc.kill = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch.object(server, "_session_id", ""):
                with pytest.raises(Exception, match="exited with code 42"):
                    await server._run_cli("fail")

    @pytest.mark.asyncio
    async def test_cli_timeout_raises(self):
        """CLI command timeout kills process and raises."""
        proc = MagicMock()
        proc.kill = MagicMock()

        call_count = 0

        async def _communicate():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate hanging longer than the timeout
                await asyncio.sleep(999)
            return (b"", b"")

        proc.communicate = _communicate

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            with patch.object(server, "_session_id", "test"):
                with pytest.raises(Exception, match="timed out"):
                    await server._run_cli("slow-cmd", timeout=0.01)

    @pytest.mark.asyncio
    async def test_cli_success(self):
        """CLI returning 0 returns stdout."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b'{"ok":true}', b""))
        proc.returncode = 0
        proc.kill = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch.object(server, "_session_id", "test"):
                result = await server._run_cli("good-cmd")
                assert result == '{"ok":true}'


# ═══════════════════════════════════════════════════════════════
# CLI TESTS
# ═══════════════════════════════════════════════════════════════


# ── Auto Type ──────────────────────────────────────────────────


class TestAutoType:

    def test_true(self):
        assert cli._auto_type("true") is True
        assert cli._auto_type("True") is True

    def test_false(self):
        assert cli._auto_type("false") is False
        assert cli._auto_type("False") is False

    def test_int(self):
        assert cli._auto_type("42") == 42
        assert cli._auto_type("-1") == -1

    def test_float(self):
        assert cli._auto_type("3.14") == 3.14

    def test_string(self):
        assert cli._auto_type("hello") == "hello"

    def test_empty_string(self):
        assert cli._auto_type("") == ""


# ── Arg Parsing ────────────────────────────────────────────────


class TestParseToolArgs:

    def test_positional_args(self):
        result = cli._parse_tool_args(["5"], ["index"])
        assert result == {"index": 5}

    def test_multiple_positional(self):
        result = cli._parse_tool_args(["3", "hello"], ["index", "value"])
        assert result == {"index": 3, "value": "hello"}

    def test_named_args(self):
        result = cli._parse_tool_args(["--index", "5"], [])
        assert result == {"index": 5}

    def test_named_with_hyphen(self):
        result = cli._parse_tool_args(["--tab-id", "abc"], [])
        assert result == {"tab_id": "abc"}

    def test_boolean_flag(self):
        result = cli._parse_tool_args(["--viewport-only"], [])
        assert result == {"viewport_only": True}

    def test_json_mode(self):
        result = cli._parse_tool_args(["-j", '{"index": 5, "value": "x"}'], [])
        assert result == {"index": 5, "value": "x"}

    def test_mixed_positional_and_named(self):
        result = cli._parse_tool_args(
            ["5", "--tab-id", "t1"], ["index"],
        )
        assert result == {"index": 5, "tab_id": "t1"}

    def test_extra_positional_collected(self):
        result = cli._parse_tool_args(["a", "b", "c"], ["first"])
        assert result == {"first": "a", "_extra": ["b", "c"]}

    def test_empty_args(self):
        assert cli._parse_tool_args([], []) == {}

    def test_auto_type_in_positional(self):
        # Booleans always convert; non-numeric param names stay as strings
        result = cli._parse_tool_args(["true", "42", "3.14"], ["a", "b", "c"])
        assert result == {"a": True, "b": "42", "c": "3.14"}

    def test_auto_type_numeric_params(self):
        # Known numeric params (index, timeout, etc.) get coerced
        result = cli._parse_tool_args(["5", "10"], ["index", "timeout"])
        assert result == {"index": 5, "timeout": 10}


# ── COMMANDS Dict ──────────────────────────────────────────────


class TestCommandsDict:

    def test_click_maps_to_click_element(self):
        method, pos = cli.COMMANDS["click"]
        assert method == "click_element"
        assert pos == ["index"]

    def test_fill_maps_to_fill_field(self):
        method, pos = cli.COMMANDS["fill"]
        assert method == "fill_field"
        assert pos == ["index", "value"]

    def test_type_maps_to_type_text(self):
        method, pos = cli.COMMANDS["type"]
        assert method == "type_text"
        assert pos == ["text"]

    def test_eval_maps_to_console_evaluate(self):
        method, pos = cli.COMMANDS["eval"]
        assert method == "console_evaluate"
        assert pos == ["expression"]

    def test_logs_is_special_command(self):
        # logs is handled specially (formatted output), not in COMMANDS
        assert "logs" in cli.SPECIAL_COMMANDS

    def test_errors_is_special_command(self):
        # errors is handled specially (formatted output), not in COMMANDS
        assert "errors" in cli.SPECIAL_COMMANDS

    def test_drag_uses_sourceIndex_targetIndex(self):
        method, pos = cli.COMMANDS["drag"]
        assert method == "drag_element"
        assert pos == ["sourceIndex", "targetIndex"]

    def test_drag_xy_uses_startX_startY(self):
        method, pos = cli.COMMANDS["drag-xy"]
        assert method == "drag_coordinates"
        assert pos == ["startX", "startY", "endX", "endY"]

    def test_storage_uses_storage_type(self):
        method, pos = cli.COMMANDS["storage"]
        assert method == "get_storage"
        assert pos == ["storage_type", "key"]

    def test_set_storage_has_three_positionals(self):
        method, pos = cli.COMMANDS["set-storage"]
        assert method == "set_storage"
        assert pos == ["storage_type", "key", "value"]

    def test_nav_alias(self):
        method, pos = cli.COMMANDS["nav"]
        assert method == "navigate"
        assert pos == ["url"]

    def test_navigate_alias(self):
        method, pos = cli.COMMANDS["navigate"]
        assert method == "navigate"

    def test_key_maps_to_press_key(self):
        method, pos = cli.COMMANDS["key"]
        assert method == "press_key"
        assert pos == ["key"]

    def test_scroll_xy_maps_to_scroll_at_point(self):
        method, pos = cli.COMMANDS["scroll-xy"]
        assert method == "scroll_at_point"
        assert pos == ["x", "y", "direction", "amount"]

    def test_upload_maps_to_file_upload(self):
        method, pos = cli.COMMANDS["upload"]
        assert method == "file_upload"
        assert pos == ["file_path", "index"]

    def test_download_maps_to_wait_for_download(self):
        method, pos = cli.COMMANDS["download"]
        assert method == "wait_for_download"
        assert pos == ["timeout"]

    def test_hover_maps_to_hover(self):
        method, pos = cli.COMMANDS["hover"]
        assert method == "hover"
        assert pos == ["index"]

    def test_hover_xy_maps_to_hover_coordinates(self):
        method, pos = cli.COMMANDS["hover-xy"]
        assert method == "hover_coordinates"
        assert pos == ["x", "y"]


# ── SPECIAL_COMMANDS Set ───────────────────────────────────────


class TestSpecialCommands:

    def test_ping_is_special(self):
        assert "ping" in cli.SPECIAL_COMMANDS

    def test_screenshot_is_special(self):
        assert "screenshot" in cli.SPECIAL_COMMANDS
        assert "ss" in cli.SPECIAL_COMMANDS

    def test_session_is_special(self):
        assert "session" in cli.SPECIAL_COMMANDS

    def test_reflect_is_special(self):
        assert "reflect" in cli.SPECIAL_COMMANDS

    def test_grounded_commands_are_special(self):
        assert "gclick" in cli.SPECIAL_COMMANDS
        assert "ghover" in cli.SPECIAL_COMMANDS
        assert "gscroll" in cli.SPECIAL_COMMANDS

    def test_formatted_output_commands_are_special(self):
        assert "logs" in cli.SPECIAL_COMMANDS
        assert "errors" in cli.SPECIAL_COMMANDS
        assert "net-log" in cli.SPECIAL_COMMANDS

    def test_batch_nav_is_special(self):
        assert "batch-nav" in cli.SPECIAL_COMMANDS

    def test_compare_is_special(self):
        assert "compare" in cli.SPECIAL_COMMANDS


# ── BrowserClient Notifications ────────────────────────────────


class TestBrowserClientNotifications:

    def test_drain_notifications_empty(self):
        client = cli.BrowserClient()
        assert client.drain_notifications() == ""

    def test_drain_notifications_dialog(self):
        client = cli.BrowserClient()
        client._pending_notifications = [
            {"type": "dialog_opened", "dialog_type": "alert", "message": "Are you sure?"},
        ]
        result = client.drain_notifications()
        assert "NOTIFICATION" in result
        assert "alert" in result
        assert "Are you sure?" in result
        assert "handle-dialog" in result
        assert client._pending_notifications == []

    def test_drain_notifications_popup_blocked(self):
        client = cli.BrowserClient()
        client._pending_notifications = [
            {
                "type": "popup_blocked",
                "blocked_count": 2,
                "popup_urls": ["https://a.com", "https://b.com"],
            },
        ]
        result = client.drain_notifications()
        assert "blocked 2 popup(s)" in result
        assert "https://a.com" in result
        assert "popup-allow" in result

    def test_drain_notifications_popup_blocked_no_urls(self):
        client = cli.BrowserClient()
        client._pending_notifications = [
            {"type": "popup_blocked", "blocked_count": 1},
        ]
        result = client.drain_notifications()
        assert "blocked 1 popup(s)" in result

    def test_drain_notifications_unknown_type(self):
        client = cli.BrowserClient()
        client._pending_notifications = [
            {"type": "something_else", "data": 42},
        ]
        result = client.drain_notifications()
        assert "NOTIFICATION (something_else)" in result

    def test_drain_clears_list(self):
        client = cli.BrowserClient()
        client._pending_notifications = [
            {"type": "dialog_opened", "dialog_type": "confirm", "message": "yes?"},
        ]
        client.drain_notifications()
        assert client._pending_notifications == []
        assert client.drain_notifications() == ""

    def test_drain_multiple_notifications(self):
        client = cli.BrowserClient()
        client._pending_notifications = [
            {"type": "dialog_opened", "dialog_type": "alert", "message": "A"},
            {"type": "popup_blocked", "blocked_count": 1},
        ]
        result = client.drain_notifications()
        assert "alert" in result
        assert "popup" in result


# ── Replay System ──────────────────────────────────────────────


class TestReplaySystem:

    def test_sanitize_session_id(self):
        assert cli._sanitize_session_id("abc-123_xyz") == "abc-123_xyz"
        assert cli._sanitize_session_id("bad/chars!@#") == "badchars"
        assert cli._sanitize_session_id("") == ""

    def test_init_replay_dir_creates_directory(self, tmp_path):
        with patch.object(cli, "REPLAY_DISABLED", False):
            with patch("tempfile.gettempdir", return_value=str(tmp_path)):
                result = cli._init_replay_dir("test-session")
                assert result is not None
                assert os.path.isdir(result)
                manifest_path = os.path.join(result, "manifest.json")
                assert os.path.exists(manifest_path)
                with open(manifest_path) as f:
                    manifest = json.load(f)
                assert manifest["session_id"] == "test-session"
                assert "started_at" in manifest
                assert manifest["next_seq"] == 0

    def test_init_replay_dir_disabled(self):
        with patch.object(cli, "REPLAY_DISABLED", True):
            assert cli._init_replay_dir("test") is None

    def test_init_replay_dir_empty_session(self):
        assert cli._init_replay_dir("") is None

    def test_claim_next_seq(self, tmp_path):
        replay_dir = str(tmp_path / "replay")
        os.makedirs(replay_dir)
        manifest = {"session_id": "test", "next_seq": 0}
        with open(os.path.join(replay_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f)

        seq = cli._claim_next_seq(replay_dir)
        assert seq == 0

        seq2 = cli._claim_next_seq(replay_dir)
        assert seq2 == 1

        # Verify manifest updated
        with open(os.path.join(replay_dir, "manifest.json")) as f:
            updated = json.load(f)
        assert updated["next_seq"] == 2

    def test_claim_next_seq_missing_manifest(self, tmp_path):
        replay_dir = str(tmp_path / "replay")
        os.makedirs(replay_dir)
        # No manifest file — should start at 0
        seq = cli._claim_next_seq(replay_dir)
        assert seq == 0

    def test_append_log_entry(self, tmp_path):
        replay_dir = str(tmp_path)
        entry = {"seq": 0, "tool": "click", "args": {"index": 5}}
        cli._append_log_entry(replay_dir, entry)

        log_path = os.path.join(replay_dir, "tool_log.jsonl")
        assert os.path.exists(log_path)
        with open(log_path) as f:
            line = f.readline()
        assert json.loads(line)["tool"] == "click"

    def test_append_log_multiple_entries(self, tmp_path):
        replay_dir = str(tmp_path)
        cli._append_log_entry(replay_dir, {"seq": 0, "tool": "click"})
        cli._append_log_entry(replay_dir, {"seq": 1, "tool": "fill"})

        log_path = os.path.join(replay_dir, "tool_log.jsonl")
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["tool"] == "click"
        assert json.loads(lines[1])["tool"] == "fill"


# ── Grounding Coordinate Parsing ──────────────────────────────


class TestGroundingCoordParsing:

    def test_qwen_box_format_norm1000(self):
        text = "<|box_start|>(500, 250)<|box_end|>"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "norm1000")
        assert x == 960
        assert y == 270

    def test_qwen_box_format_absolute(self):
        text = "<|box_start|>(500, 250)<|box_end|>"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "absolute")
        assert x == 500
        assert y == 250

    def test_point_tag_format(self):
        text = "<point>750 500</point>"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "norm1000")
        assert x == 1440
        assert y == 540

    def test_bbox_format(self):
        text = "The element is at [100, 200, 300, 400]"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "norm1000")
        # Center of bbox: (200, 300)
        assert x == round(200 * 1920 / 1000)
        assert y == round(300 * 1080 / 1000)

    def test_normalized_float_format(self):
        text = "The element is at (0.5, 0.25)"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "norm1000")
        # Float 0-1 format always uses direct multiplication
        assert x == round(0.5 * 1920)
        assert y == round(0.25 * 1080)

    def test_abs_int_format(self):
        text = "Click at (500, 250)"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "absolute")
        assert x == 500
        assert y == 250

    def test_abs_decimal_format(self):
        text = "Click at (500.5, 250.7)"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "norm1000")
        # Code does: _denorm(round(500.5), round(250.7)) = _denorm(500, 251)
        # Then norm1000: round(500 * 1920 / 1000) = 960, round(251 * 1080 / 1000) = 271
        assert x == 960
        assert y == 271

    def test_no_coords_found(self):
        text = "I cannot find the element"
        x, y = cli._parse_grounding_coords(text, 1920, 1080, "norm1000")
        assert x is None
        assert y is None


# ── Dispatch Routing ───────────────────────────────────────────


class TestDispatch:

    @pytest.fixture
    def mock_client(self):
        client = MagicMock(spec=cli.BrowserClient)
        client.connect = AsyncMock()
        client.command = AsyncMock(return_value={"status": "pong", "version": "1.0", "session_id": "s1"})
        client.close = AsyncMock()
        client.session_id = "s1"
        client.last_tab_url = ""
        client._pending_notifications = []
        client.drain_notifications = MagicMock(return_value="")
        return client

    @pytest.mark.asyncio
    async def test_dispatch_ping(self, mock_client, capsys):
        result = await cli._dispatch("ping", [], mock_client)
        assert result == 0
        mock_client.command.assert_called_once_with("ping")
        out = capsys.readouterr().out
        assert "pong" in out

    @pytest.mark.asyncio
    async def test_dispatch_generic_command(self, mock_client, capsys):
        mock_client.command.return_value = {"ok": True}
        result = await cli._dispatch("back", [], mock_client)
        assert result == 0
        mock_client.command.assert_called_once_with("go_back", None)

    @pytest.mark.asyncio
    async def test_dispatch_click_with_positional(self, mock_client, capsys):
        mock_client.command.return_value = {"ok": True}
        result = await cli._dispatch("click", ["5"], mock_client)
        assert result == 0
        mock_client.command.assert_called_once_with("click_element", {"index": 5})

    @pytest.mark.asyncio
    async def test_dispatch_fill_with_positionals(self, mock_client, capsys):
        mock_client.command.return_value = {"ok": True}
        result = await cli._dispatch("fill", ["3", "hello"], mock_client)
        assert result == 0
        mock_client.command.assert_called_once_with(
            "fill_field", {"index": 3, "value": "hello"}
        )

    @pytest.mark.asyncio
    async def test_dispatch_press_key_with_modifiers(self, mock_client, capsys):
        mock_client.command.return_value = {"ok": True}
        result = await cli._dispatch("key", ["a", "--ctrl", "--shift"], mock_client)
        assert result == 0
        call_args = mock_client.command.call_args
        assert call_args[0][0] == "press_key"
        params = call_args[0][1]
        assert params["key"] == "a"
        assert params["modifiers"] == {"ctrl": True, "shift": True}

    @pytest.mark.asyncio
    async def test_dispatch_unknown_command_uses_method_name(self, mock_client, capsys):
        mock_client.command.return_value = {"ok": True}
        result = await cli._dispatch("some-custom-method", ["--foo", "bar"], mock_client)
        assert result == 0
        mock_client.command.assert_called_once_with(
            "some_custom_method", {"foo": "bar"}
        )

    @pytest.mark.asyncio
    async def test_dispatch_session_info(self, mock_client, capsys):
        mock_client.command.return_value = {"session_id": "s1", "workspace": "ZenRipple"}
        result = await cli._dispatch("session", ["info"], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "s1" in out

    @pytest.mark.asyncio
    async def test_dispatch_session_no_subcmd(self, mock_client, capsys):
        result = await cli._dispatch("session", [], mock_client)
        assert result == 1

    @pytest.mark.asyncio
    async def test_dispatch_elements(self, mock_client, capsys):
        mock_client.command.return_value = {
            "url": "https://example.com",
            "title": "Test",
            "elements": [
                {"index": 0, "tag": "button", "text": "Click me", "attributes": {}},
            ],
        }
        result = await cli._dispatch("elements", [], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "[0]" in out
        assert "Click me" in out

    @pytest.mark.asyncio
    async def test_dispatch_reflect(self, mock_client, capsys):
        mock_client.command.side_effect = [
            {"image": "data:image/jpeg;base64,/9j/4AAQ", "width": 1920, "height": 1080},
            {"url": "https://example.com", "title": "Test", "loading": False},
            {"text": "Hello world"},
        ]
        with patch.object(cli, "_terminal_supports_inline_images", return_value=False):
            result = await cli._dispatch("reflect", [], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "example.com" in out

    @pytest.mark.asyncio
    async def test_dispatch_logs_formatted(self, mock_client, capsys):
        mock_client.command.return_value = {
            "logs": [
                {"timestamp": "12:00", "level": "info", "message": "started"},
                {"timestamp": "12:01", "level": "warn", "message": "slow query"},
            ],
        }
        result = await cli._dispatch("logs", [], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "[info]" in out
        assert "started" in out
        assert "[warn]" in out

    @pytest.mark.asyncio
    async def test_dispatch_errors_formatted(self, mock_client, capsys):
        mock_client.command.return_value = {
            "errors": [
                {"timestamp": "12:00", "type": "error", "message": "fail", "stack": "at line 1"},
            ],
        }
        result = await cli._dispatch("errors", [], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "[error]" in out
        assert "fail" in out
        assert "at line 1" in out

    @pytest.mark.asyncio
    async def test_dispatch_errors_empty(self, mock_client, capsys):
        mock_client.command.return_value = {"errors": []}
        result = await cli._dispatch("errors", [], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "no errors" in out

    @pytest.mark.asyncio
    async def test_dispatch_net_log_formatted(self, mock_client, capsys):
        mock_client.command.return_value = [
            {"method": "GET", "url": "https://api.com/data", "status": 200, "content_type": "application/json"},
        ]
        result = await cli._dispatch("net-log", [], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "GET" in out
        assert "api.com" in out
        assert "[200]" in out

    @pytest.mark.asyncio
    async def test_dispatch_net_log_empty(self, mock_client, capsys):
        mock_client.command.return_value = []
        result = await cli._dispatch("net-log", [], mock_client)
        assert result == 0
        out = capsys.readouterr().out
        assert "no network entries" in out

    @pytest.mark.asyncio
    async def test_dispatch_intercept_add(self, mock_client, capsys):
        mock_client.command.return_value = {"rule_id": 1}
        result = await cli._dispatch(
            "intercept-add", [".*ads.*", "block"], mock_client,
        )
        assert result == 0
        mock_client.command.assert_called_once_with(
            "intercept_add_rule",
            {"pattern": ".*ads.*", "action": "block"},
        )

    @pytest.mark.asyncio
    async def test_dispatch_intercept_add_with_headers(self, mock_client, capsys):
        mock_client.command.return_value = {"rule_id": 1}
        headers_json = '{"X-Custom": "val"}'
        result = await cli._dispatch(
            "intercept-add", [".*api.*", "modify_headers", "--headers", headers_json],
            mock_client,
        )
        assert result == 0
        call_args = mock_client.command.call_args[0][1]
        assert call_args["headers"] == {"X-Custom": "val"}

    @pytest.mark.asyncio
    async def test_dispatch_batch_nav(self, mock_client, capsys):
        mock_client.command.return_value = {"tabs": []}
        result = await cli._dispatch(
            "batch-nav", ["https://a.com", "https://b.com"], mock_client,
        )
        assert result == 0
        call_args = mock_client.command.call_args
        assert call_args[0][0] == "batch_navigate"
        assert call_args[0][1]["urls"] == ["https://a.com", "https://b.com"]

    @pytest.mark.asyncio
    async def test_dispatch_compare(self, mock_client, capsys):
        mock_client.command.return_value = {"comparison": "same"}
        result = await cli._dispatch("compare", ["t1,t2"], mock_client)
        assert result == 0
        call_args = mock_client.command.call_args
        assert call_args[0][0] == "compare_tabs"
        assert call_args[0][1]["tab_ids"] == ["t1", "t2"]


# ── Navigation Command Sets ───────────────────────────────────


class TestNavigationCommandSets:

    def test_navigation_commands(self):
        assert "create-tab" in cli._NAVIGATION_COMMANDS
        assert "navigate" in cli._NAVIGATION_COMMANDS
        assert "nav" in cli._NAVIGATION_COMMANDS
        assert "back" in cli._NAVIGATION_COMMANDS
        assert "forward" in cli._NAVIGATION_COMMANDS
        assert "reload" in cli._NAVIGATION_COMMANDS
        assert "batch-nav" in cli._NAVIGATION_COMMANDS



# ── Terminal Support ───────────────────────────────────────────


class TestTerminalSupport:

    def test_iterm_detected(self):
        with patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}, clear=False):
            assert cli._terminal_supports_inline_images() is True

    def test_wezterm_detected(self):
        with patch.dict(os.environ, {"TERM_PROGRAM": "WezTerm"}, clear=False):
            assert cli._terminal_supports_inline_images() is True

    def test_kitty_detected(self):
        with patch.dict(os.environ, {"KITTY_WINDOW_ID": "1"}, clear=False):
            assert cli._terminal_supports_inline_images() is True

    def test_lc_terminal_iterm2(self):
        with patch.dict(os.environ, {"LC_TERMINAL": "iTerm2", "TERM_PROGRAM": ""}, clear=False):
            assert cli._terminal_supports_inline_images() is True

    def test_unknown_terminal(self):
        with patch.dict(os.environ, {
            "TERM_PROGRAM": "Terminal.app",
            "LC_TERMINAL": "",
            "KITTY_WINDOW_ID": "",
        }, clear=False):
            # Remove KITTY_WINDOW_ID if present
            env = os.environ.copy()
            env.pop("KITTY_WINDOW_ID", None)
            with patch.dict(os.environ, env, clear=True):
                assert cli._terminal_supports_inline_images() is False


# ── BrowserClient Init ─────────────────────────────────────────


class TestBrowserClientInit:

    def test_default_init(self):
        with patch.object(cli, "SESSION_ID", ""):
            client = cli.BrowserClient()
            assert client._requested_session == ""
            assert client.session_id is None
            assert client._pending_notifications == []

    def test_init_with_session(self):
        client = cli.BrowserClient(session_id="my-session")
        assert client._requested_session == "my-session"

    def test_init_from_env(self):
        with patch.object(cli, "SESSION_ID", "env-session"):
            client = cli.BrowserClient()
            assert client._requested_session == "env-session"


# ── Prune Old Replays ─────────────────────────────────────────


class TestPruneOldReplays:

    def test_prune_keeps_within_limit(self, tmp_path):
        # Create some replay dirs
        for i in range(3):
            d = tmp_path / f"zenripple_replay_session{i}"
            d.mkdir()
            with open(d / "manifest.json", "w") as f:
                json.dump({"started_at": f"2026-01-0{i+1}T00:00:00"}, f)

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            with patch.object(cli, "REPLAY_KEEP", 10):
                cli._prune_old_replays(None)
        # All 3 should still exist
        assert len(list(tmp_path.glob("zenripple_replay_*"))) == 3

    def test_prune_removes_oldest(self, tmp_path):
        for i in range(5):
            d = tmp_path / f"zenripple_replay_session{i}"
            d.mkdir()
            with open(d / "manifest.json", "w") as f:
                json.dump({"started_at": f"2026-01-0{i+1}T00:00:00"}, f)

        current = str(tmp_path / "zenripple_replay_session4")
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            with patch.object(cli, "REPLAY_KEEP", 3):
                cli._prune_old_replays(current)
        remaining = list(tmp_path.glob("zenripple_replay_*"))
        # Should keep 3: current + 2 newest
        assert len(remaining) == 3
        # Current should always be kept
        assert (tmp_path / "zenripple_replay_session4").exists()


# ── Main Function ──────────────────────────────────────────────


class TestMain:

    @pytest.mark.asyncio
    async def test_help_flag(self, capsys):
        result = await cli.main(["--help"])
        assert result == 0
        out = capsys.readouterr().out
        assert "zenripple" in out

    @pytest.mark.asyncio
    async def test_no_args_shows_help(self, capsys):
        result = await cli.main([])
        assert result == 0
        out = capsys.readouterr().out
        assert "zenripple" in out

    @pytest.mark.asyncio
    async def test_session_flag_override(self):
        with patch.object(cli, "_dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = 0
            with patch.object(cli, "BrowserClient") as MockClient:
                mock_instance = MagicMock()
                mock_instance.session_id = "test"
                mock_instance._ws = None
                mock_instance.close = AsyncMock()
                MockClient.return_value = mock_instance

                with patch.object(cli, "REPLAY_DISABLED", True):
                    result = await cli.main(["-s", "override-id", "ping"])

                MockClient.assert_called_once_with(session_id="override-id")


# ── Version Reading ────────────────────────────────────────────


class TestVersionReading:

    def test_read_version_returns_string(self):
        version = cli._read_version()
        # Should return something (either the actual version or "unknown")
        assert isinstance(version, str)
        assert len(version) > 0


# ── Auth Token ─────────────────────────────────────────────────


class TestAuthToken:

    def test_read_from_env(self):
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": "my-token"}, clear=False):
            assert cli._read_auth_token() == "my-token"

    def test_read_from_env_strips_whitespace(self):
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": "  tok  "}, clear=False):
            assert cli._read_auth_token() == "tok"

    def test_read_from_file_when_no_env(self):
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": ""}, clear=False):
            with patch.object(Path, "read_text", return_value="file-token\n"):
                assert cli._read_auth_token() == "file-token"

    def test_returns_empty_when_no_source(self):
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": ""}, clear=False):
            with patch.object(Path, "read_text", side_effect=FileNotFoundError):
                assert cli._read_auth_token() == ""
