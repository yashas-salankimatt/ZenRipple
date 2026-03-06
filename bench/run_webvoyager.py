"""WebVoyager benchmark runner with parallel execution and resume support.

Usage:
    # Run 10 tasks, 5 at a time (default concurrency)
    uv run --project bench python -m bench.run_webvoyager --tasks 10

    # Resume from where we left off
    uv run --project bench python -m bench.run_webvoyager --tasks 10 --resume

    # Run all remaining tasks
    uv run --project bench python -m bench.run_webvoyager --resume

    # Adjust concurrency
    uv run --project bench python -m bench.run_webvoyager --tasks 10 --concurrency 3

    # Filter by website
    uv run --project bench python -m bench.run_webvoyager --tasks 5 --site Amazon --resume

    # Show current progress without running
    uv run --project bench python -m bench.run_webvoyager --status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from bench.loaders.webvoyager import load_tasks, tasks_to_scenarios
from bench.metrics import MetricsCollector
from bench.runner import BenchmarkRunner
from bench.verify import BrowserVerifier

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def log(msg: str = ""):
    """Print to stderr (stdout is captured by Claude Agent SDK's query())."""
    print(msg, file=sys.stderr, flush=True)


DATA_FILE = PROJECT_ROOT / "bench" / "data" / "webvoyager_full.jsonl"
PROGRESS_FILE = PROJECT_ROOT / "bench" / "results" / "webvoyager_progress.json"


def load_progress() -> dict:
    """Load progress from disk."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed": {}, "started_at": None}


def save_progress(progress: dict):
    """Save progress to disk."""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def print_status(progress: dict, total_tasks: int):
    """Print current benchmark progress."""
    completed = progress.get("completed", {})
    passed = sum(1 for v in completed.values() if v["passed"])
    failed = len(completed) - passed
    remaining = total_tasks - len(completed)
    total_cost = sum(v.get("cost_usd", 0) or 0 for v in completed.values())
    total_duration = sum(v.get("duration_ms", 0) or 0 for v in completed.values())

    log(f"WebVoyager Benchmark Progress")
    log(f"{'=' * 50}")
    log(f"Total tasks:     {total_tasks}")
    log(f"Completed:       {len(completed)}")
    log(f"  Passed:        {passed}")
    log(f"  Failed:        {failed}")
    log(f"Remaining:       {remaining}")
    if completed:
        log(f"Pass rate:       {passed / len(completed):.1%}")
    log(f"Total cost:      ${total_cost:.2f}")
    log(f"Total duration:  {total_duration / 1000:.0f}s")

    if progress.get("started_at"):
        started = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(progress["started_at"])
        )
        log(f"Started:         {started}")

    # Per-site breakdown
    if completed:
        sites: dict[str, dict] = {}
        for v in completed.values():
            site = v.get("website", "unknown")
            if site not in sites:
                sites[site] = {"passed": 0, "failed": 0, "total": 0}
            sites[site]["total"] += 1
            if v["passed"]:
                sites[site]["passed"] += 1
            else:
                sites[site]["failed"] += 1

        log(f"\nPer-site breakdown:")
        log(f"  {'Site':<25s} {'Pass':>5s} {'Fail':>5s} {'Total':>6s} {'Rate':>6s}")
        log(f"  {'-' * 47}")
        for site in sorted(sites, key=lambda s: sites[s]["total"], reverse=True):
            s = sites[site]
            rate = s["passed"] / s["total"] if s["total"] else 0
            log(
                f"  {site:<25s} {s['passed']:>5d} {s['failed']:>5d} "
                f"{s['total']:>6d} {rate:>5.0%}"
            )


async def run(args: argparse.Namespace):
    """Run the benchmark."""
    # Load tasks
    data_file = Path(args.data) if args.data else DATA_FILE
    if not data_file.exists():
        log(f"Data file not found: {data_file}")
        log("Download the WebVoyager dataset first:")
        log(
            "  curl -sL https://raw.githubusercontent.com/MinorJerry/WebVoyager/"
            "main/data/WebVoyager_data.jsonl -o bench/data/webvoyager_full.jsonl"
        )
        sys.exit(1)

    all_tasks = load_tasks(data_file)

    # Filter by site if requested
    if args.site:
        site_lower = args.site.lower()
        all_tasks = [
            t for t in all_tasks if site_lower in t.website.lower()
        ]
        if not all_tasks:
            log(f"No tasks found for site '{args.site}'")
            sys.exit(1)

    # Status only
    if args.status:
        progress = load_progress()
        print_status(progress, len(all_tasks))
        return

    # Load or reset progress
    if args.resume:
        progress = load_progress()
    else:
        progress = {"completed": {}, "started_at": time.time()}
        save_progress(progress)

    if not progress.get("started_at"):
        progress["started_at"] = time.time()

    # Find tasks that haven't been completed yet
    completed_ids = set(progress["completed"].keys())
    pending_tasks = [t for t in all_tasks if t.task_id not in completed_ids]

    if not pending_tasks:
        log("All tasks completed!")
        print_status(progress, len(all_tasks))
        return

    # Limit to --tasks N
    if args.tasks:
        pending_tasks = pending_tasks[: args.tasks]

    log(
        f"Running {len(pending_tasks)} task(s) "
        f"({len(completed_ids)} already completed, "
        f"{len(all_tasks) - len(completed_ids) - len(pending_tasks)} remaining after this batch)"
    )
    log()

    # Convert to scenarios
    scenarios = tasks_to_scenarios(
        pending_tasks,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
        timeout_seconds=args.timeout,
    )

    # Build task_id lookup (scenario.id is "wv-{task_id}")
    task_by_scenario: dict[str, type] = {}
    for task, scenario in zip(pending_tasks, scenarios):
        task_by_scenario[scenario.id] = task

    # Run — each task gets its own ZenRipple session (no shared state)
    concurrency = args.concurrency
    collector = MetricsCollector()
    verifier = BrowserVerifier()
    runner = BenchmarkRunner(collector, verifier)

    passed_count = 0
    failed_count = 0
    batch_cost = 0.0
    progress_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(i: int, scenario, task):
        nonlocal passed_count, failed_count, batch_cost

        async with semaphore:
            log(
                f"[{i}/{len(scenarios)}] {task.website} | {task.task_id}"
            )
            log(f"  {task.intent[:100]}")

            try:
                result = await runner.run_scenario(scenario)
            except Exception as e:
                log(f"  ERROR: {e}")
                async with progress_lock:
                    progress["completed"][task.task_id] = {
                        "passed": False,
                        "website": task.website,
                        "error": str(e),
                        "cost_usd": 0,
                        "duration_ms": 0,
                        "timestamp": time.time(),
                    }
                    save_progress(progress)
                failed_count += 1
                return

            cost = result.total_cost_usd or 0
            batch_cost += cost
            status = "PASS" if result.passed else "FAIL"

            if result.passed:
                passed_count += 1
            else:
                failed_count += 1

            log(
                f"  {status} | {result.duration_ms / 1000:.1f}s | "
                f"${cost:.4f} | {result.tool_call_count} tools"
            )
            if not result.passed and result.error:
                log(f"  Error: {result.error[:150]}")
            if result.agent_response:
                resp = result.agent_response[:120].replace("\n", " ")
                log(f"  Answer: {resp}")
            log()

            async with progress_lock:
                progress["completed"][task.task_id] = {
                    "passed": result.passed,
                    "website": task.website,
                    "cost_usd": cost,
                    "duration_ms": result.duration_ms,
                    "tool_calls": result.tool_call_count,
                    "error": result.error,
                    "timestamp": time.time(),
                }
                save_progress(progress)

    log(f"Concurrency: {concurrency}")

    try:
        coros = [
            run_one(i, scenario, task_by_scenario[scenario.id])
            for i, scenario in enumerate(scenarios, 1)
        ]
        await asyncio.gather(*coros)
    except KeyboardInterrupt:
        log("\nInterrupted. Progress saved.")
    finally:
        await verifier.close()

    # Summary
    total_done = len(progress["completed"])
    total_passed = sum(1 for v in progress["completed"].values() if v["passed"])

    log(f"{'=' * 50}")
    log(f"Batch:  {passed_count} passed, {failed_count} failed (${batch_cost:.2f})")
    if total_done > 0:
        log(
            f"Overall: {total_passed}/{total_done} passed "
            f"({total_passed / total_done:.0%}), "
            f"{len(all_tasks) - total_done} remaining"
        )
    log(f"Resume with: uv run --project bench python -m bench.run_webvoyager --tasks {args.tasks or 10} --resume")


def main():
    parser = argparse.ArgumentParser(
        description="Run the WebVoyager benchmark with resume support"
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=None,
        help="Number of tasks to run (default: all remaining)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous progress (without this flag, starts fresh)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current progress without running any tasks",
    )
    parser.add_argument(
        "--site",
        type=str,
        default=None,
        help="Filter tasks by website name (e.g. Amazon, GitHub, ESPN)",
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help="Path to WebVoyager data file (default: bench/data/webvoyager_full.jsonl)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=30,
        help="Max agent turns per task (default: 30)",
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=1.00,
        help="Max budget per task in USD (default: 1.00)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per task in seconds (default: 300)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Number of tasks to run in parallel (default: 5)",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
