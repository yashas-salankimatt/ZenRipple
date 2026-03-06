"""WebVoyager benchmark task loader.

Loads tasks from JSON and converts them into bench Scenario objects
with LLM-judge verification (mirroring WebVoyager's GPT-4V evaluation).

Supports the common JSON formats found in WebVoyager implementations:

  Format A (original):  {"task_id": "...", "web_name": "...", "task": "...", "url": "..."}
  Format B (variants):  {"id": "...", "website": "...", "intent": "...", "start_url": "..."}
  Format C (ques-style): {"id": "...", "web": "...", "ques": "...", "url": "..."}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.judge import llm_judge
from bench.scenario import BrowserStateCheck, Scenario, ScenarioCategory


@dataclass
class WebVoyagerTask:
    """A single WebVoyager benchmark task."""

    task_id: str
    website: str
    url: str
    intent: str


def _normalize_task(raw: dict[str, Any], index: int) -> WebVoyagerTask:
    """Normalize varying JSON formats into a single WebVoyagerTask."""
    task_id = str(
        raw.get("task_id")
        or raw.get("id")
        or f"wv-{index}"
    )
    website = str(
        raw.get("web_name")
        or raw.get("website")
        or raw.get("web")
        or "unknown"
    )
    url = str(
        raw.get("url")
        or raw.get("start_url")
        or raw.get("web")
        or ""
    )
    intent = str(
        raw.get("task")
        or raw.get("intent")
        or raw.get("ques")
        or ""
    )
    if not intent:
        raise ValueError(f"Task at index {index} has no intent/task/ques field")
    if not url:
        raise ValueError(f"Task at index {index} has no url/start_url field")
    return WebVoyagerTask(task_id=task_id, website=website, url=url, intent=intent)


def load_tasks(path: str | Path) -> list[WebVoyagerTask]:
    """Load WebVoyager tasks from a JSON or JSONL file.

    Supports:
    - JSON array files (.json)
    - JSONL files (.jsonl) with one JSON object per line
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"WebVoyager tasks file not found: {path}")

    if path.suffix == ".jsonl":
        data = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    data.append(json.loads(line))
    else:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON array, got {type(data).__name__}")

    return [_normalize_task(raw, i) for i, raw in enumerate(data)]


def _make_judge_check(task_intent: str) -> BrowserStateCheck:
    """Create a BrowserStateCheck that uses the LLM judge."""

    async def check(state: dict[str, Any]) -> bool:
        agent_response = state.get("agent_response", "")
        screenshot_path = state.get("screenshot_path", "")
        passed, _reason = await llm_judge(
            task_intent, agent_response, screenshot_path
        )
        return passed

    return BrowserStateCheck(
        description="WebVoyager LLM judge",
        check_fn=check,
    )


def tasks_to_scenarios(
    tasks: list[WebVoyagerTask],
    *,
    max_turns: int = 30,
    max_budget_usd: float = 1.00,
    timeout_seconds: int = 300,
) -> list[Scenario]:
    """Convert WebVoyager tasks into benchmark Scenarios."""
    scenarios: list[Scenario] = []
    for task in tasks:
        tag = task.website.lower().replace(" ", "_")
        scenarios.append(
            Scenario(
                id=f"wv-{task.task_id}",
                name=f"WebVoyager: {task.intent[:60]}",
                category=ScenarioCategory.MULTI_STEP,
                prompt=(
                    f"Navigate to {task.url} and complete this task:\n\n"
                    f"{task.intent}\n\n"
                    "IMPORTANT: You MUST use the browser tools (browser_*) to "
                    "interact with the page. Do NOT use WebFetch, Bash, curl, "
                    "or any other non-browser tools. Use browser_create_tab to "
                    "open the URL, then use browser tools to read content and "
                    "interact with the page.\n\n"
                    "When you are done, clearly state your final answer."
                ),
                verifications=[_make_judge_check(task.intent)],
                max_turns=max_turns,
                max_budget_usd=max_budget_usd,
                timeout_seconds=timeout_seconds,
                tags=["webvoyager", tag],
                difficulty="hard",
            )
        )
    return scenarios
