"""LLM judge for evaluating browser agent task completion via Claude Agent SDK.

Mirrors WebVoyager's GPT-4V judge: the judge sees the task description,
the agent's final answer, and a screenshot of the browser — nothing else.
The judge reads the screenshot file using Claude Code's built-in Read tool.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import ResultMessage

PROJECT_ROOT = Path(__file__).resolve().parent.parent

JUDGE_PROMPT = """\
You are an impartial judge evaluating whether a browser automation agent \
successfully completed a web task.

## Task
{task}

## Agent's Final Answer
{agent_response}

## Screenshot
Read the screenshot file at: {screenshot_path}

After viewing the screenshot, determine if the task was completed successfully.

Consider:
- Does the browser show evidence that the task was completed?
- Is the agent's answer consistent with what's visible on the page?
- Was the core objective achieved, even if the approach was imperfect?

Respond with exactly one line:
PASS: <brief explanation>
or
FAIL: <brief explanation>"""


async def llm_judge(
    task: str,
    agent_response: str,
    screenshot_path: str,
) -> tuple[bool, str]:
    """Evaluate task completion using Claude Agent SDK.

    The judge reads the screenshot via the built-in Read tool, then
    evaluates whether the task was completed — identical to WebVoyager's
    GPT-4V judge but using Claude.

    Returns (passed, explanation).
    """
    if not screenshot_path or not os.path.exists(screenshot_path):
        return False, "No screenshot available for judge evaluation"

    prompt = JUDGE_PROMPT.format(
        task=task,
        agent_response=agent_response or "(no response)",
        screenshot_path=screenshot_path,
    )

    options = ClaudeAgentOptions(
        max_turns=5,
        max_budget_usd=0.10,
        permission_mode="bypassPermissions",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
        },
        cwd=str(PROJECT_ROOT),
    )

    result_text = ""
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                result_text = msg.result or ""
    except Exception as e:
        return False, f"Judge error: {e}"

    if not result_text:
        return False, "Judge returned empty response"

    # Extract PASS/FAIL from the response (may be buried in markdown)
    for line in result_text.strip().split("\n"):
        stripped = line.strip().lstrip("*").strip()
        if stripped.upper().startswith("PASS"):
            return True, result_text
        if stripped.upper().startswith("FAIL"):
            return False, result_text

    return False, f"Could not parse judge verdict from: {result_text}"
