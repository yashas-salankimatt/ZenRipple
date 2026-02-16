"""Parallel session test: runs two scenarios concurrently to verify multi-session support.

Usage:
    uv run --project bench python -m bench.parallel_test
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from bench.metrics import MetricsCollector, RunResult
from bench.runner import BenchmarkRunner
from bench.scenarios.amazon_cart import AMAZON_SCENARIOS
from bench.scenarios.linkedin_vc import LINKEDIN_VC_SCENARIOS
from bench.verify import BrowserVerifier


def print_result_detail(label: str, result: RunResult | Exception):
    """Print full detail for a scenario result including tool trace."""
    print(f"\n{'━' * 70}")
    print(f"  {label}")
    print(f"{'━' * 70}")

    if isinstance(result, Exception):
        print(f"  EXCEPTION: {result}")
        return

    status = "PASS" if result.passed else "FAIL"
    cost = f"${result.total_cost_usd:.4f}" if result.total_cost_usd else "N/A"
    print(f"  Status: {status}")
    print(f"  Duration: {result.duration_ms / 1000:.1f}s")
    print(f"  Cost: {cost}")
    print(f"  Tool calls: {result.tool_call_count}")
    print(f"  Turns: {result.num_turns}")

    if result.error:
        print(f"  Error: {result.error}")
    if result.failure_category:
        print(f"  Failure category: {result.failure_category}")

    # Verification results
    if result.verification_results:
        print(f"\n  Verifications:")
        for check, passed in result.verification_results.items():
            sym = "✓" if passed else "✗"
            print(f"    [{sym}] {check}")

    # Full tool trace
    if result.tool_call_trace:
        print(f"\n  Tool Trace ({len(result.tool_call_trace)} calls):")
        print(f"  {'─' * 60}")
        for i, tc in enumerate(result.tool_call_trace, 1):
            tool_short = tc["tool"].replace("mcp__zenleap-browser__browser_", "")
            inp = json.dumps(tc["input"], default=str)
            if len(inp) > 150:
                inp = inp[:150] + "..."
            preview = tc.get("result_preview", "") or ""
            if len(preview) > 300:
                preview = preview[:300] + "..."
            print(f"    {i:3d}. {tool_short}")
            print(f"         input: {inp}")
            if preview:
                # Indent multi-line previews
                lines = preview.split("\n")
                print(f"         result: {lines[0]}")
                for line in lines[1:5]:  # Show up to 5 lines of result
                    print(f"                 {line}")
                if len(lines) > 5:
                    print(f"                 ... ({len(lines) - 5} more lines)")
        print(f"  {'─' * 60}")

    # Agent's final response
    if result.agent_response:
        resp = result.agent_response
        print(f"\n  Agent Response:")
        # Print full response, wrapped
        for line in resp.split("\n"):
            print(f"    {line}")

    print()


async def run_parallel():
    """Run LinkedIn and Amazon scenarios in parallel with separate sessions."""
    linkedin = LINKEDIN_VC_SCENARIOS[0]  # li-001
    amazon = AMAZON_SCENARIOS[0]  # amz-001

    print(f"{'=' * 70}")
    print(f"  PARALLEL SESSION TEST")
    print(f"{'=' * 70}")
    print(f"  Scenario A: {linkedin.id} — {linkedin.name}")
    print(f"  Scenario B: {amazon.id} — {amazon.name}")
    print(f"  Started at: {time.strftime('%H:%M:%S')}")
    print(f"{'=' * 70}")
    print()

    # Each scenario gets its own runner + verifier (independent WS connections/sessions)
    collector_a = MetricsCollector()
    collector_b = MetricsCollector()
    verifier_a = BrowserVerifier()
    verifier_b = BrowserVerifier()
    runner_a = BenchmarkRunner(collector_a, verifier_a)
    runner_b = BenchmarkRunner(collector_b, verifier_b)

    start = time.time()

    # Run both scenarios concurrently
    print("Launching both scenarios concurrently...")
    print("  (Each gets its own MCP server subprocess + browser session)")
    print()

    result_a, result_b = await asyncio.gather(
        runner_a.run_scenario(linkedin),
        runner_b.run_scenario(amazon),
        return_exceptions=True,
    )

    elapsed = time.time() - start

    # Print detailed results for both
    print_result_detail(f"Scenario A: {linkedin.id} — {linkedin.name}", result_a)
    print_result_detail(f"Scenario B: {amazon.id} — {amazon.name}", result_b)

    # Summary
    print(f"{'=' * 70}")
    print(f"  SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total wall time: {elapsed:.1f}s")

    for label, result in [("A (LinkedIn)", result_a), ("B (Amazon)", result_b)]:
        if isinstance(result, Exception):
            print(f"  {label}: EXCEPTION")
        else:
            status = "PASS" if result.passed else "FAIL"
            cost = f"${result.total_cost_usd:.4f}" if result.total_cost_usd else "N/A"
            print(f"  {label}: {status} | {result.duration_ms / 1000:.1f}s | {cost} | {result.tool_call_count} tools")

    print(f"{'=' * 70}")

    # Cleanup
    await verifier_a.close()
    await verifier_b.close()


if __name__ == "__main__":
    asyncio.run(run_parallel())
