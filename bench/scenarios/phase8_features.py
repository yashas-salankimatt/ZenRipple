"""Phase 8 feature benchmark scenarios.

Tests: compact DOM, accessibility tree, viewport-only filtering.
"""

from __future__ import annotations

from typing import Any

from bench.scenario import BrowserStateCheck, Scenario, ScenarioCategory


async def verify_tab_exists(state: dict[str, Any]) -> bool:
    tabs = state.get("tabs", [])
    return any(t.get("url", "") != "about:blank" for t in tabs)


PHASE8_SCENARIOS = [
    Scenario(
        id="p8-001",
        name="Use compact DOM representation",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Open https://example.com in a new tab and wait for it to load. "
            "Use browser_get_elements_compact to get a compact list of interactive elements. "
            "Tell me all the interactive elements you found and their indices."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_get_elements_compact",
        ],
        max_turns=10,
        max_budget_usd=0.20,
        difficulty="easy",
        tags=["smoke", "phase8", "compact_dom"],
    ),
    Scenario(
        id="p8-002",
        name="Get accessibility tree",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Open https://example.com in a new tab and wait for it to load. "
            "Use browser_get_accessibility_tree to extract the page's accessibility tree. "
            "Tell me what roles and names you found."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_get_accessibility_tree",
        ],
        max_turns=10,
        max_budget_usd=0.20,
        difficulty="easy",
        tags=["smoke", "phase8", "a11y"],
    ),
    Scenario(
        id="p8-003",
        name="Viewport-only DOM filtering",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Open https://en.wikipedia.org/wiki/Web_browser in a new tab and wait for it to load. "
            "Use browser_get_dom with viewport_only=true to get only elements visible in the viewport. "
            "Then use browser_get_dom without viewport_only to get all elements. "
            "Compare the counts and tell me how many elements were filtered out."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_get_dom",
        ],
        max_turns=12,
        max_budget_usd=0.35,
        difficulty="medium",
        tags=["smoke", "phase8", "smart_dom"],
    ),
]
