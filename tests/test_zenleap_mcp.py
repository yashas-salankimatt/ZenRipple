"""Tests for the ZenLeap MCP server.

Covers message formatting, connection management, tool definitions,
and error handling. Uses a mock WebSocket server to simulate the browser.
"""

import asyncio
import base64
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from mcp.server.fastmcp.utilities.types import Image

import zenleap_mcp_server as server


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
    async def test_returns_image(self):
        fake_ws = FakeWebSocket(
            responses=[
                {"id": "x", "result": {"image": _TINY_DATA_URL, "width": 1, "height": 1}}
            ]
        )
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_screenshot()
        assert isinstance(result, Image)
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
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_click_coordinates(100, 200)
        data = json.loads(result)
        assert data["success"] is True
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "click_coordinates"
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
            with pytest.raises(ConnectionRefusedError):
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

    @pytest.mark.asyncio
    async def test_new_session_url(self):
        """Without ZENLEAP_SESSION_ID, connects to /new."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenLeap-Session": "abc-1234"}
        )
        with patch.object(server, "SESSION_ID", ""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            ws = await server.get_ws()
        assert ws is fake_ws
        mock_connect.assert_called_once_with(
            "ws://localhost:9876/new",
            max_size=10 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=120,
        )
        assert server._session_id == "abc-1234"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_join_session_url(self):
        """With ZENLEAP_SESSION_ID set, connects to /session/<id>."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenLeap-Session": "existing-session"}
        )
        with patch.object(server, "SESSION_ID", "existing-session"), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            ws = await server.get_ws()
        assert ws is fake_ws
        mock_connect.assert_called_once_with(
            "ws://localhost:9876/session/existing-session",
            max_size=10 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=120,
        )
        assert server._session_id == "existing-session"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_custom_ws_url(self):
        """ZENLEAP_WS_URL is respected in URL construction."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket()
        with patch.object(server, "SESSION_ID", ""), \
             patch.object(server, "BROWSER_WS_URL", "ws://remote:1234"), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws) as mock_connect:
            ws = await server.get_ws()
        mock_connect.assert_called_once_with(
            "ws://remote:1234/new",
            max_size=10 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=120,
        )
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_session_id_extracted_from_headers(self):
        """X-ZenLeap-Session header is stored in _session_id."""
        server._ws_connection = None
        server._session_id = None
        fake_ws = FakeWebSocket(
            response_headers={"X-ZenLeap-Session": "sess-xyz"}
        )
        with patch.object(server, "SESSION_ID", ""), \
             patch("websockets.connect", new_callable=AsyncMock, return_value=fake_ws):
            await server.get_ws()
        assert server._session_id == "sess-xyz"
        server._ws_connection = None
        server._session_id = None

    @pytest.mark.asyncio
    async def test_session_id_none_when_no_header(self):
        """When no X-ZenLeap-Session header, _session_id stays None."""
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
    async def test_reconnect_uses_saved_session_id(self):
        """When connection dies, reconnects to same session using saved _session_id."""
        dead_ws = FakeWebSocket()
        dead_ws.closed = True
        server._ws_connection = dead_ws
        server._session_id = "old-session"

        new_ws = FakeWebSocket(
            response_headers={"X-ZenLeap-Session": "old-session"}
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
            response_headers={"X-ZenLeap-Session": "fresh-session"}
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


# ── Phase 12: Session Management Tools ────────────────────────


class TestSessionManagement:
    """Tests for session_info, session_close, list_sessions MCP tools."""

    @pytest.mark.asyncio
    async def test_session_info(self):
        resp = {
            "session_id": "abc-1234",
            "workspace_name": "Zen AI Agent",
            "workspace_id": "ws-uuid",
            "connection_id": "conn-1",
            "connection_count": 2,
            "tab_count": 3,
            "created_at": 1700000000000,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_info()
        data = json.loads(result)
        assert data["session_id"] == "abc-1234"
        assert data["workspace_name"] == "Zen AI Agent"
        assert data["connection_count"] == 2
        assert data["tab_count"] == 3
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "session_info"

    @pytest.mark.asyncio
    async def test_session_close(self):
        resp = {"success": True, "session_id": "abc-1234", "tabs_closed": 3}
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_session_close()
        data = json.loads(result)
        assert data["success"] is True
        assert data["tabs_closed"] == 3
        msg = json.loads(fake_ws.sent[0])
        assert msg["method"] == "session_close"

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        resp = [
            {
                "session_id": "abc-1234",
                "workspace_name": "Zen AI Agent",
                "connection_count": 1,
                "tab_count": 2,
                "created_at": 1700000000000,
            },
            {
                "session_id": "def-5678",
                "workspace_name": "Zen AI Agent",
                "connection_count": 3,
                "tab_count": 5,
                "created_at": 1700001000000,
            },
        ]
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_list_sessions()
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["session_id"] == "abc-1234"
        assert data[1]["session_id"] == "def-5678"
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
            "previous_owner": None,
            "was_stale": False,
        }
        fake_ws = FakeWebSocket(responses=[{"id": "x", "result": resp}])
        with patch.object(server, "get_ws", return_value=fake_ws):
            result = await server.browser_claim_tab("panel2")
        data = json.loads(result)
        assert data["success"] is True
        assert data["tab_id"] == "panel2"
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
