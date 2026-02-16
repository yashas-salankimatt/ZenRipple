"""Phase 9 feature benchmark scenarios.

Tests: multi-tab coordination, visual grounding, action recording.
"""

from __future__ import annotations

from typing import Any

from bench.scenario import BrowserStateCheck, Scenario, ScenarioCategory


async def verify_multiple_tabs(state: dict[str, Any]) -> bool:
    """Verify at least 2 non-blank tabs exist."""
    tabs = state.get("tabs", [])
    non_blank = [t for t in tabs if t.get("url", "") != "about:blank"]
    return len(non_blank) >= 2


async def verify_tab_exists(state: dict[str, Any]) -> bool:
    tabs = state.get("tabs", [])
    return any(t.get("url", "") != "about:blank" for t in tabs)


PHASE9_SCENARIOS = [
    Scenario(
        id="p9-001",
        name="Batch navigate and compare tabs",
        category=ScenarioCategory.TAB_MANAGEMENT,
        prompt=(
            "Use browser_batch_navigate to open these URLs simultaneously: "
            "https://example.com, https://www.iana.org "
            "Wait for both tabs to load. "
            "Then use browser_compare_tabs with both tab IDs to compare their content. "
            "Tell me the differences in their text."
        ),
        verifications=[
            BrowserStateCheck("multiple tabs exist", verify_multiple_tabs),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_batch_navigate",
            "mcp__zenleap-browser__browser_compare_tabs",
        ],
        max_turns=15,
        max_budget_usd=0.40,
        difficulty="medium",
        tags=["smoke", "phase9", "multi_tab"],
    ),
    Scenario(
        id="p9-002",
        name="Find element by description",
        category=ScenarioCategory.INFORMATION_EXTRACTION,
        prompt=(
            "Open https://example.com in a new tab and wait for it to load. "
            "Use browser_find_element_by_description with description 'more information link' "
            "to find the relevant element. "
            "Tell me the element index and what it points to."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_find_element_by_description",
        ],
        max_turns=10,
        max_budget_usd=0.25,
        difficulty="easy",
        tags=["smoke", "phase9", "visual_grounding"],
    ),
    Scenario(
        id="p9-003",
        name="Record and replay navigation actions",
        category=ScenarioCategory.MULTI_STEP,
        prompt=(
            "Start recording with browser_record_start. "
            "Then open https://example.com in a new tab and wait for it to load. "
            "Navigate to https://www.iana.org and wait for it to load. "
            "Stop recording with browser_record_stop. "
            "Save the recording to /tmp/bench_recording.json with browser_record_save. "
            "Tell me how many actions were recorded."
        ),
        verifications=[
            BrowserStateCheck("tab exists", verify_tab_exists),
        ],
        expected_tools=[
            "mcp__zenleap-browser__browser_record_start",
            "mcp__zenleap-browser__browser_create_tab",
            "mcp__zenleap-browser__browser_navigate",
            "mcp__zenleap-browser__browser_record_stop",
            "mcp__zenleap-browser__browser_record_save",
        ],
        max_turns=15,
        max_budget_usd=0.35,
        difficulty="medium",
        tags=["smoke", "phase9", "recording"],
    ),
]
