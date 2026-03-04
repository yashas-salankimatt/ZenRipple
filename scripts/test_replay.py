#!/usr/bin/env python3
"""Integration test: session replay with multi-prompt workflow.

Drives the MCP server through the JSON-RPC stdio transport,
keeping a single server process alive for the full workflow.
"""
import asyncio
import json
import os
import sys

# Ensure we can import the mcp library
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))


async def call_tool(proc, tool_name, args=None, req_id=1):
    """Send a JSON-RPC tool call and return the parsed result."""
    msg = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args or {}},
    }
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line.encode())
    await proc.stdin.drain()

    # Read lines until we get a response with our id
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError("Server closed stdout")
        text = raw.decode().strip()
        if not text:
            continue
        try:
            resp = json.loads(text)
        except json.JSONDecodeError:
            continue
        if resp.get("id") == req_id:
            if "error" in resp:
                return {"error": resp["error"]}
            # Extract text content from MCP tool result
            content = resp.get("result", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    try:
                        return json.loads(block["text"])
                    except (json.JSONDecodeError, TypeError):
                        return block["text"]
            return content


async def main():
    # Start the MCP server as a subprocess
    server_cmd = [
        sys.executable, "-m", "zenripple_mcp_server"
    ]
    env = os.environ.copy()
    env["ZENRIPPLE_SESSION_ID"] = f"replay-test-{os.getpid()}"
    env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..", "mcp")

    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "--project",
        os.path.join(os.path.dirname(__file__), "..", "mcp"),
        "python",
        os.path.join(os.path.dirname(__file__), "..", "mcp", "zenripple_mcp_server.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    req_id = 0

    def next_id():
        nonlocal req_id
        req_id += 1
        return req_id

    try:
        # Initialize the MCP connection
        init_msg = {
            "jsonrpc": "2.0",
            "id": next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "replay-test", "version": "1.0"},
            },
        }
        proc.stdin.write((json.dumps(init_msg) + "\n").encode())
        await proc.stdin.drain()

        # Read init response
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            text = raw.decode().strip()
            if not text:
                continue
            try:
                resp = json.loads(text)
                if resp.get("id") == 1:
                    print(f"✓ MCP initialized: {resp.get('result', {}).get('serverInfo', {}).get('name', 'unknown')}")
                    break
            except json.JSONDecodeError:
                continue

        # Send initialized notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write((json.dumps(notif) + "\n").encode())
        await proc.stdin.drain()
        await asyncio.sleep(0.5)

        # === STEP 1: Start replay ===
        print("\n=== Step 1: Start replay ===")
        result = await call_tool(proc, "browser_replay_start", {}, next_id())
        print(f"  Result: {json.dumps(result, indent=2)}")
        replay_dir = result.get("dir", "") if isinstance(result, dict) else ""

        # === STEP 2: Mark prompt 1 ===
        print("\n=== Step 2: Mark prompt — 'Search for protein shakes on Amazon' ===")
        result = await call_tool(proc, "browser_replay_mark_prompt",
                                 {"text": "Search for protein shakes on Amazon"}, next_id())
        print(f"  Result: {json.dumps(result, indent=2)}")

        # === STEP 3: Navigate to Amazon ===
        print("\n=== Step 3: Navigate to Amazon ===")
        result = await call_tool(proc, "browser_create_tab",
                                 {"url": "https://www.amazon.com"}, next_id())
        print(f"  create_tab: {str(result)[:120]}")

        print("  Waiting for load...")
        result = await call_tool(proc, "browser_wait_for_load", {}, next_id())
        print(f"  wait_for_load: {str(result)[:120]}")

        # === STEP 4: Type in search ===
        print("\n=== Step 4: Search for protein shakes ===")
        result = await call_tool(proc, "browser_find_element_by_description",
                                 {"description": "search input"}, next_id())
        print(f"  find search: {str(result)[:200]}")

        # Parse the first [N] index from the text response
        import re
        search_idx = 0
        match = re.search(r"\[(\d+)\]", str(result))
        if match:
            search_idx = int(match.group(1))
        print(f"  Using element index: {search_idx}")

        # Click to focus, type to fill, Enter to submit
        result = await call_tool(proc, "browser_click",
                                 {"index": search_idx}, next_id())
        print(f"  click search: {str(result)[:80]}")

        result = await call_tool(proc, "browser_type",
                                 {"text": "protein shakes"}, next_id())
        print(f"  type: {str(result)[:80]}")

        result = await call_tool(proc, "browser_press_key",
                                 {"key": "Enter"}, next_id())
        print(f"  press Enter: {str(result)[:80]}")

        # Wait for navigation to start then complete
        await asyncio.sleep(2)
        print("  Waiting for results...")
        result = await call_tool(proc, "browser_wait_for_load", {}, next_id())
        print(f"  wait_for_load: {str(result)[:120]}")

        # Scroll down to see results
        result = await call_tool(proc, "browser_scroll",
                                 {"direction": "down", "amount": 500}, next_id())
        print(f"  scroll: {str(result)[:80]}")

        # === STEP 5: Check replay status ===
        print("\n=== Step 5: Check replay status after prompt 1 ===")
        result = await call_tool(proc, "browser_replay_status", {}, next_id())
        print(f"  Status: {json.dumps(result, indent=2)}")

        # === STEP 6: Mark prompt 2 ===
        print("\n=== Step 6: Mark prompt — 'Find a typewriter on eBay' ===")
        result = await call_tool(proc, "browser_replay_mark_prompt",
                                 {"text": "Find a typewriter on eBay"}, next_id())
        print(f"  Result: {json.dumps(result, indent=2)}")

        # === STEP 7: Navigate to eBay ===
        print("\n=== Step 7: Navigate to eBay ===")
        result = await call_tool(proc, "browser_navigate",
                                 {"url": "https://www.ebay.com"}, next_id())
        print(f"  navigate: {str(result)[:120]}")

        print("  Waiting for load...")
        result = await call_tool(proc, "browser_wait_for_load", {}, next_id())
        print(f"  wait_for_load: {str(result)[:120]}")

        # Search on eBay
        result = await call_tool(proc, "browser_find_element_by_description",
                                 {"description": "search input"}, next_id())
        print(f"  find search: {str(result)[:200]}")

        search_idx = 0
        match = re.search(r"\[(\d+)\]", str(result))
        if match:
            search_idx = int(match.group(1))
        print(f"  Using element index: {search_idx}")

        result = await call_tool(proc, "browser_click",
                                 {"index": search_idx}, next_id())
        print(f"  click search: {str(result)[:80]}")

        result = await call_tool(proc, "browser_type",
                                 {"text": "typewriter"}, next_id())
        print(f"  type: {str(result)[:80]}")

        result = await call_tool(proc, "browser_press_key",
                                 {"key": "Enter"}, next_id())
        print(f"  press Enter: {str(result)[:80]}")

        await asyncio.sleep(2)
        print("  Waiting for results...")
        result = await call_tool(proc, "browser_wait_for_load", {}, next_id())
        print(f"  wait_for_load: {str(result)[:120]}")

        result = await call_tool(proc, "browser_scroll",
                                 {"direction": "down", "amount": 500}, next_id())
        print(f"  scroll: {str(result)[:80]}")

        # === STEP 8: Final status ===
        print("\n=== Step 8: Final replay status ===")
        result = await call_tool(proc, "browser_replay_status", {}, next_id())
        print(f"  Status: {json.dumps(result, indent=2)}")

        # === STEP 9: Save full session video ===
        output_path = "/tmp/zenripple_replay_test_session.mp4"
        print(f"\n=== Step 9: Save full session video → {output_path} ===")
        result = await call_tool(proc, "browser_replay_save_video",
                                 {"output_path": output_path, "scope": "session"}, next_id())
        print(f"  Result: {json.dumps(result, indent=2)}")

        # === STEP 10: Save last prompt only ===
        output_path2 = "/tmp/zenripple_replay_test_last_prompt.mp4"
        print(f"\n=== Step 10: Save last prompt video → {output_path2} ===")
        result = await call_tool(proc, "browser_replay_save_video",
                                 {"output_path": output_path2, "scope": "last_prompt"}, next_id())
        print(f"  Result: {json.dumps(result, indent=2)}")

        # === STEP 11: Save prompt 0 only ===
        output_path3 = "/tmp/zenripple_replay_test_prompt0.mp4"
        print(f"\n=== Step 11: Save prompt 0 video → {output_path3} ===")
        result = await call_tool(proc, "browser_replay_save_video",
                                 {"output_path": output_path3, "scope": "prompt", "prompt_index": 0}, next_id())
        print(f"  Result: {json.dumps(result, indent=2)}")

        # === STEP 12: Stop replay ===
        print("\n=== Step 12: Stop replay ===")
        result = await call_tool(proc, "browser_replay_stop", {}, next_id())
        print(f"  Result: {json.dumps(result, indent=2)}")

        # === STEP 13: List frame files ===
        if replay_dir:
            print(f"\n=== Step 13: Inspect replay directory: {replay_dir} ===")
            import glob
            frames = sorted(glob.glob(os.path.join(replay_dir, "frame_*.jpg")))
            print(f"  Frame files: {len(frames)}")
            for f in frames[:5]:
                size = os.path.getsize(f)
                print(f"    {os.path.basename(f)}: {size:,} bytes")
            if len(frames) > 5:
                print(f"    ... and {len(frames) - 5} more")

            manifest_path = os.path.join(replay_dir, "manifest.json")
            if os.path.exists(manifest_path):
                with open(manifest_path) as mf:
                    manifest = json.load(mf)
                print(f"  Manifest: {len(manifest.get('frames', []))} frames, {len(manifest.get('prompts', []))} prompts")

        # Check output files
        print("\n=== Output files ===")
        for p in [output_path, output_path2, output_path3]:
            if os.path.exists(p):
                size = os.path.getsize(p)
                print(f"  ✓ {p}: {size:,} bytes")
            else:
                print(f"  ✗ {p}: NOT FOUND")

        print("\n✓ Replay integration test complete!")

    finally:
        proc.stdin.close()
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
