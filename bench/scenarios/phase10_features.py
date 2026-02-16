"""Phase 10 feature benchmark scenarios.

Tests: chrome-context eval, drag-and-drop, reflection tool.
"""

from __future__ import annotations

from typing import Any

from bench.scenario import BrowserStateCheck, Scenario, ScenarioCategory


async def verify_tab_exists(state: dict[str, Any]) -> bool:
    tabs = state.get("tabs", [])
    return any(t.get("url", "") != "about:blank" for t in tabs)


async def verify_browser_alive(state: dict[str, Any]) -> bool:
    """Verify the browser WS connection is alive (always passes if we got state)."""
    return True


PHASE10_SCENARIOS = [
    Scenario(
        id="p10-001",
        name="Chrome-context eval — get app name",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Use browser_eval_chrome to evaluate 'Services.appinfo.name' "
            "in the browser's chrome context. Tell me what the browser is called."
        ),
        verifications=[
            BrowserStateCheck("browser alive", verify_browser_alive),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_eval_chrome",
        ],
        max_turns=5,
        max_budget_usd=0.15,
        difficulty="easy",
        tags=["smoke", "phase10", "chrome_eval"],
    ),
    Scenario(
        id="p10-002",
        name="Drag between coordinates on a page",
        category=ScenarioCategory.MULTI_STEP,
        prompt=(
            "Open https://example.com in a new tab and wait for it to load. "
            "Use browser_drag_coordinates to drag from coordinates (100, 100) "
            "to (300, 300) with 5 steps. Tell me the result."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_drag_coordinates",
        ],
        max_turns=10,
        max_budget_usd=0.25,
        difficulty="easy",
        tags=["smoke", "phase10", "drag"],
    ),
    Scenario(
        id="p10-003",
        name="Reflect on current page state",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Open https://example.com in a new tab and wait for it to load. "
            "Use browser_reflect with goal 'understand page content' to get "
            "a comprehensive snapshot. Tell me what the page contains based "
            "on the screenshot and text."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_reflect",
        ],
        max_turns=10,
        max_budget_usd=0.30,
        difficulty="easy",
        tags=["smoke", "phase10", "reflection"],
    ),
]
