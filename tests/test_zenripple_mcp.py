"""Tests for the ZenRipple MCP server.

Covers message formatting, connection management, tool definitions,
and error handling. Uses a mock WebSocket server to simulate the browser.
"""

import asyncio
import base64
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from mcp.server.fastmcp.utilities.types import Image

import zenripple_mcp_server as server
import zenripple_session_file as session_file


# ── Helpers ─────────────────────────────────────────────────────


class FakeResponse:
    """Simulates a websockets v16 response object."""

    def __init__(self, headers=None):
        self.headers = headers or {}


class FakeWebSocket:
    """Simulates a websockets connection for testing."""

    def __init__(self, responses=None, response_headers=None):
        self.sent = []
        self._responses = responses or []
        self._response_idx = 0
        self.closed = False
        # v16+ API: ws.response.headers
        self.response = FakeResponse(response_headers or {})

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if self._response_idx < len(self._responses):
            resp = self._responses[self._response_idx]
            self._response_idx += 1
            # Echo back the correct message ID from the sent request
            if isinstance(resp, dict) and self.sent:
                try:
                    sent_msg = json.loads(self.sent[-1])
                    resp = {**resp, "id": sent_msg.get("id", resp.get("id"))}
                except (json.JSONDecodeError, IndexError):
                    pass
            return json.dumps(resp) if isinstance(resp, dict) else resp
        raise asyncio.TimeoutError("No more responses")

    async def ping(self):
        if self.closed:
            raise ConnectionError("closed")

    async def close(self):
        self.closed = True


# ── text_result ─────────────────────────────────────────────────


class TestTextResult:
    def test_dict(self):
        result = server.text_result({"key": "value"})
        assert json.loads(result) == {"key": "value"}

    def test_list(self):
        result = server.text_result([1, 2, 3])
        assert json.loads(result) == [1, 2, 3]

    def test_string(self):
        assert server.text_result("hello") == "hello"

    def test_number(self):
        assert server.text_result(42) == "42"

    def test_nested(self):
        data = {"tabs": [{"id": "1", "title": "Test"}]}
        result = server.text_result(data)
        assert json.loads(result) == data


# ── browser_command ─────────────────────────────────────────────


class TestBrowserCommand:
    @pytest.mark.asyncio
    async def test_sends_correct_format(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "ignored", "result": {"ok": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_command("ping", {"foo": "bar"})

        assert len(fake_ws.sent) == 1
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "ping"
        assert msg["params"] == {"foo": "bar"}
        assert "id" in msg
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_default_empty_params(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": {}}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_command("list_tabs")

        msg = json.loads(fake_ws.sent[0])
        assert msg["params"] == {}

    @pytest.mark.asyncio
    async def test_raises_on_error_response(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Tab not found"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="Tab not found"):
                await server.browser_command("close_tab", {"tab_id": "bad"})

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        fake_ws = FakeWebSocket(responses=[])  # no responses -> timeout
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(asyncio.TimeoutError):
                await server.browser_command("ping")

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self):
        """Connection-level errors trigger one retry with reconnection."""
        call_count = 0

        async def flaky_get_ws():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                ws = FakeWebSocket(responses=[])
                # Make send raise to simulate connection error
                async def bad_send(msg):
                    raise ConnectionError("socket closed")
                ws.send = bad_send
                return ws
            else:
                return FakeWebSocket(
                    responses=[{"id": "x", "result": {"ok": True}}]
                )

        with patch.object(server, "get_ws", side_effect=flaky_get_ws):
            result = await server.browser_command("ping")
        assert result == {"ok": True}
        assert call_count == 2  # first attempt failed, second succeeded

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_no_result_key(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x"}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_command("ping")
        assert result == {}


# ── get_ws ──────────────────────────────────────────────────────


class TestGetWs:
    @pytest.mark.asyncio
    async def test_creates_new_connection(self):
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket()
        with patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
            ws = await server.get_ws()
        assert ws is fake_ws
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_reuses_existing_connection(self):
        fake_ws = FakeWebSocket()
        server._ws_connection = fake_ws
        ws = await server.get_ws()
        assert ws is fake_ws
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_reconnects_on_dead_connection(self):
        dead_ws = FakeWebSocket()
        dead_ws.closed = True
        server._ws_connection = dead_ws

        new_ws = FakeWebSocket()
        with patch("websockets.connect", new_callable=AsyncMock, return_value=new_ws):
            ws = await server.get_ws()
        assert ws is new_ws
        server._ws_connection = None
        server._session_id = None


# ── Tool Definitions ────────────────────────────────────────────


class TestToolDefinitions:
    """Verify all expected tools are registered and callable."""

    @pytest.mark.asyncio
    async def test_create_tab(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"tab_id": "panel1", "url": "https://example.com"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_create_tab("https://example.com")
        data = json.loads(result)
        assert data["tab_id"] == "panel1"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "create_tab"
        assert msg["params"]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_create_tab_persist(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"tab_id": "panel1", "url": "https://example.com", "persist": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_create_tab("https://example.com", persist=True)
        data = json.loads(result)
        assert data["persist"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["persist"] is True

    @pytest.mark.asyncio
    async def test_create_tab_persist_by_default(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"tab_id": "panel1", "url": "https://example.com", "persist": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_create_tab("https://example.com")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["persist"] is True

    @pytest.mark.asyncio
    async def test_close_tab_default(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_close_tab()
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] is None

    @pytest.mark.asyncio
    async def test_list_tabs(self):
        tabs = [
            {"tab_id": "p1", "title": "Tab 1", "url": "https://a.com", "active": True},
            {"tab_id": "p2", "title": "Tab 2", "url": "https://b.com", "active": False},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": tabs}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_tabs()
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["active"] is True

    @pytest.mark.asyncio
    async def test_navigate(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_navigate("https://example.com")
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "navigate"
        assert msg["params"]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_go_back(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_go_back()
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "go_back"

    @pytest.mark.asyncio
    async def test_go_forward(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_go_forward()
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "go_forward"

    @pytest.mark.asyncio
    async def test_reload(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_reload()
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "reload"

    @pytest.mark.asyncio
    async def test_get_page_info(self):
        info = {
            "url": "https://example.com",
            "title": "Example",
            "loading": False,
            "can_go_back": True,
            "can_go_forward": False,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": info}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_page_info()
        data = json.loads(result)
        assert data["title"] == "Example"

    @pytest.mark.asyncio
    async def test_wait(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait(0.1)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["seconds"] == 0.1


# ── Observation Tools (Phase 2) ────────────────────────────────


# Minimal valid 1x1 white PNG (67 bytes)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_PNG_B64 = base64.b64encode(_TINY_PNG).decode()
_TINY_DATA_URL = f"data:image/png;base64,{_TINY_PNG_B64}"


class TestScreenshot:
    @pytest.mark.asyncio
    async def test_returns_image_and_dimensions(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_screenshot()
        assert isinstance(result, list)
        assert isinstance(result[0], Image)
        assert "1x1px" in result[1]
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "screenshot"

    @pytest.mark.asyncio
    async def test_sends_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_screenshot("panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"

    @pytest.mark.asyncio
    async def test_default_tab_id_none(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_screenshot()
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] is None


class TestGetDom:
    @pytest.mark.asyncio
    async def test_formats_elements(self):
        dom_result = {
            "elements": [
                {
                    "index": 0,
                    "tag": "a",
                    "text": "Click me",
                    "attributes": {"href": "https://example.com"},
                    "rect": {"x": 10, "y": 20, "w": 100, "h": 30},
                },
                {
                    "index": 1,
                    "tag": "button",
                    "text": "Submit",
                    "attributes": {"type": "submit"},
                    "rect": {"x": 50, "y": 100, "w": 80, "h": 40},
                },
            ],
            "url": "https://example.com",
            "title": "Example",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom_result}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dom()
        assert "Page: https://example.com" in result
        assert "Title: Example" in result
        assert '[0] <a href="https://example.com">Click me</a>' in result
        assert '[1] <button type="submit">Submit</button>' in result
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "get_dom"

    @pytest.mark.asyncio
    async def test_empty_elements(self):
        dom_result = {
            "elements": [],
            "url": "about:blank",
            "title": "",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom_result}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dom()
        assert "Page: about:blank" in result
        assert "Interactive elements:" in result

    @pytest.mark.asyncio
    async def test_sends_tab_id(self):
        dom_result = {"elements": [], "url": "", "title": ""}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom_result}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_dom("panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"


class TestGetPageText:
    @pytest.mark.asyncio
    async def test_returns_text(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"text": "Hello World"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_page_text()
        assert result == "Hello World"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "get_page_text"

    @pytest.mark.asyncio
    async def test_empty_text(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"text": ""}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_page_text()
        assert result == ""

    @pytest.mark.asyncio
    async def test_sends_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"text": "test"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_page_text("panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"


class TestGetPageHTML:
    @pytest.mark.asyncio
    async def test_returns_html(self):
        html = "<html><body><h1>Hello</h1></body></html>"
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"html": html}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_page_html()
        assert result == html
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "get_page_html"

    @pytest.mark.asyncio
    async def test_empty_html(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"html": ""}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_page_html()
        assert result == ""

    @pytest.mark.asyncio
    async def test_sends_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"html": "<html></html>"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_page_html("panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"


# ── Interaction Tools (Phase 3) ─────────────────────────────────


class TestClick:
    @pytest.mark.asyncio
    async def test_click_element(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "button", "text": "Submit"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_click(0)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "click_element"
        assert msg["params"]["index"] == 0

    @pytest.mark.asyncio
    async def test_click_with_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "a", "text": "Link"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_click(3, "panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"
        assert msg["params"]["index"] == 3

    @pytest.mark.asyncio
    async def test_click_coordinates(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "div", "text": ""}}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_click_coordinates(100, 200)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "click_coordinates"
        # No scaling when no cached dimensions
        assert msg["params"]["x"] == 100
        assert msg["params"]["y"] == 200


class TestFill:
    @pytest.mark.asyncio
    async def test_fill_field(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "input", "value": "hello"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_fill(2, "hello")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "fill_field"
        assert msg["params"]["index"] == 2
        assert msg["params"]["value"] == "hello"

    @pytest.mark.asyncio
    async def test_fill_with_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "textarea", "value": "text"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_fill(1, "text", "panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"

    @pytest.mark.asyncio
    async def test_select_option(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "value": "opt2"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_select_option(5, "opt2")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "select_option"
        assert msg["params"]["index"] == 5
        assert msg["params"]["value"] == "opt2"


class TestType:
    @pytest.mark.asyncio
    async def test_type_text(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "length": 5}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_type("hello")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "type_text"
        assert msg["params"]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_press_key(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "key": "Enter"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_press_key("Enter")
        data = json.loads(result)
        assert data["key"] == "Enter"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "press_key"
        assert msg["params"]["key"] == "Enter"

    @pytest.mark.asyncio
    async def test_press_key_with_modifiers(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "key": "a"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_press_key("a", ctrl=True, shift=True)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["modifiers"]["ctrl"] is True
        assert msg["params"]["modifiers"]["shift"] is True
        assert msg["params"]["modifiers"]["alt"] is False
        assert msg["params"]["modifiers"]["meta"] is False


class TestScroll:
    @pytest.mark.asyncio
    async def test_scroll_default(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "scrollX": 0, "scrollY": 500}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_scroll()
        data = json.loads(result)
        assert data["scrollY"] == 500
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "scroll"
        assert msg["params"]["direction"] == "down"
        assert msg["params"]["amount"] == 500

    @pytest.mark.asyncio
    async def test_scroll_up(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "scrollX": 0, "scrollY": 0}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_scroll("up", 300)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["direction"] == "up"
        assert msg["params"]["amount"] == 300


class TestHover:
    @pytest.mark.asyncio
    async def test_hover(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "a", "text": "Link"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_hover(1)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "hover"
        assert msg["params"]["index"] == 1

    @pytest.mark.asyncio
    async def test_hover_with_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "button", "text": "Menu"}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_hover(0, "panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"
        assert msg["params"]["index"] == 0


# ── Console / Eval (Phase 4) ────────────────────────────────────


class TestConsoleSetup:
    @pytest.mark.asyncio
    async def test_setup(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_setup()
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "console_setup"

    @pytest.mark.asyncio
    async def test_setup_with_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_console_setup("panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"


class TestConsoleLogs:
    @pytest.mark.asyncio
    async def test_formats_logs(self):
        logs = [
            {"level": "log", "message": "hello world", "timestamp": "2025-01-01T00:00:00.000Z"},
            {"level": "warn", "message": "be careful", "timestamp": "2025-01-01T00:00:01.000Z"},
        ]
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"logs": logs}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_logs()
        assert "[log]" in result
        assert "hello world" in result
        assert "[warn]" in result
        assert "be careful" in result
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "console_get_logs"

    @pytest.mark.asyncio
    async def test_empty_logs(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"logs": []}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_logs()
        assert "no console logs" in result.lower()

    @pytest.mark.asyncio
    async def test_sends_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"logs": []}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_console_logs("panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"


class TestConsoleErrors:
    @pytest.mark.asyncio
    async def test_formats_errors(self):
        errors = [
            {
                "type": "uncaught_error",
                "message": "x is not defined",
                "filename": "script.js",
                "lineno": 42,
                "stack": "ReferenceError: x is not defined\n    at script.js:42",
                "timestamp": "2025-01-01T00:00:00.000Z",
            },
        ]
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"errors": errors}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_errors()
        assert "[uncaught_error]" in result
        assert "x is not defined" in result
        assert "script.js:42" in result

    @pytest.mark.asyncio
    async def test_empty_errors(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"errors": []}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_errors()
        assert "no errors" in result.lower()

    @pytest.mark.asyncio
    async def test_sends_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"errors": []}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_console_errors("panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"


class TestWaitForLoad:
    @pytest.mark.asyncio
    async def test_wait_for_load(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "url": "https://example.com", "title": "Example", "loading": False}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_load()
        data = json.loads(result)
        assert data["success"] is True
        assert data["loading"] is False
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "wait_for_load"

    @pytest.mark.asyncio
    async def test_wait_for_load_with_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "url": "https://example.com", "title": "Example", "loading": False}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_wait_for_load("panel1", timeout=10)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"
        assert msg["params"]["timeout"] == 10

    @pytest.mark.asyncio
    async def test_wait_for_load_still_loading(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "url": "https://example.com", "title": "", "loading": True}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_load(timeout=1)
        data = json.loads(result)
        assert data["loading"] is True


class TestSaveScreenshot:
    @pytest.mark.asyncio
    async def test_save_screenshot(self, tmp_path):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}}
            ]
        )
        file_path = str(tmp_path / "test.png")
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_save_screenshot(file_path)
        assert "Screenshot saved" in result
        assert "test.png" in result
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "screenshot"
        # Verify the file was written with correct PNG data
        with open(file_path, "rb") as f:
            data = f.read()
        assert data == _TINY_PNG

    @pytest.mark.asyncio
    async def test_save_screenshot_with_tab_id(self, tmp_path):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}}
            ]
        )
        file_path = str(tmp_path / "tab.png")
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_save_screenshot(file_path, "panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"

    @pytest.mark.asyncio
    async def test_save_screenshot_creates_dirs(self, tmp_path):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}}
            ]
        )
        file_path = str(tmp_path / "subdir" / "nested" / "shot.png")
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_save_screenshot(file_path)
        assert "Screenshot saved" in result
        assert os.path.exists(file_path)


class TestConsoleEval:
    @pytest.mark.asyncio
    async def test_eval_success(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"result": "2"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_eval("1+1")
        assert result == "2"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "console_evaluate"
        assert msg["params"]["expression"] == "1+1"

    @pytest.mark.asyncio
    async def test_eval_error(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"error": "x is not defined", "stack": "ReferenceError..."}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_eval("x.y.z")
        assert "Error:" in result
        assert "x is not defined" in result

    @pytest.mark.asyncio
    async def test_eval_with_tab_id(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"result": "hello"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_console_eval("'hello'", "panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"
        assert msg["params"]["expression"] == "'hello'"

    @pytest.mark.asyncio
    async def test_eval_returns_string(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"result": "Example Domain"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_console_eval("document.title")
        assert result == "Example Domain"


# ── Error Paths ─────────────────────────────────────────────────


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_connection_refused(self):
        server._ws_connection = None
        with patch(
            "websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionRefusedError("refused"),
        ):
            with pytest.raises(ConnectionError, match="Could not connect to Zen Browser"):
                await server.get_ws()
        server._ws_connection = None

    @pytest.mark.asyncio
    async def test_error_response_unknown_message(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"code": -1}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="Unknown browser error"):
                await server.browser_command("bad_method")


# ── Phase 6: New Tools ────────────────────────────────────────


class TestListFrames:
    @pytest.mark.asyncio
    async def test_list_frames(self):
        frames = [
            {"frame_id": 1, "url": "https://example.com", "is_top": True},
            {"frame_id": 2, "url": "https://ads.example.com", "is_top": False},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": frames}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_frames()
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["is_top"] is True


class TestGetDomWithFrameId:
    @pytest.mark.asyncio
    async def test_get_dom_passes_frame_id(self):
        dom = {"elements": [], "url": "https://example.com", "title": "Test"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_dom(frame_id=42)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["frame_id"] == 42

    @pytest.mark.asyncio
    async def test_get_dom_no_frame_id(self):
        dom = {"elements": [], "url": "https://example.com", "title": "Test"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_dom()
        msg = json.loads(fake_ws.sent[0])
        assert "frame_id" not in msg["params"]


class TestWaitForElement:
    @pytest.mark.asyncio
    async def test_wait_for_element(self):
        resp = {"found": True, "tag": "button", "text": "Submit"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_element("button.submit")
        data = json.loads(result)
        assert data["found"] is True


class TestWaitForText:
    @pytest.mark.asyncio
    async def test_wait_for_text(self):
        resp = {"found": True}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_text("Hello World")
        data = json.loads(result)
        assert data["found"] is True


class TestNavigationStatus:
    @pytest.mark.asyncio
    async def test_get_navigation_status(self):
        resp = {"url": "https://example.com", "http_status": 200, "error_code": 0, "loading": False}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_navigation_status()
        data = json.loads(result)
        assert data["http_status"] == 200

    @pytest.mark.asyncio
    async def test_get_navigation_status_404(self):
        resp = {"url": "https://example.com/bad", "http_status": 404, "error_code": 0, "loading": False}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_navigation_status()
        data = json.loads(result)
        assert data["http_status"] == 404


class TestDialogs:
    @pytest.mark.asyncio
    async def test_get_dialogs_empty(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": []}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dialogs()
        assert json.loads(result) == []

    @pytest.mark.asyncio
    async def test_get_dialogs_with_alert(self):
        dialogs = [{"type": "alertCheck", "message": "Hello!", "default_value": ""}]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dialogs}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dialogs()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["type"] == "alertCheck"

    @pytest.mark.asyncio
    async def test_handle_dialog_accept(self):
        resp = {"success": True, "action": "accept", "type": "alertCheck"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_handle_dialog("accept")
        data = json.loads(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_handle_dialog_with_text(self):
        resp = {"success": True, "action": "accept", "type": "prompt"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_handle_dialog("accept", text="my input")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["text"] == "my input"


class TestTabEvents:
    @pytest.mark.asyncio
    async def test_get_tab_events_empty(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": []}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_tab_events()
        assert json.loads(result) == []

    @pytest.mark.asyncio
    async def test_get_tab_events_with_popup(self):
        events = [
            {"type": "tab_opened", "tab_id": "p1", "opener_tab_id": "t1", "is_agent_tab": True},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": events}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_tab_events()
        data = json.loads(result)
        assert data[0]["type"] == "tab_opened"
        assert data[0]["is_agent_tab"] is True


class TestClipboard:
    @pytest.mark.asyncio
    async def test_clipboard_read(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": {"text": "hello"}}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_clipboard_read()
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_clipboard_write(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": {"success": True, "length": 5}}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_clipboard_write("hello")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["text"] == "hello"


# ── Phase 7: Cookies ──────────────────────────────────────────


class TestCookies:
    @pytest.mark.asyncio
    async def test_get_cookies(self):
        cookies = [
            {"name": "session", "value": "abc123", "domain": "example.com", "path": "/",
             "secure": True, "httpOnly": True, "sameSite": "lax", "expires": "session"},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": cookies}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_cookies(url="https://example.com")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["name"] == "session"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "get_cookies"
        assert msg["params"]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_get_cookies_with_name(self):
        cookies = [{"name": "token", "value": "xyz"}]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": cookies}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_cookies(url="https://example.com", name="token")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["name"] == "token"

    @pytest.mark.asyncio
    async def test_set_cookie(self):
        resp = {"success": True, "cookie": "test=val"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_set_cookie("test", "val")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "set_cookie"
        assert msg["params"]["name"] == "test"
        assert msg["params"]["value"] == "val"

    @pytest.mark.asyncio
    async def test_set_cookie_with_options(self):
        resp = {"success": True, "cookie": "pref=dark"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_set_cookie(
                "pref", "dark",
                httpOnly=True, sameSite="Strict"
            )
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["httpOnly"] is True
        assert msg["params"]["sameSite"] == "Strict"

    @pytest.mark.asyncio
    async def test_delete_cookies(self):
        resp = {"success": True, "removed": 3}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_delete_cookies(url="https://example.com")
        data = json.loads(result)
        assert data["removed"] == 3
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "delete_cookies"

    @pytest.mark.asyncio
    async def test_delete_cookie_by_name(self):
        resp = {"success": True, "removed": 1}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_delete_cookies(url="https://example.com", name="token")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["name"] == "token"


# ── Phase 7: Storage ──────────────────────────────────────────


class TestStorage:
    @pytest.mark.asyncio
    async def test_get_storage_single_key(self):
        resp = {"value": "dark"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_storage("localStorage", "theme")
        data = json.loads(result)
        assert data["value"] == "dark"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "get_storage"
        assert msg["params"]["storage_type"] == "localStorage"
        assert msg["params"]["key"] == "theme"

    @pytest.mark.asyncio
    async def test_get_storage_all(self):
        resp = {"entries": {"theme": "dark", "lang": "en"}, "count": 2}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_storage("sessionStorage")
        data = json.loads(result)
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_set_storage(self):
        resp = {"success": True, "key": "theme", "length": 1}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_set_storage("localStorage", "theme", "dark")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "set_storage"
        assert msg["params"]["key"] == "theme"
        assert msg["params"]["value"] == "dark"

    @pytest.mark.asyncio
    async def test_delete_storage_key(self):
        resp = {"success": True, "key": "theme", "length": 0}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_delete_storage("localStorage", "theme")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "delete_storage"
        assert msg["params"]["key"] == "theme"

    @pytest.mark.asyncio
    async def test_delete_storage_clear_all(self):
        resp = {"success": True, "cleared": 5, "length": 0}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_delete_storage("sessionStorage")
        data = json.loads(result)
        assert data["cleared"] == 5


# ── Phase 7: Network Monitoring ───────────────────────────────


class TestNetworkMonitoring:
    @pytest.mark.asyncio
    async def test_network_monitor_start(self):
        resp = {"success": True, "note": "Network monitoring started"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_network_monitor_start()
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "network_monitor_start"

    @pytest.mark.asyncio
    async def test_network_monitor_stop(self):
        resp = {"success": True, "note": "Network monitoring stopped"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_network_monitor_stop()
        data = json.loads(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_network_get_log_empty(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": []}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_network_get_log()
        assert "no network entries" in result.lower()

    @pytest.mark.asyncio
    async def test_network_get_log_with_entries(self):
        entries = [
            {"method": "GET", "url": "https://api.example.com/data", "type": "response", "status": 200, "content_type": "application/json"},
            {"method": "POST", "url": "https://api.example.com/submit", "type": "response", "status": 201, "content_type": ""},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": entries}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_network_get_log()
        assert "GET https://api.example.com/data [200]" in result
        assert "POST https://api.example.com/submit [201]" in result

    @pytest.mark.asyncio
    async def test_network_get_log_with_filters(self):
        entries = [{"method": "GET", "url": "https://example.com", "status": 404}]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": entries}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_network_get_log(url_filter="example", method_filter="GET", status_filter=404, limit=10)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["url_filter"] == "example"
        assert msg["params"]["method_filter"] == "GET"
        assert msg["params"]["status_filter"] == 404
        assert msg["params"]["limit"] == 10


# ── Phase 7: Request Interception ─────────────────────────────


class TestRequestInterception:
    @pytest.mark.asyncio
    async def test_add_rule_block(self):
        resp = {"success": True, "rule_id": 1}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_intercept_add_rule("ads\\.example\\.com", "block")
        data = json.loads(result)
        assert data["rule_id"] == 1
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "intercept_add_rule"
        assert msg["params"]["pattern"] == "ads\\.example\\.com"
        assert msg["params"]["action"] == "block"

    @pytest.mark.asyncio
    async def test_add_rule_modify_headers(self):
        resp = {"success": True, "rule_id": 2}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_intercept_add_rule(
                "api\\.example\\.com", "modify_headers",
                headers='{"Authorization": "Bearer tok123"}'
            )
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["headers"] == {"Authorization": "Bearer tok123"}

    @pytest.mark.asyncio
    async def test_remove_rule(self):
        resp = {"success": True}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_intercept_remove_rule(1)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["rule_id"] == 1

    @pytest.mark.asyncio
    async def test_list_rules(self):
        rules = [
            {"id": 1, "pattern": "ads\\.com", "action": "block", "headers": {}},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": rules}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_intercept_list_rules()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["action"] == "block"


# ── Phase 7: Session Persistence ──────────────────────────────


class TestSessionPersistence:
    @pytest.mark.asyncio
    async def test_session_save(self):
        resp = {"success": True, "tabs": 3, "cookies": 5, "file": "/tmp/session.json"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_save("/tmp/session.json")
        data = json.loads(result)
        assert data["tabs"] == 3
        assert data["cookies"] == 5
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "session_save"
        assert msg["params"]["file_path"] == "/tmp/session.json"

    @pytest.mark.asyncio
    async def test_session_restore(self):
        resp = {"success": True, "tabs_restored": 3, "cookies_restored": 5}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_restore("/tmp/session.json")
        data = json.loads(result)
        assert data["tabs_restored"] == 3
        assert data["cookies_restored"] == 5
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "session_restore"


# ── Phase 8: Smart DOM Filtering ──────────────────────────────


class TestSmartDOMFiltering:
    @pytest.mark.asyncio
    async def test_viewport_only(self):
        dom = {"elements": [{"index": 0, "tag": "button", "text": "Submit", "attributes": {}, "rect": {"x": 0, "y": 0, "w": 100, "h": 40}}], "url": "https://example.com", "title": "Test", "total": 1}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dom(viewport_only=True)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["viewport_only"] is True
        assert "Submit" in result

    @pytest.mark.asyncio
    async def test_max_elements(self):
        dom = {"elements": [{"index": 0, "tag": "a", "text": "Link", "attributes": {}, "rect": {"x": 0, "y": 0, "w": 50, "h": 20}}], "url": "https://example.com", "title": "Test", "total": 1}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dom(max_elements=10)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["max_elements"] == 10

    @pytest.mark.asyncio
    async def test_default_params_not_sent(self):
        dom = {"elements": [], "url": "", "title": "", "total": 0}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_dom()
        msg = json.loads(fake_ws.sent[0])
        assert "viewport_only" not in msg["params"]
        assert "max_elements" not in msg["params"]
        assert "incremental" not in msg["params"]


# ── Phase 8: Incremental DOM ──────────────────────────────────


class TestIncrementalDOM:
    @pytest.mark.asyncio
    async def test_incremental_diff(self):
        dom = {
            "elements": [{"index": 0, "tag": "button", "text": "New", "attributes": {}, "rect": {"x": 0, "y": 0, "w": 50, "h": 30}}],
            "url": "https://example.com",
            "title": "Test",
            "total": 1,
            "incremental": True,
            "diff": {"added": 1, "removed": 0, "total": 1, "added_elements": [{"index": 0, "tag": "button", "text": "New"}], "removed_elements": []},
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dom(incremental=True)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["incremental"] is True
        assert "Changes: +1 -0" in result
        assert "Added:" in result

    @pytest.mark.asyncio
    async def test_incremental_no_changes(self):
        dom = {
            "elements": [],
            "url": "https://example.com",
            "title": "Test",
            "total": 0,
            "incremental": True,
            "diff": {"added": 0, "removed": 0, "total": 0, "added_elements": [], "removed_elements": []},
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_dom(incremental=True)
        assert "Changes: +0 -0" in result


# ── Phase 8: Compact DOM ──────────────────────────────────────


class TestCompactDOM:
    @pytest.mark.asyncio
    async def test_compact_representation(self):
        dom = {
            "elements": [
                {"index": 0, "tag": "a", "text": "Example", "attributes": {"href": "https://example.com"}, "rect": {"x": 0, "y": 0, "w": 100, "h": 20}},
                {"index": 1, "tag": "button", "text": "Submit", "attributes": {"type": "submit"}, "rect": {"x": 0, "y": 40, "w": 80, "h": 30}},
                {"index": 2, "tag": "input", "text": "", "attributes": {"value": "hello", "type": "text"}, "rect": {"x": 0, "y": 80, "w": 200, "h": 30}},
            ],
            "url": "https://example.com",
            "title": "Test",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_elements_compact()
        assert "URL: https://example.com" in result
        assert "[0] Example (a \u2192https://example.com)" in result
        assert "[1] Submit (button type=submit)" in result
        assert "[2]  (input =hello)" in result

    @pytest.mark.asyncio
    async def test_compact_with_role(self):
        dom = {
            "elements": [
                {"index": 0, "tag": "div", "text": "Menu", "role": "button", "attributes": {}, "rect": {"x": 0, "y": 0, "w": 50, "h": 30}},
            ],
            "url": "https://example.com",
            "title": "Test",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_elements_compact()
        assert "[0] Menu (div role=button)" in result

    @pytest.mark.asyncio
    async def test_compact_viewport_only(self):
        dom = {"elements": [], "url": "https://example.com", "title": "Test"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_elements_compact(viewport_only=True)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["viewport_only"] is True

    @pytest.mark.asyncio
    async def test_compact_max_elements(self):
        dom = {"elements": [], "url": "https://example.com", "title": "Test"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_elements_compact(max_elements=20)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["max_elements"] == 20


# ── Phase 8: Accessibility Tree ───────────────────────────────


class TestAccessibilityTree:
    @pytest.mark.asyncio
    async def test_accessibility_tree(self):
        resp = {
            "nodes": [
                {"role": "document", "name": "Example", "depth": 0},
                {"role": "heading", "name": "Hello World", "depth": 1},
                {"role": "link", "name": "Click me", "depth": 1},
                {"role": "pushbutton", "name": "Submit", "depth": 1},
            ],
            "total": 4,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_accessibility_tree()
        assert "Accessibility tree (4 nodes)" in result
        assert "[document] Example" in result
        assert "  [heading] Hello World" in result
        assert "  [link] Click me" in result
        assert "  [pushbutton] Submit" in result

    @pytest.mark.asyncio
    async def test_accessibility_tree_error(self):
        resp = {"nodes": [], "error": "Accessibility service not available"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_accessibility_tree()
        assert "Accessibility tree error" in result

    @pytest.mark.asyncio
    async def test_accessibility_tree_empty(self):
        resp = {"nodes": [], "total": 0}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_accessibility_tree()
        assert "no accessibility nodes" in result

    @pytest.mark.asyncio
    async def test_accessibility_tree_with_value(self):
        resp = {
            "nodes": [{"role": "entry", "name": "Search", "value": "hello", "depth": 0}],
            "total": 1,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_accessibility_tree()
        assert "[entry] Search =hello" in result

    @pytest.mark.asyncio
    async def test_accessibility_tree_sends_params(self):
        resp = {"nodes": [], "total": 0}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_get_accessibility_tree("panel1", frame_id=42)
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "get_accessibility_tree"
        assert msg["params"]["tab_id"] == "panel1"
        assert msg["params"]["frame_id"] == 42


# ── Phase 9: Multi-Tab Coordination ──────────────────────────


class TestMultiTabCoordination:
    @pytest.mark.asyncio
    async def test_compare_tabs(self):
        resp = [
            {"tab_id": "p1", "url": "https://a.com", "title": "A", "text_preview": "Page A"},
            {"tab_id": "p2", "url": "https://b.com", "title": "B", "text_preview": "Page B"},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_compare_tabs("p1,p2")
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["tab_id"] == "p1"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "compare_tabs"
        assert msg["params"]["tab_ids"] == ["p1", "p2"]

    @pytest.mark.asyncio
    async def test_compare_tabs_too_few(self):
        result = await server.browser_compare_tabs("p1")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_batch_navigate(self):
        resp = {"success": True, "tabs": [
            {"tab_id": "p1", "url": "https://a.com"},
            {"tab_id": "p2", "url": "https://b.com"},
        ]}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_batch_navigate("https://a.com,https://b.com")
        data = json.loads(result)
        assert data["success"] is True
        assert len(data["tabs"]) == 2
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "batch_navigate"
        assert msg["params"]["urls"] == ["https://a.com", "https://b.com"]
        assert msg["params"]["persist"] is True

    @pytest.mark.asyncio
    async def test_batch_navigate_persist(self):
        resp = {"success": True, "tabs": [
            {"tab_id": "p1", "url": "https://a.com"},
        ], "persist": True}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_batch_navigate("https://a.com", persist=True)
        data = json.loads(result)
        assert data["persist"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["persist"] is True

    @pytest.mark.asyncio
    async def test_batch_navigate_empty(self):
        result = await server.browser_batch_navigate("")
        assert "Error" in result


# ── Phase 9: Visual Grounding ─────────────────────────────────


class TestVisualGrounding:
    @pytest.mark.asyncio
    async def test_find_element_basic(self):
        dom = {
            "elements": [
                {"index": 0, "tag": "a", "text": "Home", "attributes": {"href": "/"}},
                {"index": 1, "tag": "button", "text": "Login", "attributes": {"type": "submit"}},
                {"index": 2, "tag": "input", "text": "Search", "attributes": {"type": "text", "name": "q"}},
                {"index": 3, "tag": "a", "text": "About Us", "attributes": {"href": "/about"}},
            ],
            "url": "https://example.com",
            "title": "Test",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_find_element_by_description("login button")
        assert "Matches for 'login button'" in result
        assert "[1]" in result  # Login button should be a top match
        assert "Login" in result

    @pytest.mark.asyncio
    async def test_find_element_no_match(self):
        dom = {
            "elements": [
                {"index": 0, "tag": "a", "text": "Home", "attributes": {}},
            ],
            "url": "https://example.com",
            "title": "Test",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_find_element_by_description("submit form")
        assert "No elements match" in result

    @pytest.mark.asyncio
    async def test_find_element_empty_page(self):
        dom = {"elements": [], "url": "", "title": ""}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_find_element_by_description("anything")
        assert "no interactive elements" in result

    @pytest.mark.asyncio
    async def test_find_element_with_role(self):
        dom = {
            "elements": [
                {"index": 0, "tag": "div", "text": "Menu", "role": "navigation", "attributes": {}},
                {"index": 1, "tag": "div", "text": "Content", "role": "main", "attributes": {}},
            ],
            "url": "https://example.com",
            "title": "Test",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": dom}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_find_element_by_description("navigation menu")
        assert "[0]" in result  # navigation div should match


# ── Phase 9: Action Recording ─────────────────────────────────


class TestActionRecording:
    @pytest.mark.asyncio
    async def test_record_start(self):
        resp = {"success": True, "note": "Recording started"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_record_start()
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "record_start"

    @pytest.mark.asyncio
    async def test_record_stop(self):
        resp = {"success": True, "actions": 5}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_record_stop()
        data = json.loads(result)
        assert data["actions"] == 5

    @pytest.mark.asyncio
    async def test_record_save(self):
        resp = {"success": True, "file": "/tmp/rec.json", "actions": 5}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_record_save("/tmp/rec.json")
        data = json.loads(result)
        assert data["actions"] == 5
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["file_path"] == "/tmp/rec.json"

    @pytest.mark.asyncio
    async def test_record_replay(self):
        resp = {"success": True, "replayed": 5, "total": 5}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_record_replay("/tmp/rec.json", delay=0.1)
        data = json.loads(result)
        assert data["replayed"] == 5
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["delay"] == 0.1

    @pytest.mark.asyncio
    async def test_record_replay_with_errors(self):
        resp = {"success": True, "replayed": 3, "total": 5, "errors": [{"method": "bad", "error": "failed"}]}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_record_replay("/tmp/rec.json")
        data = json.loads(result)
        assert data["errors"] is not None


# ── Phase 10: Drag-and-Drop ──────────────────────────────────


class TestDrag:
    @pytest.mark.asyncio
    async def test_drag_element(self):
        resp = {"success": True, "from": {"x": 100, "y": 100}, "to": {"x": 300, "y": 300}, "steps": 10, "source_tag": "div", "target_tag": "div"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_drag(0, 1)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "drag_element"
        assert msg["params"]["sourceIndex"] == 0
        assert msg["params"]["targetIndex"] == 1

    @pytest.mark.asyncio
    async def test_drag_element_custom_steps(self):
        resp = {"success": True, "steps": 5}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_drag(0, 1, steps=5)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["steps"] == 5

    @pytest.mark.asyncio
    async def test_drag_element_with_tab_id(self):
        resp = {"success": True}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_drag(0, 1, tab_id="panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"

    @pytest.mark.asyncio
    async def test_drag_element_with_frame_id(self):
        resp = {"success": True}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_drag(0, 1, frame_id=42)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["frame_id"] == 42

    @pytest.mark.asyncio
    async def test_drag_coordinates(self):
        resp = {"success": True, "from": {"x": 10, "y": 20}, "to": {"x": 300, "y": 400}, "steps": 10}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_drag_coordinates(10, 20, 300, 400)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "drag_coordinates"
        assert msg["params"]["startX"] == 10
        assert msg["params"]["startY"] == 20
        assert msg["params"]["endX"] == 300
        assert msg["params"]["endY"] == 400

    @pytest.mark.asyncio
    async def test_drag_coordinates_custom_steps(self):
        resp = {"success": True, "steps": 20}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_drag_coordinates(0, 0, 100, 100, steps=20)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["steps"] == 20


# ── Phase 10: Chrome-Context Eval ────────────────────────────


class TestChromeEval:
    @pytest.mark.asyncio
    async def test_eval_chrome_simple(self):
        resp = {"result": "Zen"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_eval_chrome("Services.appinfo.name")
        assert "Zen" in result
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "eval_chrome"
        assert msg["params"]["expression"] == "Services.appinfo.name"

    @pytest.mark.asyncio
    async def test_eval_chrome_error(self):
        resp = {"error": "ReferenceError: x is not defined", "stack": "line 1"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_eval_chrome("x.y.z")
        assert "Error:" in result
        assert "ReferenceError" in result

    @pytest.mark.asyncio
    async def test_eval_chrome_complex_result(self):
        resp = {"result": {"name": "Zen", "version": "1.0", "tabs": [1, 2, 3]}}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_eval_chrome("({name: 'Zen', version: '1.0', tabs: [1,2,3]})")
        data = json.loads(result)
        assert data["name"] == "Zen"
        assert data["tabs"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_eval_chrome_number_result(self):
        resp = {"result": 42}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_eval_chrome("gBrowser.tabs.length")
        assert "42" in result

    @pytest.mark.asyncio
    async def test_eval_chrome_null_result(self):
        resp = {"result": None}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_eval_chrome("null")
        assert "null" in result


# ── Phase 10: Reflection ─────────────────────────────────────


class TestReflect:
    @pytest.mark.asyncio
    async def test_reflect_basic(self):
        # 1x1 white JPEG
        tiny_jpeg = base64.b64encode(b'\xff\xd8\xff\xe0').decode()
        fake_ws = FakeWebSocket(responses=[
            {"id": "x", "result": {"image": f"data:image/jpeg;base64,{tiny_jpeg}"}},
            {"id": "x", "result": {"text": "Example Domain"}},
            {"id": "x", "result": {"url": "https://example.com", "title": "Example Domain", "loading": False}},
        ])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_reflect()
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], Image)
        assert "URL: https://example.com" in result[1]
        assert "Example Domain" in result[1]

    @pytest.mark.asyncio
    async def test_reflect_with_goal(self):
        tiny_jpeg = base64.b64encode(b'\xff\xd8\xff\xe0').decode()
        fake_ws = FakeWebSocket(responses=[
            {"id": "x", "result": {"image": f"data:image/jpeg;base64,{tiny_jpeg}"}},
            {"id": "x", "result": {"text": "Page content"}},
            {"id": "x", "result": {"url": "https://example.com", "title": "Test", "loading": False}},
        ])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_reflect(goal="find the login button")
        assert "Goal: find the login button" in result[1]

    @pytest.mark.asyncio
    async def test_reflect_no_screenshot(self):
        fake_ws = FakeWebSocket(responses=[
            {"id": "x", "result": {"image": ""}},
            {"id": "x", "result": {"text": "Page text here"}},
            {"id": "x", "result": {"url": "https://example.com", "title": "Test", "loading": False}},
        ])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_reflect()
        # Should only have 1 block (text), no Image
        assert len(result) == 1
        assert "Page text here" in result[0]

    @pytest.mark.asyncio
    async def test_reflect_with_tab_id(self):
        tiny_jpeg = base64.b64encode(b'\xff\xd8\xff\xe0').decode()
        fake_ws = FakeWebSocket(responses=[
            {"id": "x", "result": {"image": f"data:image/jpeg;base64,{tiny_jpeg}"}},
            {"id": "x", "result": {"text": "text"}},
            {"id": "x", "result": {"url": "https://example.com", "title": "Test", "loading": False}},
        ])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_reflect(tab_id="panel1")
        # All 3 commands should have tab_id
        for sent in fake_ws.sent:
            msg = json.loads(sent)
            assert msg["params"]["tab_id"] == "panel1"

    @pytest.mark.asyncio
    async def test_reflect_truncates_text(self):
        long_text = "x" * 100000
        tiny_jpeg = base64.b64encode(b'\xff\xd8\xff\xe0').decode()
        fake_ws = FakeWebSocket(responses=[
            {"id": "x", "result": {"image": f"data:image/jpeg;base64,{tiny_jpeg}"}},
            {"id": "x", "result": {"text": long_text}},
            {"id": "x", "result": {"url": "https://example.com", "title": "Test", "loading": False}},
        ])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_reflect()
        # Text block should be truncated (50K chars of x's + header lines)
        text_block = result[1]
        # The page text portion should be capped at 50K
        assert len(text_block) < 51000


# ── Phase 11: File Upload ────────────────────────────────────


class TestFileUpload:
    @pytest.mark.asyncio
    async def test_file_upload_basic(self):
        resp = {"success": True, "file_name": "photo.jpg", "file_size": 12345, "file_type": "image/jpeg"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_file_upload("/tmp/photo.jpg", 3)
        data = json.loads(result)
        assert data["success"] is True
        assert data["file_name"] == "photo.jpg"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "file_upload"
        assert msg["params"]["file_path"] == "/tmp/photo.jpg"
        assert msg["params"]["index"] == 3

    @pytest.mark.asyncio
    async def test_file_upload_with_tab_id(self):
        resp = {"success": True, "file_name": "doc.pdf", "file_size": 5000, "file_type": "application/pdf"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_file_upload("/tmp/doc.pdf", 5, tab_id="panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"

    @pytest.mark.asyncio
    async def test_file_upload_with_frame_id(self):
        resp = {"success": True, "file_name": "img.png", "file_size": 1000, "file_type": "image/png"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_file_upload("/tmp/img.png", 2, frame_id=42)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["frame_id"] == 42

    @pytest.mark.asyncio
    async def test_file_upload_file_not_found(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "File not found: /bad/path"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="File not found"):
                await server.browser_file_upload("/bad/path", 0)

    @pytest.mark.asyncio
    async def test_file_upload_wrong_element_type(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Element [0] is <input type=text>, not <input type=\"file\">"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="not.*file"):
                await server.browser_file_upload("/tmp/photo.jpg", 0)


# ── Phase 11: Wait for Download ──────────────────────────────


class TestWaitForDownload:
    @pytest.mark.asyncio
    async def test_wait_for_download_basic(self):
        resp = {
            "success": True, "file_path": "/Users/user/Downloads/report.pdf",
            "file_name": "report.pdf", "file_size": 50000, "content_type": "application/pdf"
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_download()
        data = json.loads(result)
        assert data["success"] is True
        assert data["file_name"] == "report.pdf"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "wait_for_download"
        assert msg["params"]["timeout"] == 60

    @pytest.mark.asyncio
    async def test_wait_for_download_custom_timeout(self):
        resp = {"success": True, "file_path": "/tmp/file.zip", "file_name": "file.zip", "file_size": 100000, "content_type": "application/zip"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_wait_for_download(timeout=30)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["timeout"] == 30

    @pytest.mark.asyncio
    async def test_wait_for_download_with_save_to(self):
        resp = {"success": True, "file_path": "/tmp/saved.pdf", "file_name": "saved.pdf", "file_size": 50000, "content_type": "application/pdf"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_wait_for_download(save_to="/tmp/saved.pdf")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["save_to"] == "/tmp/saved.pdf"

    @pytest.mark.asyncio
    async def test_wait_for_download_timeout(self):
        resp = {"success": False, "error": "Timeout: no download completed within 5s", "timeout": True}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_download(timeout=5)
        data = json.loads(result)
        assert data["success"] is False
        assert data["timeout"] is True

    @pytest.mark.asyncio
    async def test_wait_for_download_failure(self):
        resp = {"success": False, "error": "Network error", "file_path": "/tmp/partial.zip"}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_download()
        data = json.loads(result)
        assert data["success"] is False
        assert "Network error" in data["error"]

    @pytest.mark.asyncio
    async def test_wait_for_download_save_to_error(self):
        resp = {
            "success": True, "file_path": "/Users/user/Downloads/file.pdf",
            "save_to_error": "Permission denied", "file_name": "file.pdf",
            "file_size": 50000, "content_type": "application/pdf"
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_wait_for_download(save_to="/restricted/path")
        data = json.loads(result)
        assert data["success"] is True
        assert "save_to_error" in data


# ── Phase 12: Session URL Routing ─────────────────────────────


class TestGetWsSessionRouting:
    """Tests for URL-based session routing in get_ws()."""

    @pytest.fixture(autouse=True)
    def _no_session_files(self):
        """Prevent session file I/O from leaking between tests."""
        with patch.object(server, "_read_session_file", return_value=""), \
             patch.object(server, "_write_session_file"):
            yield

    @pytest.mark.asyncio
    async def test_new_session_url(self):
        """Without ZENRIPPLE_SESSION_ID, connects to /new."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "abc-1234"}
        )
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(server, "_read_auth_token", return_value="test-token"), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            ws = await server.get_ws()
        assert ws is fake_ws
        mock_connect.assert_called_once_with(
            "ws://localhost:9876/new",
            max_size=10 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=120,
            additional_headers={"Authorization": "Bearer test-token"},
        )
        assert server._session_id == "abc-1234"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_join_session_url(self):
        """With ZENRIPPLE_SESSION_ID set, connects to /session/<id>."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "existing-session"}
        )
        with patch.object(server, "SESSION_ID", "existing-session"), \
             patch.object(server, "_read_auth_token", return_value="test-token"), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            ws = await server.get_ws()
        assert ws is fake_ws
        mock_connect.assert_called_once_with(
            "ws://localhost:9876/session/existing-session",
            max_size=10 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=120,
            additional_headers={"Authorization": "Bearer test-token"},
        )
        assert server._session_id == "existing-session"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_custom_ws_url(self):
        """ZENRIPPLE_WS_URL is respected in URL construction."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket()
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(server, "BROWSER_WS_URL", "ws://remote:1234"), \
             patch.object(server, "_read_auth_token", return_value="test-token"), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            ws = await server.get_ws()
        mock_connect.assert_called_once_with(
            "ws://remote:1234/new",
            max_size=10 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=120,
            additional_headers={"Authorization": "Bearer test-token"},
        )
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_session_id_extracted_from_headers(self):
        """X-ZenRipple-Session header is stored in _session_id."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "sess-xyz"}
        )
        with patch.object(server, "SESSION_ID", ""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
            await server.get_ws()
        assert server._session_id == "sess-xyz"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_session_id_none_when_no_header(self):
        """When no X-ZenRipple-Session header, _session_id stays None."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(response_headers={})
        with patch.object(server, "SESSION_ID", ""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
            await server.get_ws()
        assert server._session_id is None
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    @patch.object(server, "_read_auth_token", return_value="test-token")
    async def test_reconnect_uses_saved_session_id(self, _mock_token):
        """When connection dies, reconnects to same session using saved _session_id."""
        dead_ws = FakeWebSocket()
        dead_ws.closed = True
        server._ws_connection = dead_ws
        server._session_id = "old-session"

        new_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "old-session"}
        )
        with patch.object(server, "SESSION_ID", ""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=new_ws) as mock_connect:
            ws = await server.get_ws()
        assert ws is new_ws
        # Should reconnect to /session/old-session, NOT /new
        mock_connect.assert_called_once_with(
            "ws://localhost:9876/session/old-session",
            max_size=10 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=120,
            additional_headers={"Authorization": "Bearer test-token"},
        )
        assert server._session_id == "old-session"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_reconnect_fallback_to_new_on_404(self):
        """If saved session was destroyed (404), falls back to creating a new one."""
        server._ws_connection = None
        server._session_id = "dead-session"

        new_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "fresh-session"}
        )
        connect_calls = []

        async def mock_connect(url, **kwargs):
            connect_calls.append(url)
            if "dead-session" in url:
                raise Exception("connection rejected")
            return new_ws

        with patch.object(server, "SESSION_ID", ""), \
             patch("websockets.connect", side_effect=mock_connect):
            ws = await server.get_ws()
        assert ws is new_ws
        assert len(connect_calls) == 2
        assert connect_calls[0] == "ws://localhost:9876/session/dead-session"
        assert connect_calls[1] == "ws://localhost:9876/new"
        assert server._session_id == "fresh-session"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_no_response_attribute(self):
        """Gracefully handles ws without response attribute."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket()
        del fake_ws.response  # simulate websockets without response
        with patch.object(server, "SESSION_ID", ""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
            ws = await server.get_ws()
        assert ws is fake_ws
        assert server._session_id is None
        server._ws_connection = None
        server._session_id = None


# ── Auth Token ────────────────────────────────────────────────


class TestAuthToken:
    """Tests for _read_auth_token and auth header passing."""

    @pytest.fixture(autouse=True)
    def _no_session_files(self):
        """Prevent session file I/O from leaking between tests."""
        with patch.object(server, "_read_session_file", return_value=""), \
             patch.object(server, "_write_session_file"):
            yield

    def test_auth_token_from_env(self):
        """ZENRIPPLE_AUTH_TOKEN env var takes priority."""
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": "env-token-123"}):
            assert server._read_auth_token() == "env-token-123"

    def test_auth_token_from_file(self, tmp_path):
        """Reads token from ~/.zenripple/auth file."""
        auth_file = tmp_path / ".zenripple" / "auth"
        auth_file.parent.mkdir()
        auth_file.write_text("file-token-456\n")
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": ""}), \
             patch("zenripple_mcp_server.Path.home", return_value=tmp_path):
            assert server._read_auth_token() == "file-token-456"

    def test_auth_token_missing_file(self, tmp_path):
        """Returns empty string when file doesn't exist."""
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": ""}), \
             patch("zenripple_mcp_server.Path.home", return_value=tmp_path):
            assert server._read_auth_token() == ""

    def test_auth_token_env_overrides_file(self, tmp_path):
        """Env var takes priority even when file exists."""
        auth_file = tmp_path / ".zenripple" / "auth"
        auth_file.parent.mkdir()
        auth_file.write_text("file-token\n")
        with patch.dict(os.environ, {"ZENRIPPLE_AUTH_TOKEN": "env-token"}), \
             patch("zenripple_mcp_server.Path.home", return_value=tmp_path):
            assert server._read_auth_token() == "env-token"

    @pytest.mark.asyncio
    async def test_empty_token_sends_no_header(self):
        """When no token available, additional_headers is empty dict."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket()
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(server, "_read_auth_token", return_value=""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            await server.get_ws()
        _, kwargs = mock_connect.call_args
        assert kwargs["additional_headers"] == {}
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_token_sends_bearer_header(self):
        """When token is available, Authorization header is passed."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket()
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(server, "_read_auth_token", return_value="my-secret"), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            await server.get_ws()
        _, kwargs = mock_connect.call_args
        assert kwargs["additional_headers"] == {"Authorization": "Bearer my-secret"}
        server._ws_connection = None
        server._session_id = None


# ── Auto-Session (Terminal-Keyed File Persistence) ────────────


class TestCallerKey:
    """Tests for _get_caller_key() terminal identification."""

    def setup_method(self):
        """Clear caller key cache before each test so env changes take effect."""
        session_file._caller_key_cache = None

    def test_tmux_pane(self):
        with patch.dict(os.environ, {"TMUX_PANE": "%17"}, clear=False):
            key = session_file.get_caller_key()
            assert len(key) == 16
            assert key.isalnum()

    def test_iterm_session(self):
        with patch.dict(os.environ, {"ITERM_SESSION_ID": "w0t0p0:abc"}, clear=False):
            session_file._caller_key_cache = None
            key = session_file.get_caller_key()
            assert len(key) == 16

    def test_explicit_caller_id_takes_priority(self):
        """ZENRIPPLE_CALLER_ID overrides terminal env vars."""
        with patch.dict(os.environ, {
            "ZENRIPPLE_CALLER_ID": "subagent-1",
            "TMUX_PANE": "%17",
        }, clear=False):
            session_file._caller_key_cache = None
            key = session_file.get_caller_key()
        with patch.dict(os.environ, {
            "ZENRIPPLE_CALLER_ID": "subagent-1",
        }, clear=False):
            # Remove TMUX_PANE — should get same key since CALLER_ID matches
            env = os.environ.copy()
            env.pop("TMUX_PANE", None)
            with patch.dict(os.environ, env, clear=True):
                session_file._caller_key_cache = None
                key2 = session_file.get_caller_key()
        assert key == key2

    def test_different_panes_different_keys(self):
        with patch.dict(os.environ, {"TMUX_PANE": "%17"}, clear=False):
            session_file._caller_key_cache = None
            key1 = session_file.get_caller_key()
        with patch.dict(os.environ, {"TMUX_PANE": "%18"}, clear=False):
            session_file._caller_key_cache = None
            key2 = session_file.get_caller_key()
        assert key1 != key2

    def test_default_fallback(self):
        """When no terminal env var is set, returns 'default'."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("ZENRIPPLE_CALLER_ID", "TMUX_PANE", "ITERM_SESSION_ID",
                            "TERM_SESSION_ID", "VSCODE_PID", "WINDOWID")}
        with patch.dict(os.environ, env, clear=True):
            session_file._caller_key_cache = None
            assert session_file.get_caller_key() == "default"

    def test_whitespace_only_skipped(self):
        """Whitespace-only env var values are skipped."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("ZENRIPPLE_CALLER_ID", "TMUX_PANE", "ITERM_SESSION_ID",
                            "TERM_SESSION_ID", "VSCODE_PID", "WINDOWID")}
        env["TMUX_PANE"] = "   "
        with patch.dict(os.environ, env, clear=True):
            session_file._caller_key_cache = None
            assert session_file.get_caller_key() == "default"


class TestAutoSession:
    """Tests for session file read/write and get_ws() integration."""

    def test_read_session_file(self, tmp_path):
        """Reads session ID from the caller's file."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        with patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="abc123"):
            (sessions / "abc123").write_text("sess-uuid-1\n")
            assert server._read_session_file() == "sess-uuid-1"

    def test_read_session_file_missing(self, tmp_path):
        """Returns empty string when file doesn't exist."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        with patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="nonexistent"):
            assert server._read_session_file() == ""

    def test_write_session_file(self, tmp_path):
        """Writes session ID to the caller's file."""
        sessions = tmp_path / "sessions"
        with patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="abc123"):
            server._write_session_file("new-sess-id")
        assert (sessions / "abc123").read_text().strip() == "new-sess-id"

    def test_write_creates_directory(self, tmp_path):
        """Creates sessions directory if it doesn't exist."""
        sessions = tmp_path / "deep" / "sessions"
        with patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="key1"):
            server._write_session_file("sess-123")
        assert sessions.exists()
        assert (sessions / "key1").read_text().strip() == "sess-123"

    def test_write_permission_error_silent(self, tmp_path):
        """Write failure is silently ignored."""
        sessions = tmp_path / "readonly"
        sessions.mkdir()
        sessions.chmod(0o444)
        try:
            with patch.object(session_file, "SESSIONS_DIR", sessions / "sub"), \
                 patch.object(session_file, "get_caller_key", return_value="key1"):
                # Should not raise
                server._write_session_file("sess-123")
        finally:
            sessions.chmod(0o755)

    def test_delete_session_file(self, tmp_path):
        """Deletes the caller's session file."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "delkey").write_text("sess-to-delete\n")
        with patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="delkey"):
            session_file.delete_session_file()
        assert not (sessions / "delkey").exists()

    def test_delete_session_file_missing(self, tmp_path):
        """Deleting nonexistent file doesn't raise."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        with patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="nope"):
            session_file.delete_session_file()  # should not raise

    @pytest.mark.asyncio
    async def test_session_close_deletes_file(self, tmp_path):
        """browser_session_close removes the session file when not using env var."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "closekey").write_text("closing-session\n")
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="closekey"), \
             patch.object(server, "browser_command", new_callable=AsyncMock,
                          return_value={"success": True}):
            await server.browser_session_close()
        assert not (sessions / "closekey").exists()

    @pytest.mark.asyncio
    async def test_session_close_skips_file_with_env_var(self, tmp_path):
        """browser_session_close does NOT delete file when ZENRIPPLE_SESSION_ID is set."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "envkey").write_text("pinned-session\n")
        with patch.object(server, "SESSION_ID", "pinned-session"), \
             patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="envkey"), \
             patch.object(server, "browser_command", new_callable=AsyncMock,
                          return_value={"success": True}):
            await server.browser_session_close()
        # File should still exist — env var sessions don't touch the file
        assert (sessions / "envkey").exists()

    @pytest.mark.asyncio
    async def test_session_close_clears_memory(self, tmp_path):
        """browser_session_close clears _session_id and _ws_connection."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "memkey").write_text("mem-session\n")
        server._session_id = "mem-session"
        server._ws_connection = "fake-ws"
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="memkey"), \
             patch.object(server, "browser_command", new_callable=AsyncMock,
                          return_value={"success": True}):
            await server.browser_session_close()
        assert server._session_id is None
        assert server._ws_connection is None

    @pytest.mark.asyncio
    async def test_session_file_used_in_get_ws(self, tmp_path):
        """get_ws() reads session ID from file when no env var or in-memory ID."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "testkey").write_text("file-session-id\n")

        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "file-session-id"}
        )
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="testkey"), \
             patch.object(server, "_read_auth_token", return_value=""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            ws = await server.get_ws()
        assert ws is fake_ws
        mock_connect.assert_called_once()
        url = mock_connect.call_args[0][0]
        assert url == "ws://localhost:9876/session/file-session-id"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_session_file_written_after_connect(self, tmp_path):
        """get_ws() writes session ID to file after successful connect."""
        sessions = tmp_path / "sessions"

        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "new-session-xyz"}
        )
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="writekey"), \
             patch.object(server, "_read_auth_token", return_value=""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
            await server.get_ws()
        assert (sessions / "writekey").read_text().strip() == "new-session-xyz"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_session_file_not_written_when_env_var_set(self, tmp_path):
        """Explicit ZENRIPPLE_SESSION_ID skips file write."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()

        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "explicit-sess"}
        )
        with patch.object(server, "SESSION_ID", "explicit-sess"), \
             patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="envkey"), \
             patch.object(server, "_read_auth_token", return_value=""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
            await server.get_ws()
        assert not (sessions / "envkey").exists()
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_stale_file_session_falls_back_to_new(self, tmp_path):
        """If file session is expired, falls back to /new and updates file."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "stalekey").write_text("dead-session\n")

        server._ws_connection = None
        server._session_id = None

        new_ws = FakeWebSocket(
            response_headers={"X-ZenRipple-Session": "fresh-session"}
        )

        async def mock_connect(url, **kwargs):
            if "dead-session" in url:
                raise Exception("session not found")
            return new_ws

        with patch.object(server, "SESSION_ID", ""), \
             patch.object(session_file, "SESSIONS_DIR", sessions), \
             patch.object(session_file, "get_caller_key", return_value="stalekey"), \
             patch.object(server, "_read_auth_token", return_value=""), \
             patch("websockets.connect", side_effect=mock_connect):
            ws = await server.get_ws()
        assert ws is new_ws
        # File should be updated with the fresh session
        assert (sessions / "stalekey").read_text().strip() == "fresh-session"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_multiple_callers_different_files(self, tmp_path):
        """Two different callers get different session files."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()

        for caller, sess_id in [("caller-a", "sess-a"), ("caller-b", "sess-b")]:
            server._ws_connection = None
            server._session_id = None
            fake_ws = FakeWebSocket(
                response_headers={"X-ZenRipple-Session": sess_id}
            )
            with patch.object(server, "SESSION_ID", ""), \
                 patch.object(session_file, "SESSIONS_DIR", sessions), \
                 patch.object(session_file, "get_caller_key", return_value=caller), \
                 patch.object(server, "_read_auth_token", return_value=""), \
                 patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
                await server.get_ws()

        assert (sessions / "caller-a").read_text().strip() == "sess-a"
        assert (sessions / "caller-b").read_text().strip() == "sess-b"
        server._ws_connection = None
        server._session_id = None


# ── Phase 12: Session Management Tools ────────────────────────


class TestSessionManagement:
    """Tests for session_info, session_close, list_sessions MCP tools."""

    @pytest.mark.asyncio
    async def test_session_info(self):
        resp = {
            "session_id": "abc-1234",
            "workspace_name": "ZenRipple",
            "workspace_id": "ws-uuid",
            "connection_id": "conn-1",
            "connection_count": 2,
            "tab_count": 3,
            "claimed_tab_count": 1,
            "color_index": 2,
            "name": "test-session",
            "created_at": 1700000000000,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_info()
        data = json.loads(result)
        assert data["session_id"] == "abc-1234"
        assert data["workspace_name"] == "ZenRipple"
        assert data["connection_count"] == 2
        assert data["tab_count"] == 3
        assert data["claimed_tab_count"] == 1
        assert data["color_index"] == 2
        assert data["name"] == "test-session"
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "session_info"

    @pytest.mark.asyncio
    async def test_session_close(self):
        resp = {"success": True, "session_id": "abc-1234", "tabs_closed": 3, "tabs_released": 2}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_close()
        data = json.loads(result)
        assert data["success"] is True
        assert data["tabs_closed"] == 3
        assert data["tabs_released"] == 2
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "session_close"

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        resp = [
            {
                "session_id": "abc-1234",
                "workspace_name": "ZenRipple",
                "connection_count": 1,
                "tab_count": 2,
                "color_index": 0,
                "name": "researcher",
                "created_at": 1700000000000,
            },
            {
                "session_id": "def-5678",
                "workspace_name": "ZenRipple",
                "connection_count": 3,
                "tab_count": 5,
                "color_index": 1,
                "name": None,
                "created_at": 1700001000000,
            },
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_sessions()
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["session_id"] == "abc-1234"
        assert data[0]["color_index"] == 0
        assert data[1]["session_id"] == "def-5678"
        assert data[1]["color_index"] == 1
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "list_sessions"

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": []}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_sessions()
        data = json.loads(result)
        assert data == []

    @pytest.mark.asyncio
    async def test_session_info_error(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Session expired"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="Session expired"):
                await server.browser_session_info()

    @pytest.mark.asyncio
    async def test_session_close_already_closed(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Session not found"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="Session not found"):
                await server.browser_session_close()


class TestSessionNaming:
    """Tests for browser_set_session_name tool and name in session/list responses."""

    @pytest.mark.asyncio
    async def test_set_session_name(self):
        resp = {"name": "researcher", "other_session_names": ["coder", "reviewer"]}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_set_session_name(name="researcher")
        data = json.loads(result)
        assert data["name"] == "researcher"
        assert "coder" in data["other_session_names"]
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "set_session_name"
        assert msg["params"]["name"] == "researcher"

    @pytest.mark.asyncio
    async def test_set_session_name_too_long_error(self):
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "name must be at most 32 characters"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="name must be at most 32 characters"):
                await server.browser_set_session_name(name="a" * 50)

    @pytest.mark.asyncio
    async def test_session_info_includes_name(self):
        resp = {
            "session_id": "abc-1234",
            "workspace_name": "ZenRipple",
            "workspace_id": "ws-uuid",
            "connection_id": "conn-1",
            "connection_count": 1,
            "tab_count": 2,
            "claimed_tab_count": 0,
            "color_index": 0,
            "created_at": 1700000000000,
            "name": "researcher",
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_info()
        data = json.loads(result)
        assert data["name"] == "researcher"

    @pytest.mark.asyncio
    async def test_session_info_name_null_when_unset(self):
        resp = {
            "session_id": "abc-1234",
            "workspace_name": "ZenRipple",
            "workspace_id": "ws-uuid",
            "connection_id": "conn-1",
            "connection_count": 1,
            "tab_count": 0,
            "claimed_tab_count": 0,
            "color_index": 0,
            "created_at": 1700000000000,
            "name": None,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_info()
        data = json.loads(result)
        assert data["name"] is None

    @pytest.mark.asyncio
    async def test_list_sessions_includes_names(self):
        resp = [
            {
                "session_id": "abc-1234",
                "workspace_name": "ZenRipple",
                "connection_count": 1,
                "tab_count": 2,
                "color_index": 0,
                "created_at": 1700000000000,
                "name": "researcher",
            },
            {
                "session_id": "def-5678",
                "workspace_name": "ZenRipple",
                "connection_count": 1,
                "tab_count": 1,
                "color_index": 1,
                "created_at": 1700001000000,
                "name": None,
            },
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_sessions()
        data = json.loads(result)
        assert data[0]["name"] == "researcher"
        assert data[1]["name"] is None

    @pytest.mark.asyncio
    async def test_set_session_name_clear(self):
        resp = {"name": None, "other_session_names": ["coder"]}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_set_session_name(name="")
        data = json.loads(result)
        assert data["name"] is None
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["name"] == ""

    @pytest.mark.asyncio
    async def test_set_session_name_other_sessions_empty(self):
        resp = {"name": "solo-agent", "other_session_names": []}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_set_session_name(name="solo-agent")
        data = json.loads(result)
        assert data["name"] == "solo-agent"
        assert data["other_session_names"] == []


# ── Tab Claiming (Phase 13) ──────────────────────────────────────


class TestListWorkspaceTabs:
    """Tests for browser_list_workspace_tabs tool."""

    @pytest.mark.asyncio
    async def test_lists_all_workspace_tabs(self):
        """Should return all tabs in the workspace including unclaimed ones."""
        resp = [
            {
                "tab_id": "panel1",
                "title": "Agent Tab",
                "url": "https://agent.example.com",
                "ownership": "owned",
                "is_mine": True,
            },
            {
                "tab_id": "panel2",
                "title": "User Tab",
                "url": "https://user.example.com",
                "ownership": "unclaimed",
                "is_mine": False,
            },
            {
                "tab_id": "panel3",
                "title": "Stale Tab",
                "url": "https://stale.example.com",
                "ownership": "stale",
                "is_mine": False,
                "owner_session_id": "old-session-id",
            },
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_workspace_tabs()
        data = json.loads(result)
        assert len(data) == 3
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "list_workspace_tabs"

    @pytest.mark.asyncio
    async def test_ownership_field_values(self):
        """Each tab should have a valid ownership field."""
        resp = [
            {"tab_id": "p1", "title": "T1", "url": "u1", "ownership": "owned", "is_mine": True},
            {"tab_id": "p2", "title": "T2", "url": "u2", "ownership": "unclaimed", "is_mine": False},
            {"tab_id": "p3", "title": "T3", "url": "u3", "ownership": "stale", "is_mine": False,
             "owner_session_id": "stale-sess"},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_workspace_tabs()
        data = json.loads(result)
        statuses = {t["ownership"] for t in data}
        assert statuses == {"owned", "unclaimed", "stale"}

    @pytest.mark.asyncio
    async def test_is_mine_field(self):
        """The is_mine field should indicate ownership by calling session."""
        resp = [
            {"tab_id": "p1", "title": "My Tab", "url": "u1", "ownership": "owned", "is_mine": True},
            {"tab_id": "p2", "title": "Not Mine", "url": "u2", "ownership": "owned", "is_mine": False,
             "owner_session_id": "other-session"},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_workspace_tabs()
        data = json.loads(result)
        assert data[0]["is_mine"] is True
        assert data[1]["is_mine"] is False

    @pytest.mark.asyncio
    async def test_claimed_field_for_owned_tabs(self):
        """Tabs owned by calling session should include claimed status."""
        resp = [
            {"tab_id": "p1", "title": "Created", "url": "u1", "ownership": "owned",
             "is_mine": True, "claimed": False},
            {"tab_id": "p2", "title": "Claimed", "url": "u2", "ownership": "owned",
             "is_mine": True, "claimed": True},
            {"tab_id": "p3", "title": "Foreign", "url": "u3", "ownership": "unclaimed",
             "is_mine": False},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_workspace_tabs()
        data = json.loads(result)
        assert data[0]["claimed"] is False
        assert data[1]["claimed"] is True
        assert "claimed" not in data[2]

    @pytest.mark.asyncio
    async def test_empty_workspace(self):
        """Should return empty list when workspace has no tabs."""
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": []}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_workspace_tabs()
        data = json.loads(result)
        assert data == []

    @pytest.mark.asyncio
    async def test_owner_session_id_only_for_foreign_tabs(self):
        """owner_session_id should only appear for tabs NOT owned by the caller."""
        resp = [
            {"tab_id": "p1", "title": "Mine", "url": "u1", "ownership": "owned", "is_mine": True},
            {"tab_id": "p2", "title": "Foreign", "url": "u2", "ownership": "stale", "is_mine": False,
             "owner_session_id": "foreign-sess"},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_workspace_tabs()
        data = json.loads(result)
        assert "owner_session_id" not in data[0]
        assert data[1]["owner_session_id"] == "foreign-sess"

    @pytest.mark.asyncio
    async def test_error_propagation(self):
        """Should propagate browser errors."""
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Workspace not found"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="Workspace not found"):
                await server.browser_list_workspace_tabs()


class TestClaimTab:
    """Tests for browser_claim_tab tool."""

    @pytest.mark.asyncio
    async def test_claim_unclaimed_tab(self):
        """Should successfully claim an unclaimed (user-opened) tab."""
        resp = {
            "success": True,
            "tab_id": "panel2",
            "url": "https://user.example.com",
            "title": "User Tab",
            "persist": True,
            "previous_owner": None,
            "was_stale": False,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_claim_tab("panel2")
        data = json.loads(result)
        assert data["success"] is True
        assert data["tab_id"] == "panel2"
        assert data["persist"] is True
        assert data["previous_owner"] is None
        assert data["was_stale"] is False
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "claim_tab"
        assert msg["params"]["tab_id"] == "panel2"

    @pytest.mark.asyncio
    async def test_claim_stale_tab(self):
        """Should successfully claim a tab from a stale session."""
        resp = {
            "success": True,
            "tab_id": "panel3",
            "url": "https://stale.example.com",
            "title": "Stale Tab",
            "persist": True,
            "previous_owner": "old-session-123",
            "was_stale": True,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_claim_tab("panel3")
        data = json.loads(result)
        assert data["success"] is True
        assert data["was_stale"] is True
        assert data["previous_owner"] == "old-session-123"

    @pytest.mark.asyncio
    async def test_claim_already_owned_tab(self):
        """Claiming a tab already owned by calling session should return already_owned."""
        resp = {
            "success": True,
            "tab_id": "panel1",
            "already_owned": True,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_claim_tab("panel1")
        data = json.loads(result)
        assert data["success"] is True
        assert data["already_owned"] is True

    @pytest.mark.asyncio
    async def test_claim_actively_owned_tab_fails(self):
        """Claiming a tab actively owned by another session should fail."""
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Tab is actively owned by session abc. Cannot claim tabs from active sessions."}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="actively owned"):
                await server.browser_claim_tab("panel1")

    @pytest.mark.asyncio
    async def test_claim_nonexistent_tab_fails(self):
        """Claiming a tab that doesn't exist should fail."""
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Tab not found in workspace: bad-id"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="Tab not found"):
                await server.browser_claim_tab("bad-id")

    @pytest.mark.asyncio
    async def test_claim_by_url(self):
        """Should support claiming tabs by URL."""
        resp = {
            "success": True,
            "tab_id": "panel-auto",
            "url": "https://example.com/page",
            "title": "Example",
            "previous_owner": None,
            "was_stale": False,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_claim_tab("https://example.com/page")
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "https://example.com/page"

    @pytest.mark.asyncio
    async def test_claim_respects_session_tab_limit(self):
        """Should fail if session tab limit would be exceeded."""
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Session tab limit exceeded: 40/40 open, requested 1 more"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="tab limit exceeded"):
                await server.browser_claim_tab("panel5")

    @pytest.mark.asyncio
    async def test_claim_returns_tab_metadata(self):
        """Claimed tab response should include url and title."""
        resp = {
            "success": True,
            "tab_id": "panel-x",
            "url": "https://docs.example.com",
            "title": "Documentation",
            "previous_owner": None,
            "was_stale": False,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_claim_tab("panel-x")
        data = json.loads(result)
        assert data["url"] == "https://docs.example.com"
        assert data["title"] == "Documentation"


class TestTabClaimingWorkflow:
    """Integration-style tests verifying the list -> claim -> use workflow."""

    @pytest.mark.asyncio
    async def test_list_then_claim_workflow(self):
        """Simulate: list workspace tabs, find unclaimed, claim it."""
        list_resp = [
            {"tab_id": "agent-tab", "title": "Agent", "url": "https://a.com",
             "ownership": "owned", "is_mine": True},
            {"tab_id": "user-tab", "title": "User Page", "url": "https://b.com",
             "ownership": "unclaimed", "is_mine": False},
        ]
        claim_resp = {
            "success": True,
            "tab_id": "user-tab",
            "url": "https://b.com",
            "title": "User Page",
            "previous_owner": None,
            "was_stale": False,
        }
        # Step 1: list workspace tabs
        fake_ws1 = FakeWebSocket(responses=[{"id": "x", "result": list_resp}])
        with patch.object(server, "get_ws", return_value=fake_ws1):
            list_result = await server.browser_list_workspace_tabs()
        tabs = json.loads(list_result)
        unclaimed = [t for t in tabs if t["ownership"] == "unclaimed"]
        assert len(unclaimed) == 1
        assert unclaimed[0]["tab_id"] == "user-tab"

        # Step 2: claim the unclaimed tab
        fake_ws2 = FakeWebSocket(responses=[{"id": "x", "result": claim_resp}])
        with patch.object(server, "get_ws", return_value=fake_ws2):
            claim_result = await server.browser_claim_tab(unclaimed[0]["tab_id"])
        claimed = json.loads(claim_result)
        assert claimed["success"] is True
        assert claimed["tab_id"] == "user-tab"

    @pytest.mark.asyncio
    async def test_claim_stale_from_another_agent(self):
        """Simulate: agent B claims a stale tab from agent A."""
        list_resp = [
            {"tab_id": "stale-tab", "title": "Stale Research", "url": "https://research.com",
             "ownership": "stale", "is_mine": False, "owner_session_id": "agent-a-session"},
        ]
        claim_resp = {
            "success": True,
            "tab_id": "stale-tab",
            "url": "https://research.com",
            "title": "Stale Research",
            "previous_owner": "agent-a-session",
            "was_stale": True,
        }
        # List and verify stale
        fake_ws1 = FakeWebSocket(responses=[{"id": "x", "result": list_resp}])
        with patch.object(server, "get_ws", return_value=fake_ws1):
            list_result = await server.browser_list_workspace_tabs()
        tabs = json.loads(list_result)
        stale_tabs = [t for t in tabs if t["ownership"] == "stale"]
        assert len(stale_tabs) == 1

        # Claim the stale tab
        fake_ws2 = FakeWebSocket(responses=[{"id": "x", "result": claim_resp}])
        with patch.object(server, "get_ws", return_value=fake_ws2):
            claim_result = await server.browser_claim_tab("stale-tab")
        claimed = json.loads(claim_result)
        assert claimed["previous_owner"] == "agent-a-session"
        assert claimed["was_stale"] is True

    @pytest.mark.asyncio
    async def test_only_claimable_tabs_are_claimable(self):
        """Only unclaimed and stale tabs should be claimable; owned tabs should fail."""
        list_resp = [
            {"tab_id": "active-tab", "title": "Active", "url": "https://active.com",
             "ownership": "owned", "is_mine": False, "owner_session_id": "other-active"},
        ]
        fake_ws1 = FakeWebSocket(responses=[{"id": "x", "result": list_resp}])
        with patch.object(server, "get_ws", return_value=fake_ws1):
            list_result = await server.browser_list_workspace_tabs()
        tabs = json.loads(list_result)
        assert tabs[0]["ownership"] == "owned"

        # Attempt to claim should fail
        fake_ws2 = FakeWebSocket(
            responses=[{"id": "x", "error": {"message": "Tab is actively owned by session other-active. Cannot claim tabs from active sessions."}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws2):
            with pytest.raises(Exception, match="actively owned"):
                await server.browser_claim_tab("active-tab")

    @pytest.mark.asyncio
    async def test_mixed_workspace_tabs_filtering(self):
        """Workspace should contain a mix of owned, unclaimed, and stale tabs."""
        resp = [
            {"tab_id": "t1", "title": "My Tab 1", "url": "u1", "ownership": "owned", "is_mine": True},
            {"tab_id": "t2", "title": "My Tab 2", "url": "u2", "ownership": "owned", "is_mine": True},
            {"tab_id": "t3", "title": "User Tab", "url": "u3", "ownership": "unclaimed", "is_mine": False},
            {"tab_id": "t4", "title": "Other Agent", "url": "u4", "ownership": "owned", "is_mine": False,
             "owner_session_id": "sess-b"},
            {"tab_id": "t5", "title": "Dead Agent", "url": "u5", "ownership": "stale", "is_mine": False,
             "owner_session_id": "sess-c"},
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_workspace_tabs()
        data = json.loads(result)

        mine = [t for t in data if t["is_mine"]]
        claimable = [t for t in data if t["ownership"] in ("unclaimed", "stale")]
        not_claimable = [t for t in data if t["ownership"] == "owned" and not t["is_mine"]]

        assert len(mine) == 2
        assert len(claimable) == 2  # t3 (unclaimed) + t5 (stale)
        assert len(not_claimable) == 1  # t4 (active other agent)


# ── Screenshot Dimension Metadata ──────────────────────────────────


class TestScreenshotDimensions:
    @pytest.mark.asyncio
    async def test_returns_scale_factor_when_downscaled(self):
        """When viewport > screenshot, scale factor is shown."""
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568,
                        "height": 882,
                        "viewport_width": 1920,
                        "viewport_height": 1080,
                    },
                }
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_screenshot()
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], Image)
        assert "1920x1080" in result[1]
        assert "Scale factor" in result[1]

    @pytest.mark.asyncio
    async def test_no_scale_factor_when_no_downscale(self):
        """When viewport == screenshot, no scale factor is shown."""
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1200,
                        "height": 800,
                        "viewport_width": 1200,
                        "viewport_height": 800,
                    },
                }
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_screenshot()
        assert isinstance(result, list)
        assert "Scale factor" not in result[1]

    @pytest.mark.asyncio
    async def test_caches_dimensions(self):
        """Screenshot caches dimensions for auto-scaling."""
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568,
                        "height": 882,
                        "viewport_width": 1920,
                        "viewport_height": 1080,
                    },
                }
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_screenshot()
        dims = server._last_screenshot_dims.get("")
        assert dims is not None
        assert dims["sw"] == 1568
        assert dims["vw"] == 1920


# ── Click Coordinate Auto-Scaling ──────────────────────────────────


class TestClickCoordinatesScaling:
    @pytest.fixture(autouse=True)
    def _save_restore_dims(self):
        """Save and restore _last_screenshot_dims to prevent test leakage."""
        orig = dict(server._last_screenshot_dims)
        yield
        server._last_screenshot_dims.clear()
        server._last_screenshot_dims.update(orig)

    @pytest.mark.asyncio
    async def test_auto_scales_when_downscaled(self):
        """Coordinates scaled from screenshot-space to viewport-space."""
        server._last_screenshot_dims[""] = {
            "sw": 1568, "sh": 882, "vw": 1920, "vh": 1080
        }
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True, "tag": "button", "text": "OK"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_click_coordinates(784, 441)
        msg = json.loads(fake_ws.sent[0])
        # Viewport scaling only: 784*(1920/1568)=960, 441*(1080/882)=540
        assert msg["params"]["x"] == 960
        assert msg["params"]["y"] == 540

    @pytest.mark.asyncio
    async def test_no_scaling_when_dimensions_match(self):
        """No scaling when screenshot and viewport dims match."""
        server._last_screenshot_dims[""] = {
            "sw": 1200, "sh": 800, "vw": 1200, "vh": 800
        }
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True, "tag": "div", "text": ""}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_click_coordinates(600, 400)
        msg = json.loads(fake_ws.sent[0])
        # Pass-through: no scaling needed
        assert msg["params"]["x"] == 600
        assert msg["params"]["y"] == 400

    @pytest.mark.asyncio
    async def test_no_viewport_scaling_without_cache(self):
        """Pass-through when no screenshot dimensions cached."""
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True, "tag": "a", "text": ""}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_click_coordinates(500, 300)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["x"] == 500
        assert msg["params"]["y"] == 300

    @pytest.mark.asyncio
    async def test_scales_per_tab(self):
        """Different tabs get different viewport scaling."""
        server._last_screenshot_dims["tab-a"] = {
            "sw": 1568, "sh": 882, "vw": 1920, "vh": 1080
        }
        server._last_screenshot_dims["tab-b"] = {
            "sw": 800, "sh": 600, "vw": 800, "vh": 600
        }
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "tag": "a", "text": ""}},
                {"id": "x", "result": {"success": True, "tag": "a", "text": ""}},
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_click_coordinates(100, 100, tab_id="tab-a")
            await server.browser_click_coordinates(100, 100, tab_id="tab-b")
        msg_a = json.loads(fake_ws.sent[0])
        msg_b = json.loads(fake_ws.sent[1])
        # tab-a: viewport scaling 100*(1920/1568)=122
        assert msg_a["params"]["x"] == 122
        # tab-b: no scaling (sw==vw)
        assert msg_b["params"]["x"] == 100


# ── Grounding Coordinate Parser ──────────────────────────────────


class TestParseGroundingCoordinates:
    def test_absolute_coordinates(self):
        x, y = server._parse_grounding_coordinates("(523, 312)", 1568, 882)
        assert (x, y) == (523, 312)

    def test_absolute_square_brackets(self):
        x, y = server._parse_grounding_coordinates("[523, 312]", 1568, 882)
        assert (x, y) == (523, 312)

    def test_normalized_floats(self):
        x, y = server._parse_grounding_coordinates("(0.5, 0.3)", 1568, 882)
        assert (x, y) == (784, 265)

    def test_normalized_floats_one_point_zero(self):
        """Normalized regex matches 1.0 (bottom-right corner)."""
        x, y = server._parse_grounding_coordinates("(1.0, 1.0)", 1568, 882)
        assert (x, y) == (1568, 882)

    def test_normalized_floats_zero_point_zero(self):
        """Normalized regex matches 0.0 (top-left corner)."""
        x, y = server._parse_grounding_coordinates("(0.0, 0.0)", 1568, 882)
        assert (x, y) == (0, 0)

    def test_bounding_box_center(self):
        x, y = server._parse_grounding_coordinates("[100, 200, 300, 400]", 1568, 882)
        assert (x, y) == (200, 300)

    def test_qwen_box_token(self):
        x, y = server._parse_grounding_coordinates(
            "<|box_start|>(456,219)<|box_end|>", 1568, 882
        )
        assert (x, y) == (456, 219)

    def test_point_tag(self):
        x, y = server._parse_grounding_coordinates(
            "<point>100 200</point>", 1568, 882
        )
        assert (x, y) == (100, 200)

    def test_unparseable(self):
        x, y = server._parse_grounding_coordinates("no coordinates here", 1568, 882)
        assert x is None
        assert y is None


# ── API Key Persistence ──────────────────────────────────────────


@pytest.fixture(autouse=False)
def reset_grounding_globals():
    """Reset grounding module globals before/after each test that uses this fixture."""
    orig_key = server._GROUNDING_API_KEY
    orig_synced = server._GROUNDING_KEY_SYNCED
    orig_dims = dict(server._last_screenshot_dims)
    orig_coord_mode = server._GROUNDING_COORD_MODE
    # Use "absolute" for existing tests written for Qwen2.5-VL-style responses
    server._GROUNDING_COORD_MODE = "absolute"
    yield
    server._GROUNDING_API_KEY = orig_key
    server._GROUNDING_KEY_SYNCED = orig_synced
    server._GROUNDING_COORD_MODE = orig_coord_mode
    server._last_screenshot_dims.clear()
    server._last_screenshot_dims.update(orig_dims)


class TestEnsureGroundingKey:
    @pytest.mark.asyncio
    async def test_env_var_stored_to_config(self, reset_grounding_globals):
        """When env var is set, key is stored to browser config."""
        server._GROUNDING_API_KEY = "sk-test-123"
        server._GROUNDING_KEY_SYNCED = False
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"success": True, "key": "openrouter_api_key"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            key = await server._ensure_grounding_key()
        assert key == "sk-test-123"
        assert server._GROUNDING_KEY_SYNCED is True
        # Verify set_config was called to store the key
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "set_config"
        assert msg["params"]["key"] == "openrouter_api_key"
        assert msg["params"]["value"] == "sk-test-123"

    @pytest.mark.asyncio
    async def test_env_var_not_synced_on_set_failure(self, reset_grounding_globals):
        """When set_config fails, _GROUNDING_KEY_SYNCED stays False so retry happens."""
        server._GROUNDING_API_KEY = "sk-test-123"
        server._GROUNDING_KEY_SYNCED = False
        with patch.object(server, "browser_command", side_effect=Exception("ws error")):
            key = await server._ensure_grounding_key()
        assert key == "sk-test-123"
        # Should NOT be synced since set_config failed
        assert server._GROUNDING_KEY_SYNCED is False

    @pytest.mark.asyncio
    async def test_loads_from_config_when_no_env(self, reset_grounding_globals):
        """When no env var, key is loaded from browser config."""
        server._GROUNDING_API_KEY = ""
        server._GROUNDING_KEY_SYNCED = False
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"key": "openrouter_api_key", "value": "sk-stored-456"}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            key = await server._ensure_grounding_key()
        assert key == "sk-stored-456"
        assert server._GROUNDING_API_KEY == "sk-stored-456"
        assert server._GROUNDING_KEY_SYNCED is True

    @pytest.mark.asyncio
    async def test_not_synced_on_get_failure(self, reset_grounding_globals):
        """When get_config fails, _GROUNDING_KEY_SYNCED stays False so retry happens."""
        server._GROUNDING_API_KEY = ""
        server._GROUNDING_KEY_SYNCED = False
        with patch.object(server, "browser_command", side_effect=Exception("ws error")):
            key = await server._ensure_grounding_key()
        assert key == ""
        assert server._GROUNDING_KEY_SYNCED is False

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_key_anywhere(self, reset_grounding_globals):
        """When no env var and no stored key, returns empty string."""
        server._GROUNDING_API_KEY = ""
        server._GROUNDING_KEY_SYNCED = False
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"key": "openrouter_api_key", "value": ""}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            key = await server._ensure_grounding_key()
        assert key == ""

    @pytest.mark.asyncio
    async def test_skips_sync_when_already_synced(self, reset_grounding_globals):
        """When already synced, returns cached key without browser call."""
        server._GROUNDING_API_KEY = "sk-cached"
        server._GROUNDING_KEY_SYNCED = True
        key = await server._ensure_grounding_key()
        assert key == "sk-cached"


# ── Grounded Click ───────────────────────────────────────────────


class TestGroundedClick:
    @pytest.mark.asyncio
    async def test_no_key_returns_error(self, reset_grounding_globals):
        """Returns error when no API key available."""
        server._GROUNDING_API_KEY = ""
        server._GROUNDING_KEY_SYNCED = True
        result = await server.browser_grounded_click("the button")
        assert "OPENROUTER_API_KEY" in result

    @pytest.mark.asyncio
    async def test_uses_click_native(self, reset_grounding_globals):
        """Grounded click uses click_native for real mouse events through iframes."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                # screenshot
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
                # click_native
                {"id": "x", "result": {"success": True, "x": 400, "y": 300}},
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(400, 300)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_click("the link")

        assert "Grounded click" in result
        click_msg = json.loads(fake_ws.sent[1])
        assert click_msg["method"] == "click_native"
        assert click_msg["params"]["x"] == 400
        assert click_msg["params"]["y"] == 300

    @pytest.mark.asyncio
    async def test_viewport_scaling_in_grounded_click(self, reset_grounding_globals):
        """Grounded click scales coordinates from screenshot to viewport space."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                # screenshot: 1568px wide → viewport 1920px wide
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1920, "viewport_height": 1080,
                    },
                },
                # click_native
                {"id": "x", "result": {"success": True, "x": 960, "y": 540}},
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(784, 441)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_click("center of page")

        assert "Grounded click" in result
        click_msg = json.loads(fake_ws.sent[1])
        assert click_msg["method"] == "click_native"
        # 784 * (1920/1568) = 960, 441 * (1080/882) = 540
        assert click_msg["params"]["x"] == 960
        assert click_msg["params"]["y"] == 540

    @pytest.mark.asyncio
    async def test_4xx_not_retried(self, reset_grounding_globals):
        """4xx client errors (except 429) fail immediately without retry."""
        server._GROUNDING_API_KEY = "sk-bad"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )
        # Simulate a 401 Unauthorized from VLM API
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.HTTPStatusError("401", request=MagicMock(), response=mock_response)

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_click("the button")

        assert "401" in result
        assert call_count == 1  # No retries for 4xx

    @pytest.mark.asyncio
    async def test_coordinate_bounds_clamped(self, reset_grounding_globals):
        """VLM coordinates outside screenshot bounds are clamped."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
                {"id": "x", "result": {"success": True}},
            ]
        )
        # VLM returns coordinates beyond image bounds
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(1600, 900)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_click("off-screen element")

        assert "Grounded click" in result
        click_msg = json.loads(fake_ws.sent[1])
        # Clamped: 1600 → 1567 (1568-1), 900 → 881 (882-1)
        assert click_msg["params"]["x"] == 1567
        assert click_msg["params"]["y"] == 881

    @pytest.mark.asyncio
    async def test_empty_screenshot_returns_error(self, reset_grounding_globals):
        """Returns error when screenshot is empty."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        fake_ws = FakeWebSocket(
            responses=[{"id": "x", "result": {"image": ""}}]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_grounded_click("the button")
        assert "empty image" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_unparseable_vlm_returns_error(self, reset_grounding_globals):
        """Returns error when VLM response cannot be parsed into coordinates."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "I cannot find that element on the page."}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_click("nonexistent element")

        assert "could not parse coordinates" in result.lower()

    @pytest.mark.asyncio
    async def test_malformed_vlm_json_returns_error(self, reset_grounding_globals):
        """Returns error when VLM response JSON has unexpected structure."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )
        # Response missing "choices" key entirely
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"error": "model overloaded"},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_click("the button")

        assert "unexpected VLM response" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_5xx_retried_then_succeeds(self, reset_grounding_globals):
        """5xx errors trigger retry; eventual success works."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
                {"id": "x", "result": {"success": True}},
            ]
        )
        call_count = 0
        mock_error_resp = MagicMock()
        mock_error_resp.status_code = 503
        mock_error_resp.text = "Service Unavailable"

        mock_ok_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(400, 300)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.HTTPStatusError("503", request=MagicMock(), response=mock_error_resp)
            return mock_ok_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await server.browser_grounded_click("the link")

        assert "Grounded click" in result
        assert call_count == 2  # First call failed, second succeeded


class TestParseGroundingCoordinatesExtended:
    """Additional edge-case tests for _parse_grounding_coordinates."""

    def test_coordinates_embedded_in_text(self):
        """Coordinates embedded in natural language are extracted."""
        x, y = server._parse_grounding_coordinates(
            "The button is located at (523, 312) in the image.", 1568, 882
        )
        assert (x, y) == (523, 312)

    def test_qwen_box_takes_priority(self):
        """Qwen box token is matched before generic tuple patterns."""
        text = "<|box_start|>(100,200)<|box_end|> (300, 400)"
        x, y = server._parse_grounding_coordinates(text, 1568, 882)
        assert (x, y) == (100, 200)

    def test_decimal_absolute_coordinates(self):
        """Decimal absolute coordinates are rounded to integers."""
        x, y = server._parse_grounding_coordinates("(523.7, 312.2)", 1568, 882)
        assert (x, y) == (524, 312)


class TestParseGroundingCoordinatesNorm1000:
    """Tests for coord_mode='norm1000' (Qwen3-VL 0-1000 normalized grid)."""

    def test_norm1000_integer_pair(self):
        """0-1000 normalized coords denormalized to pixel space."""
        x, y = server._parse_grounding_coordinates(
            "(500, 500)", 1568, 1101, coord_mode="norm1000"
        )
        # 500/1000 * 1568 = 784.0, 500/1000 * 1101 = 550.5 → rounds to 550
        assert (x, y) == (784, 550)

    def test_norm1000_bounding_box(self):
        """Bounding box center denormalized from 0-1000 grid."""
        x, y = server._parse_grounding_coordinates(
            "(400, 300, 600, 500)", 1568, 1101, coord_mode="norm1000"
        )
        # center = (500, 400), denorm = (784, 440)
        assert (x, y) == (784, 440)

    def test_norm1000_qwen_box_token(self):
        """Qwen box tokens also denormalized in norm1000 mode."""
        x, y = server._parse_grounding_coordinates(
            "<|box_start|>(895,20)<|box_end|>", 1568, 1101, coord_mode="norm1000"
        )
        assert (x, y) == (round(895 * 1568 / 1000), round(20 * 1101 / 1000))

    def test_norm1000_point_tag(self):
        """Point tags also denormalized in norm1000 mode."""
        x, y = server._parse_grounding_coordinates(
            "<point>500 300</point>", 1568, 1101, coord_mode="norm1000"
        )
        assert (x, y) == (784, 330)

    def test_absolute_mode_unchanged(self):
        """Absolute mode returns raw values (backwards compatible)."""
        x, y = server._parse_grounding_coordinates(
            "(523, 312)", 1568, 882, coord_mode="absolute"
        )
        assert (x, y) == (523, 312)

    def test_normalized_floats_unaffected_by_coord_mode(self):
        """Normalized floats (0.xx) already scale to image dims, not affected by mode."""
        x, y = server._parse_grounding_coordinates(
            "(0.5, 0.3)", 1568, 882, coord_mode="norm1000"
        )
        assert (x, y) == (784, 265)

    def test_default_coord_mode_is_absolute(self):
        """Default coord_mode is absolute for backwards compatibility."""
        x, y = server._parse_grounding_coordinates("(500, 500)", 1568, 1101)
        assert (x, y) == (500, 500)


class TestSessionReplay:
    """Tests for session replay (tool call logging with screenshots)."""

    @pytest.fixture(autouse=True)
    def reset_replay_state(self):
        """Reset global replay state before each test."""
        server._replay_state_loaded = False
        server._replay_active = False
        server._replay_dir = None
        orig_session = server._session_id
        orig_session_id = server.SESSION_ID
        orig_replay_disabled = server.REPLAY_DISABLED
        server.SESSION_ID = ""
        server._session_id = None
        server._replay_state_loaded = True
        server.REPLAY_DISABLED = False
        yield
        server._replay_state_loaded = False
        server._replay_active = False
        server._replay_dir = None
        server._session_id = orig_session
        server.SESSION_ID = orig_session_id
        server.REPLAY_DISABLED = orig_replay_disabled

    # ── _load_replay_state ───────────────────────────────

    def test_auto_init_from_session_id(self, tmp_path):
        """_load_replay_state auto-creates dir + manifest when SESSION_ID is set."""
        server.SESSION_ID = "auto-test-123"
        server._replay_state_loaded = False
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            result = server._load_replay_state()
        assert result is True
        assert server._replay_active is True
        assert server._replay_dir is not None
        manifest_path = os.path.join(server._replay_dir, "manifest.json")
        assert os.path.exists(manifest_path)
        with open(manifest_path) as f:
            manifest = json.load(f)
        assert manifest["session_id"] == "auto-test-123"
        assert manifest["next_seq"] == 0

    def test_no_init_without_session_id(self):
        """_load_replay_state returns False when SESSION_ID is empty."""
        server.SESSION_ID = ""
        server._session_id = None
        server._replay_state_loaded = False
        result = server._load_replay_state()
        assert result is False
        assert server._replay_active is False

    def test_no_init_when_disabled(self):
        """_load_replay_state returns False when REPLAY_DISABLED is True."""
        server.SESSION_ID = "test-123"
        server.REPLAY_DISABLED = True
        server._replay_state_loaded = False
        result = server._load_replay_state()
        assert result is False
        assert server._replay_active is False

    def test_load_replay_state_fast_path(self):
        """Once loaded, _load_replay_state returns cached value."""
        server.SESSION_ID = "fast-test"
        server._replay_state_loaded = True
        server._replay_active = True
        result = server._load_replay_state()
        assert result is True

    # ── _claim_next_seq ─────────────────────────────────

    def test_claim_seq_increments(self, tmp_path):
        """_claim_next_seq returns sequential numbers and updates manifest."""
        server._replay_dir = str(tmp_path)
        manifest_path = os.path.join(str(tmp_path), "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump({"next_seq": 0}, f)

        seq0 = server._claim_next_seq()
        seq1 = server._claim_next_seq()
        seq2 = server._claim_next_seq()
        assert seq0 == 0
        assert seq1 == 1
        assert seq2 == 2

        with open(manifest_path) as f:
            data = json.load(f)
        assert data["next_seq"] == 3

    def test_claim_seq_no_dir(self):
        """Returns -1 when replay dir is None."""
        server._replay_dir = None
        assert server._claim_next_seq() == -1

    # ── _serialize_for_log ──────────────────────────────

    def test_serialize_string(self):
        assert server._serialize_for_log("hello") == "hello"

    def test_serialize_dict(self):
        result = server._serialize_for_log({"url": "https://example.com", "tab_id": ""})
        assert result == {"url": "https://example.com", "tab_id": ""}

    def test_serialize_list_with_image(self):
        from mcp.server.fastmcp.utilities.types import Image
        items = [Image(data=b"test", format="jpeg"), "some text"]
        result = server._serialize_for_log(items)
        assert result == ["[Image data]", "some text"]

    # ── _append_log_entry ────────────────────────────────

    def test_append_log_entry(self, tmp_path):
        """Appends a JSON line to tool_log.jsonl."""
        server._replay_dir = str(tmp_path)
        entry = {"seq": 0, "tool": "browser_ping", "args": {}, "result": "ok"}
        server._append_log_entry(entry)

        log_path = os.path.join(str(tmp_path), "tool_log.jsonl")
        assert os.path.exists(log_path)
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["tool"] == "browser_ping"

    def test_append_multiple_entries(self, tmp_path):
        """Multiple appends create multiple lines."""
        server._replay_dir = str(tmp_path)
        for i in range(3):
            server._append_log_entry({"seq": i, "tool": f"tool_{i}"})

        log_path = os.path.join(str(tmp_path), "tool_log.jsonl")
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 3

    def test_append_no_dir(self):
        """No-op when replay dir is None."""
        server._replay_dir = None
        server._append_log_entry({"seq": 0})  # Should not raise

    # ── _log_tool_call ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_log_tool_call_when_inactive(self):
        """No-op when replay is not active."""
        with patch.object(server, "browser_command", new_callable=AsyncMock) as mock_cmd:
            await server._log_tool_call("browser_ping", {}, "ok", "2026-01-01T00:00:00Z", 10.0)
            mock_cmd.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_tool_call_captures_screenshot(self, tmp_path):
        """Logs tool call with screenshot to JSONL and disk."""
        server._replay_dir = str(tmp_path)
        server._replay_active = True
        # Create manifest for seq claiming
        with open(os.path.join(str(tmp_path), "manifest.json"), "w") as f:
            json.dump({"session_id": "test", "next_seq": 0}, f)

        fake_ws = FakeWebSocket(responses=[
            {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}},
        ])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server._log_tool_call(
                "browser_click", {"index": 5}, '{"status":"ok"}',
                "2026-01-01T12:00:00Z", 150.5
            )

        # Check JSONL entry
        log_path = os.path.join(str(tmp_path), "tool_log.jsonl")
        with open(log_path) as f:
            entry = json.loads(f.readline())
        assert entry["tool"] == "browser_click"
        assert entry["args"] == {"index": 5}
        assert entry["duration_ms"] == 150.5
        assert entry["seq"] == 0
        assert entry["screenshot"] == "00000_browser_click.jpg"
        assert entry["error"] is False

        # Check screenshot file
        ss_path = os.path.join(str(tmp_path), "00000_browser_click.jpg")
        assert os.path.exists(ss_path)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_log_tool_call_screenshot_failure(self, tmp_path):
        """Screenshot failure on a visual tool doesn't prevent logging the call."""
        server._replay_dir = str(tmp_path)
        server._replay_active = True
        with open(os.path.join(str(tmp_path), "manifest.json"), "w") as f:
            json.dump({"session_id": "test", "next_seq": 0}, f)

        # Use a visual tool so screenshot capture is attempted
        with patch.object(server, "browser_command", side_effect=Exception("no tab")):
            await server._log_tool_call(
                "browser_click", {"index": 0}, '{"clicked": true}',
                "2026-01-01T12:00:00Z", 5.0
            )

        log_path = os.path.join(str(tmp_path), "tool_log.jsonl")
        with open(log_path) as f:
            entry = json.loads(f.readline())
        assert entry["tool"] == "browser_click"
        assert entry["screenshot"] is None  # Screenshot failed gracefully


    # ── browser_replay_status ───────────────────────────

    @pytest.mark.asyncio
    async def test_status_not_active(self):
        result = await server.browser_replay_status()
        data = json.loads(result)
        assert data["active"] is False

    @pytest.mark.asyncio
    async def test_status_active(self, tmp_path):
        server.SESSION_ID = "status-test"
        server._replay_state_loaded = False
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            result = await server.browser_replay_status()
        data = json.loads(result)
        assert data["active"] is True
        assert data["tool_call_count"] == 0

    @pytest.mark.asyncio
    async def test_status_with_entries(self, tmp_path):
        server._replay_dir = str(tmp_path)
        server._replay_active = True
        server._replay_state_loaded = True
        # Write some log entries
        log_path = os.path.join(str(tmp_path), "tool_log.jsonl")
        with open(log_path, "w") as f:
            f.write('{"seq":0}\n{"seq":1}\n{"seq":2}\n')
        # Write manifest
        with open(os.path.join(str(tmp_path), "manifest.json"), "w") as f:
            json.dump({"session_id": "test", "started_at": "2026-01-01T00:00:00Z"}, f)

        result = await server.browser_replay_status()
        data = json.loads(result)
        assert data["active"] is True
        assert data["tool_call_count"] == 3

    # ── Tool call logging wrapper ────────────────────────

    @pytest.mark.asyncio
    async def test_navigate_logs_tool_call(self, tmp_path):
        """browser_navigate logs tool call with screenshot via the wrapper."""
        server._replay_state_loaded = False
        server.SESSION_ID = "log-test"
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            fake_ws = FakeWebSocket(responses=[
                {"id": "x", "result": {"url": "https://example.com"}},  # navigate
                {"id": "x", "result": {"status": "complete"}},  # wait_for_load
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}},  # screenshot
            ])
            with patch.object(server, "get_ws", return_value=fake_ws):
                await server.browser_navigate("https://example.com")

        replay_dir = os.path.join(str(tmp_path), "zenripple_replay_log-test")
        log_path = os.path.join(replay_dir, "tool_log.jsonl")
        assert os.path.exists(log_path)
        with open(log_path) as f:
            entry = json.loads(f.readline())
        assert entry["tool"] == "browser_navigate"
        assert entry["args"]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_no_logging_when_inactive(self):
        """Tools don't log when replay is inactive."""
        fake_ws = FakeWebSocket(responses=[
            {"id": "x", "result": {"url": "https://example.com"}},
        ])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_navigate("https://example.com")
        # Only 1 message sent (navigate), no screenshot
        assert len(fake_ws.sent) == 1

    def test_corrupt_manifest_fresh_start(self, tmp_path):
        """_load_replay_state handles corrupt manifest by creating fresh one."""
        server._replay_state_loaded = False
        server.SESSION_ID = "corrupt-test"
        replay_dir = str(tmp_path / "zenripple_replay_corrupt-test")
        os.makedirs(replay_dir)
        with open(os.path.join(replay_dir, "manifest.json"), "w") as f:
            f.write("{bad json!!!")

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            result = server._load_replay_state()
        assert result is True
        assert server._replay_active is True
        with open(os.path.join(replay_dir, "manifest.json")) as f:
            fresh = json.load(f)
        assert fresh["session_id"] == "corrupt-test"
        assert fresh["next_seq"] == 0


class TestSanitizeSessionId:
    """Tests for _sanitize_session_id path traversal prevention."""

    def test_strips_path_traversal(self):
        assert server._sanitize_session_id("../../etc/passwd") == "etcpasswd"

    def test_all_special_chars_returns_empty(self):
        assert server._sanitize_session_id("...") == ""

    def test_empty_input(self):
        assert server._sanitize_session_id("") == ""

    def test_valid_id_unchanged(self):
        assert server._sanitize_session_id("session-123_abc") == "session-123_abc"

    def test_uuid_format(self):
        # UUIDs have hyphens which are allowed
        assert server._sanitize_session_id("a1b2c3d4-e5f6-7890-abcd-ef1234567890") == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_strips_spaces_and_slashes(self):
        assert server._sanitize_session_id("my session/with spaces") == "mysessionwithspaces"


# ── Proactive Notifications ───────────────────────────────────


class TestNotifications:
    def setup_method(self):
        server._pending_notifications.clear()

    def teardown_method(self):
        server._pending_notifications.clear()

    def test_drain_empty(self):
        assert server._drain_notifications() == ""

    def test_drain_dialog_notification(self):
        server._pending_notifications.append({
            "type": "dialog_opened",
            "dialog_type": "confirmCheck",
            "message": "Delete this item?",
            "tab_id": "tab-1",
        })
        result = server._drain_notifications()
        assert "NOTIFICATION" in result
        assert "confirmCheck" in result
        assert "Delete this item?" in result
        assert "browser_handle_dialog" in result
        # Drain clears the deque
        assert len(server._pending_notifications) == 0

    def test_drain_popup_blocked_notification(self):
        server._pending_notifications.append({
            "type": "popup_blocked",
            "blocked_count": 1,
            "popup_urls": ["https://example.com/popup"],
            "tab_id": "tab-1",
        })
        result = server._drain_notifications()
        assert "NOTIFICATION" in result
        assert "popup" in result.lower()
        assert "https://example.com/popup" in result
        assert "browser_allow_blocked_popup" in result

    def test_drain_clears_notifications(self):
        server._pending_notifications.extend([
            {"type": "dialog_opened", "dialog_type": "alert", "message": "hi", "tab_id": "t"},
            {"type": "popup_blocked", "blocked_count": 1, "popup_urls": [], "tab_id": "t"},
        ])
        server._drain_notifications()
        assert len(server._pending_notifications) == 0
        # Second drain returns nothing
        assert server._drain_notifications() == ""

    def test_append_notifications_empty(self):
        result = server._append_notifications("click succeeded")
        assert result == "click succeeded"

    def test_append_notifications_with_dialog(self):
        server._pending_notifications.append({
            "type": "dialog_opened",
            "dialog_type": "alertCheck",
            "message": "Warning!",
            "tab_id": "tab-1",
        })
        result = server._append_notifications('{"success": true}')
        assert result.startswith('{"success": true}')
        assert "NOTIFICATION" in result
        assert "Warning!" in result

    @pytest.mark.asyncio
    async def test_notifications_extracted_from_response(self):
        """browser_command() should extract _notifications from the response."""
        notifications = [{"type": "dialog_opened", "dialog_type": "alert", "message": "test", "tab_id": "t1"}]
        fake_ws = FakeWebSocket(responses=[{
            "id": "x",
            "result": {"success": True},
            "_notifications": notifications,
        }])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_command("click_element", {"index": 0})
        assert len(server._pending_notifications) == 1
        assert server._pending_notifications[0]["type"] == "dialog_opened"

    @pytest.mark.asyncio
    async def test_no_notifications_when_absent(self):
        """No _notifications key → nothing accumulated."""
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": {"success": True}}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_command("click_element", {"index": 0})
        assert len(server._pending_notifications) == 0

    @pytest.mark.asyncio
    async def test_click_appends_dialog_notification(self):
        """browser_click should include dialog notification in return value."""
        notifications = [{"type": "dialog_opened", "dialog_type": "confirmCheck",
                          "message": "Disconnect Gmail?", "tab_id": "t1"}]
        fake_ws = FakeWebSocket(responses=[{
            "id": "x",
            "result": {"success": True},
            "_notifications": notifications,
        }])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_click(index=5)
        assert "Disconnect Gmail?" in result
        assert "browser_handle_dialog" in result

    @pytest.mark.asyncio
    async def test_click_appends_popup_blocked_notification(self):
        """browser_click should include popup-blocked notification in return value."""
        notifications = [{"type": "popup_blocked", "blocked_count": 1, "popup_urls": ["https://ads.example.com"], "tab_id": "t1"}]
        fake_ws = FakeWebSocket(responses=[{
            "id": "x",
            "result": {"success": True},
            "_notifications": notifications,
        }])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_click(index=3)
        assert "popup" in result.lower()
        assert "https://ads.example.com" in result
        assert "browser_allow_blocked_popup" in result


class TestPopupBlockedTools:
    @pytest.mark.asyncio
    async def test_get_popup_blocked_events_empty(self):
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": []}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_popup_blocked_events()
        assert json.loads(result) == []

    @pytest.mark.asyncio
    async def test_get_popup_blocked_events_with_data(self):
        events = [{"type": "popup_blocked", "tab_id": "t1",
                    "blocked_count": 2, "popup_urls": ["https://example.com", "https://other.com"],
                    "timestamp": "2026-03-03T00:00:00Z"}]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": events}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_get_popup_blocked_events()
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["blocked_count"] == 2
        assert "https://example.com" in data[0]["popup_urls"]

    @pytest.mark.asyncio
    async def test_allow_blocked_popup(self):
        resp = {"success": True, "unblocked": 2, "opened_tab_ids": ["tab-1", "tab-2"],
                "popup_urls": ["https://a.com", "https://b.com"]}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_allow_blocked_popup()
        data = json.loads(result)
        assert data["success"] is True
        assert data["unblocked"] == 2
        assert data["opened_tab_ids"] == ["tab-1", "tab-2"]

    @pytest.mark.asyncio
    async def test_allow_blocked_popup_sends_tab_id(self):
        resp = {"success": True, "unblocked": 1}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_allow_blocked_popup(tab_id="my-tab")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "my-tab"

    @pytest.mark.asyncio
    async def test_allow_blocked_popup_sends_index(self):
        """index parameter is forwarded to the browser command."""
        resp = {"success": True, "unblocked": 1, "popup_url": "https://a.com",
                "opened_tab_ids": ["tab-99"]}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_allow_blocked_popup(tab_id="t", index=2)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["index"] == 2
        assert msg["params"]["tab_id"] == "t"

    @pytest.mark.asyncio
    async def test_allow_blocked_popup_no_index_omitted(self):
        """When index is -1 (default), it is NOT sent to the browser."""
        resp = {"success": True, "unblocked": 1}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_allow_blocked_popup()
        msg = json.loads(fake_ws.sent[0])
        assert "index" not in msg["params"]


class TestNotificationsOnError:
    """Notifications are extracted even when browser_command raises."""

    def setup_method(self):
        server._pending_notifications.clear()

    def teardown_method(self):
        server._pending_notifications.clear()

    @pytest.mark.asyncio
    async def test_notifications_extracted_from_error_response(self):
        notifications = [{"type": "dialog_opened", "dialog_type": "alert",
                          "message": "oops", "tab_id": "t1"}]
        fake_ws = FakeWebSocket(responses=[{
            "id": "x",
            "error": {"message": "Element not found"},
            "_notifications": notifications,
        }])
        with patch.object(server, "get_ws", return_value=fake_ws):
            with pytest.raises(Exception, match="Element not found"):
                await server.browser_command("click_element", {"index": 999})
        assert len(server._pending_notifications) == 1
        assert server._pending_notifications[0]["type"] == "dialog_opened"
        assert server._pending_notifications[0]["message"] == "oops"

    @pytest.mark.asyncio
    async def test_notifications_accumulate_across_commands(self):
        """Multiple browser_command calls accumulate notifications until drained."""
        for i in range(3):
            notif = [{"type": "dialog_opened", "dialog_type": "alert",
                      "message": f"msg{i}", "tab_id": "t"}]
            fake_ws = FakeWebSocket(responses=[{
                "id": "x", "result": {"success": True},
                "_notifications": notif,
            }])
            with patch.object(server, "get_ws", return_value=fake_ws):
                await server.browser_command("ping", {})
        assert len(server._pending_notifications) == 3
        text = server._drain_notifications()
        assert "msg0" in text
        assert "msg1" in text
        assert "msg2" in text
        assert len(server._pending_notifications) == 0

    @pytest.mark.asyncio
    async def test_notification_cap(self):
        """Notifications are capped at deque maxlen (50), keeping the newest."""
        notifications = [{"type": "dialog_opened", "dialog_type": "alert",
                          "message": f"n{i}", "tab_id": "t"} for i in range(80)]
        fake_ws = FakeWebSocket(responses=[{
            "id": "x", "result": {"ok": True},
            "_notifications": notifications,
        }])
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_command("ping", {})
        assert len(server._pending_notifications) == 50
        # Keeps the newest 50 (n30..n79)
        assert server._pending_notifications[0]["message"] == "n30"
        assert server._pending_notifications[-1]["message"] == "n79"

    def test_drain_unknown_notification_type(self):
        """Unknown notification types are included, not silently dropped."""
        server._pending_notifications.append({
            "type": "something_new", "data": "whatever", "tab_id": "t"
        })
        result = server._drain_notifications()
        assert len(server._pending_notifications) == 0
        assert "something_new" in result
        assert "whatever" in result


# ── Hover Coordinates ──────────────────────────────────────────


class TestHoverCoordinates:
    @pytest.mark.asyncio
    async def test_hover_coordinates_basic(self):
        """Hover at specific coordinates sends correct command."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "x": 100, "y": 200, "tag": "div", "text": "content"}}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_hover_coordinates(100, 200)
        data = json.loads(result)
        assert data["success"] is True
        assert data["x"] == 100
        assert data["y"] == 200
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "hover_coordinates"
        assert msg["params"]["x"] == 100
        assert msg["params"]["y"] == 200

    @pytest.mark.asyncio
    async def test_hover_coordinates_with_tab_id(self):
        """Tab ID is forwarded correctly."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "x": 50, "y": 60, "tag": "a", "text": "Link"}}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_hover_coordinates(50, 60, tab_id="tab1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "tab1"

    @pytest.mark.asyncio
    async def test_hover_coordinates_with_frame_id(self):
        """Frame ID is forwarded correctly."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "x": 10, "y": 20, "tag": "span", "text": ""}}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_hover_coordinates(10, 20, frame_id=42)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["frame_id"] == 42

    @pytest.mark.asyncio
    async def test_hover_coordinates_auto_scales(self):
        """Coordinates are auto-scaled from screenshot-space to viewport-space."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "x": 200, "y": 200, "tag": "div", "text": ""}}
            ]
        )
        # Set up screenshot dims: 1000px screenshot, 2000px viewport
        server._last_screenshot_dims[""] = {"sw": 1000, "sh": 500, "vw": 2000, "vh": 1000}
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_hover_coordinates(100, 100)
        msg = json.loads(fake_ws.sent[0])
        # 100 * (2000/1000) = 200, 100 * (1000/500) = 200
        assert msg["params"]["x"] == 200
        assert msg["params"]["y"] == 200
        server._last_screenshot_dims.clear()

    @pytest.mark.asyncio
    async def test_hover_coordinates_no_scale_when_same_dims(self):
        """No scaling when screenshot and viewport dimensions match."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"success": True, "x": 100, "y": 100, "tag": "div", "text": ""}}
            ]
        )
        server._last_screenshot_dims[""] = {"sw": 1000, "sh": 500, "vw": 1000, "vh": 500}
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_hover_coordinates(100, 100)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["x"] == 100
        assert msg["params"]["y"] == 100
        server._last_screenshot_dims.clear()


# ── Scroll at Point ────────────────────────────────────────────


class TestScrollAtPoint:
    @pytest.mark.asyncio
    async def test_scroll_at_point_default(self):
        """Scroll at point sends correct command with defaults."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {
                    "success": True, "x": 300, "y": 400, "direction": "down",
                    "amount": 500, "scrollX": 0, "scrollY": 500, "tag": "div",
                }}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_scroll_at_point(300, 400)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "scroll_at_point"
        assert msg["params"]["x"] == 300
        assert msg["params"]["y"] == 400
        assert msg["params"]["direction"] == "down"
        assert msg["params"]["amount"] == 500

    @pytest.mark.asyncio
    async def test_scroll_at_point_custom_direction_amount(self):
        """Custom direction and amount are forwarded."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {
                    "success": True, "x": 50, "y": 60, "direction": "up",
                    "amount": 200, "scrollX": 0, "scrollY": 0, "tag": "ul",
                }}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_scroll_at_point(50, 60, direction="up", amount=200)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["direction"] == "up"
        assert msg["params"]["amount"] == 200

    @pytest.mark.asyncio
    async def test_scroll_at_point_left_right(self):
        """Horizontal scroll directions work."""
        for direction in ("left", "right"):
            fake_ws = FakeWebSocket(
                responses=[
                    {"id": "x", "result": {
                        "success": True, "x": 100, "y": 100, "direction": direction,
                        "amount": 300, "scrollX": 300, "scrollY": 0, "tag": "div",
                    }}
                ]
            )
            server._last_screenshot_dims.clear()
            with patch.object(server, "get_ws", return_value=fake_ws):
                await server.browser_scroll_at_point(100, 100, direction=direction, amount=300)
            msg = json.loads(fake_ws.sent[0])
            assert msg["params"]["direction"] == direction
            assert msg["params"]["amount"] == 300

    @pytest.mark.asyncio
    async def test_scroll_at_point_with_tab_id(self):
        """Tab ID is forwarded correctly."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {
                    "success": True, "x": 10, "y": 20, "direction": "down",
                    "amount": 500, "scrollX": 0, "scrollY": 500, "tag": "div",
                }}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_scroll_at_point(10, 20, tab_id="panel1")
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["tab_id"] == "panel1"

    @pytest.mark.asyncio
    async def test_scroll_at_point_with_frame_id(self):
        """Frame ID is forwarded correctly."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {
                    "success": True, "x": 10, "y": 20, "direction": "down",
                    "amount": 500, "scrollX": 0, "scrollY": 500, "tag": "div",
                }}
            ]
        )
        server._last_screenshot_dims.clear()
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_scroll_at_point(10, 20, frame_id=99)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["frame_id"] == 99

    @pytest.mark.asyncio
    async def test_scroll_at_point_auto_scales(self):
        """Coordinates are auto-scaled from screenshot-space to viewport-space."""
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {
                    "success": True, "x": 200, "y": 200, "direction": "down",
                    "amount": 500, "scrollX": 0, "scrollY": 500, "tag": "div",
                }}
            ]
        )
        server._last_screenshot_dims[""] = {"sw": 1000, "sh": 500, "vw": 2000, "vh": 1000}
        with patch.object(server, "get_ws", return_value=fake_ws):
            await server.browser_scroll_at_point(100, 100)
        msg = json.loads(fake_ws.sent[0])
        assert msg["params"]["x"] == 200
        assert msg["params"]["y"] == 200
        server._last_screenshot_dims.clear()


# ── Grounded Hover ─────────────────────────────────────────────


class TestGroundedHover:
    @pytest.fixture(autouse=True)
    def reset_grounding(self, reset_grounding_globals):
        pass

    @pytest.mark.asyncio
    async def test_grounded_hover_basic(self):
        """Grounded hover takes screenshot, calls VLM, hovers at predicted coords."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                # screenshot
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
                # hover_coordinates
                {"id": "x", "result": {"success": True, "x": 400, "y": 300, "tag": "button", "text": "Submit"}},
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(400, 300)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_hover("the Submit button")

        assert "Grounded hover" in result
        hover_msg = json.loads(fake_ws.sent[1])
        assert hover_msg["method"] == "hover_coordinates"
        assert hover_msg["params"]["x"] == 400
        assert hover_msg["params"]["y"] == 300

    @pytest.mark.asyncio
    async def test_grounded_hover_no_api_key(self):
        """Returns error when no API key is set."""
        server._GROUNDING_API_KEY = ""
        server._GROUNDING_KEY_SYNCED = True
        result = await server.browser_grounded_hover("something")
        assert "OPENROUTER_API_KEY not set" in result

    @pytest.mark.asyncio
    async def test_grounded_hover_empty_screenshot(self):
        """Returns error when screenshot is empty."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": "", "width": 0, "height": 0}},
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_grounded_hover("the button")
        assert "empty image" in result

    @pytest.mark.asyncio
    async def test_grounded_hover_unparseable_coords(self):
        """Returns error when VLM response can't be parsed."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "I can't find that element"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_hover("nonexistent element")
        assert "could not parse coordinates" in result

    @pytest.mark.asyncio
    async def test_grounded_hover_viewport_scaling(self):
        """Grounded hover scales from screenshot-space to viewport-space."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1920, "viewport_height": 1080,
                    },
                },
                {"id": "x", "result": {"success": True, "x": 960, "y": 540, "tag": "div", "text": ""}},
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(784, 441)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_hover("center of page")

        assert "Grounded hover" in result
        hover_msg = json.loads(fake_ws.sent[1])
        assert hover_msg["method"] == "hover_coordinates"
        # 784 * (1920/1568) = 960, 441 * (1080/882) = 540
        assert hover_msg["params"]["x"] == 960
        assert hover_msg["params"]["y"] == 540

    @pytest.mark.asyncio
    async def test_grounded_hover_vlm_4xx(self):
        """4xx VLM errors fail immediately."""
        server._GROUNDING_API_KEY = "sk-bad"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        async def mock_post(*args, **kwargs):
            raise httpx.HTTPStatusError("401", request=MagicMock(), response=mock_response)

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_hover("the button")
        assert "401" in result

    @pytest.mark.asyncio
    async def test_grounded_hover_transport_error_retries(self):
        """Transport errors are retried with backoff."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("connection refused")

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await server.browser_grounded_hover("the tooltip trigger")

        assert "failed after 3 attempts" in result
        assert call_count == 3


# ── Grounded Scroll ────────────────────────────────────────────


class TestGroundedScroll:
    @pytest.fixture(autouse=True)
    def reset_grounding(self, reset_grounding_globals):
        pass

    @pytest.mark.asyncio
    async def test_grounded_scroll_basic(self):
        """Grounded scroll takes screenshot, calls VLM, scrolls at predicted coords."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                # screenshot
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
                # scroll_at_point
                {"id": "x", "result": {
                    "success": True, "x": 400, "y": 300, "direction": "down",
                    "amount": 500, "scrollX": 0, "scrollY": 500, "tag": "div",
                }},
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(400, 300)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_scroll("the dropdown menu")

        assert "Grounded scroll" in result
        scroll_msg = json.loads(fake_ws.sent[1])
        assert scroll_msg["method"] == "scroll_at_point"
        assert scroll_msg["params"]["x"] == 400
        assert scroll_msg["params"]["y"] == 300
        assert scroll_msg["params"]["direction"] == "down"
        assert scroll_msg["params"]["amount"] == 500

    @pytest.mark.asyncio
    async def test_grounded_scroll_custom_direction_amount(self):
        """Custom direction and amount are forwarded."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
                {"id": "x", "result": {
                    "success": True, "x": 200, "y": 200, "direction": "up",
                    "amount": 300, "scrollX": 0, "scrollY": 0, "tag": "ul",
                }},
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(200, 200)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_scroll(
                "the sidebar", direction="up", amount=300
            )

        assert "Grounded scroll" in result
        assert "up" in result
        scroll_msg = json.loads(fake_ws.sent[1])
        assert scroll_msg["params"]["direction"] == "up"
        assert scroll_msg["params"]["amount"] == 300

    @pytest.mark.asyncio
    async def test_grounded_scroll_no_api_key(self):
        """Returns error when no API key is set."""
        server._GROUNDING_API_KEY = ""
        server._GROUNDING_KEY_SYNCED = True
        result = await server.browser_grounded_scroll("something")
        assert "OPENROUTER_API_KEY not set" in result

    @pytest.mark.asyncio
    async def test_grounded_scroll_empty_screenshot(self):
        """Returns error when screenshot is empty."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": "", "width": 0, "height": 0}},
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_grounded_scroll("the dropdown")
        assert "empty image" in result

    @pytest.mark.asyncio
    async def test_grounded_scroll_unparseable_coords(self):
        """Returns error when VLM response can't be parsed."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "No scrollable area found"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_scroll("nonexistent area")
        assert "could not parse coordinates" in result

    @pytest.mark.asyncio
    async def test_grounded_scroll_viewport_scaling(self):
        """Grounded scroll scales from screenshot-space to viewport-space."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1920, "viewport_height": 1080,
                    },
                },
                {"id": "x", "result": {
                    "success": True, "x": 960, "y": 540, "direction": "down",
                    "amount": 500, "scrollX": 0, "scrollY": 500, "tag": "div",
                }},
            ]
        )
        mock_resp = type("Resp", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "(784, 441)"}}]},
        })()

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await server.browser_grounded_scroll("center of page")

        assert "Grounded scroll" in result
        scroll_msg = json.loads(fake_ws.sent[1])
        assert scroll_msg["method"] == "scroll_at_point"
        assert scroll_msg["params"]["x"] == 960
        assert scroll_msg["params"]["y"] == 540

    @pytest.mark.asyncio
    async def test_grounded_scroll_vlm_transport_error_retries(self):
        """Transport errors are retried with backoff."""
        server._GROUNDING_API_KEY = "sk-test"
        server._GROUNDING_KEY_SYNCED = True
        server._last_screenshot_dims.clear()
        fake_ws = FakeWebSocket(
            responses=[
                {
                    "id": "x",
                    "result": {
                        "image": _TINY_DATA_URL,
                        "width": 1568, "height": 882,
                        "viewport_width": 1568, "viewport_height": 882,
                    },
                },
            ]
        )

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("connection refused")

        with patch.object(server, "get_ws", return_value=fake_ws), \
             patch("httpx.AsyncClient.post", side_effect=mock_post), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await server.browser_grounded_scroll("the dropdown")

        assert "failed after 3 attempts" in result
        assert call_count == 3
