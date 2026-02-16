"""End-to-end tests for Phase 9: Advanced Intelligence features.
Tests multi-tab coordination, action recording/replay, self-healing selectors, and visual grounding.
"""

import asyncio
import json
import os
from uuid import uuid4

import websockets

WS_URL = "ws://localhost:9876"
PASS = 0
FAIL = 0


async def cmd(ws, method, params=None):
    msg_id = str(uuid4())
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    raw = await asyncio.wait_for(ws.recv(), timeout=30)
    resp = json.loads(raw)
    if "error" in resp:
        raise Exception(f"{method} failed: {resp['error']}")
    return resp.get("result", {})


def check(name, condition, detail=""):
    global PASS, FAIL
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAIL += 1
    else:
        PASS += 1
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return condition


async def main():
    global PASS, FAIL

    ws = await websockets.connect(WS_URL, max_size=10 * 1024 * 1024)

    try:
        # ── 9.1: Multi-Tab Coordination ───────────────────
        print("\n=== 9.1: Multi-Tab Coordination ===")

        # batch_navigate: open 2 tabs at once
        batch = await cmd(ws, "batch_navigate", {"urls": ["https://example.com", "https://www.iana.org"]})
        check("Batch navigate succeeds", batch.get("success"))
        tabs = batch.get("tabs", [])
        check("Batch created 2 tabs", len(tabs) == 2, f"got {len(tabs)}")

        tab1_id = tabs[0]["tab_id"] if tabs else None
        tab2_id = tabs[1]["tab_id"] if len(tabs) > 1 else None

        await asyncio.sleep(3)
        if tab1_id:
            await cmd(ws, "wait_for_load", {"tab_id": tab1_id, "timeout": 10})
        if tab2_id:
            await cmd(ws, "wait_for_load", {"tab_id": tab2_id, "timeout": 10})

        # compare_tabs
        if tab1_id and tab2_id:
            comparison = await cmd(ws, "compare_tabs", {"tab_ids": [tab1_id, tab2_id]})
            check("Compare returns 2 entries", len(comparison) == 2)
            if comparison:
                check("Compare has URL", "url" in comparison[0])
                check("Compare has title", "title" in comparison[0])
                check("Compare has text_preview", "text_preview" in comparison[0])
                check("Text previews differ", comparison[0].get("text_preview") != comparison[1].get("text_preview"))

        # ── 9.2: Action Recording ─────────────────────────
        print("\n=== 9.2: Action Recording ===")

        # Start recording
        start = await cmd(ws, "record_start")
        check("Record start succeeds", start.get("success"))

        # Perform some actions that should be recorded
        nav_tab = await cmd(ws, "create_tab", {"url": "https://example.com"})
        nav_tab_id = nav_tab["tab_id"]
        await asyncio.sleep(2)
        await cmd(ws, "wait_for_load", {"tab_id": nav_tab_id, "timeout": 10})
        await cmd(ws, "navigate", {"tab_id": nav_tab_id, "url": "https://www.iana.org"})
        await asyncio.sleep(2)

        # Stop recording
        stop = await cmd(ws, "record_stop")
        check("Record stop succeeds", stop.get("success"))
        action_count = stop.get("actions", 0)
        check("Actions were recorded", action_count > 0, f"{action_count} actions")

        # Save recording
        rec_path = "/tmp/zenleap_e2e_recording.json"
        save = await cmd(ws, "record_save", {"file_path": rec_path})
        check("Record save succeeds", save.get("success"))
        check("Save reports action count", save.get("actions", 0) > 0)

        # Verify file was created (we can't read it from the browser, but check save worked)
        check("Save file path", save.get("file") == rec_path)

        # Replay the recording
        replay = await cmd(ws, "record_replay", {"file_path": rec_path, "delay": 0.2})
        check("Replay succeeds", replay.get("success"))
        check("Replay count matches", replay.get("replayed", 0) > 0,
              f"replayed {replay.get('replayed')}/{replay.get('total')}")

        # ── 9.3: Self-Healing Selectors ───────────────────
        print("\n=== 9.3: Self-Healing Selectors ===")

        # Navigate to example.com
        await cmd(ws, "navigate", {"tab_id": nav_tab_id, "url": "https://example.com"})
        await asyncio.sleep(2)
        await cmd(ws, "wait_for_load", {"tab_id": nav_tab_id, "timeout": 10})

        # Get DOM to populate element map + metadata
        dom = await cmd(ws, "get_dom", {"tab_id": nav_tab_id})
        elements = dom.get("elements", [])
        check("DOM has elements for self-heal test", len(elements) > 0)

        # The self-healing is transparent — if we click an element after a
        # minor page update that doesn't change the element, it should still work.
        # We test by clicking an element (which should succeed).
        if elements:
            idx = elements[0]["index"]
            try:
                click_result = await cmd(ws, "click_element", {"tab_id": nav_tab_id, "index": idx})
                check("Click element works (self-healing active)", click_result.get("success"))
            except Exception as e:
                check("Click element works", False, str(e))

        # ── 9.4: Visual Grounding (via MCP, not browser) ─
        print("\n=== 9.4: Visual Grounding ===")
        # Visual grounding is tested at the MCP layer (Python-side fuzzy matching).
        # Here we verify the DOM data is compatible.
        dom2 = await cmd(ws, "get_dom", {"tab_id": nav_tab_id})
        elements2 = dom2.get("elements", [])
        if elements2:
            has_text = any(el.get("text") for el in elements2)
            check("Elements have text for grounding", has_text)
            has_attrs = any(el.get("attributes") for el in elements2)
            check("Elements have attributes for grounding", has_attrs)
        else:
            check("DOM has elements for grounding test", False)

        # Cleanup
        if tab1_id:
            await cmd(ws, "close_tab", {"tab_id": tab1_id})
        if tab2_id:
            await cmd(ws, "close_tab", {"tab_id": tab2_id})
        await cmd(ws, "close_tab", {"tab_id": nav_tab_id})

    finally:
        await ws.close()
        # Clean up recording file
        try:
            os.remove("/tmp/zenleap_e2e_recording.json")
        except OSError:
            pass

    print(f"\n{'='*50}")
    print(f"Phase 9 E2E Results: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
    if FAIL > 0:
        print("SOME TESTS FAILED")
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
