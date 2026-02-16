"""Phase 7 feature benchmark scenarios.

Tests: cookies, localStorage/sessionStorage, network monitoring.
"""

from __future__ import annotations

from typing import Any

from bench.scenario import BrowserStateCheck, Scenario, ScenarioCategory


async def verify_tab_exists(state: dict[str, Any]) -> bool:
    tabs = state.get("tabs", [])
    return any(t.get("url", "") != "about:blank" for t in tabs)


async def verify_page_loaded(state: dict[str, Any]) -> bool:
    info = state.get("active_page_info", {})
    return bool(info.get("title"))


PHASE7_SCENARIOS = [
    Scenario(
        id="p7-001",
        name="Set and read a cookie",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Open https://httpbin.org/cookies in a new tab and wait for it to load. "
            "Set a cookie named 'bench_test' with value 'hello123' using browser_set_cookie. "
            "Then use browser_get_cookies to read all cookies. "
            "Tell me what cookies you found."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_set_cookie",
            "mcp__zenleap-browser__browser_get_cookies",
        ],
        max_turns=12,
        max_budget_usd=0.30,
        difficulty="easy",
        tags=["smoke", "phase7", "cookies"],
    ),
    Scenario(
        id="p7-002",
        name="Set and read localStorage",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Open https://example.com in a new tab and wait for it to load. "
            "Set a localStorage key 'bench_key' with value 'bench_value' using browser_set_storage. "
            "Then read it back with browser_get_storage (storage_type='localStorage', key='bench_key'). "
            "Tell me the value you read."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_set_storage",
            "mcp__zenleap-browser__browser_get_storage",
        ],
        max_turns=12,
        max_budget_usd=0.25,
        difficulty="easy",
        tags=["smoke", "phase7", "storage"],
    ),
    Scenario(
        id="p7-003",
        name="Monitor network requests during navigation",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Start network monitoring with browser_network_monitor_start. "
            "Then open https://example.com in a new tab and wait for it to load. "
            "Then use browser_network_get_log to get captured requests. "
            "Stop monitoring with browser_network_monitor_stop. "
            "Tell me what network requests were captured."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_network_monitor_start",
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_network_get_log",
            "mcp__zenleap-browser__browser_network_monitor_stop",
        ],
        max_turns=15,
        max_budget_usd=0.35,
        difficulty="medium",
        tags=["smoke", "phase7", "network"],
    ),
]
