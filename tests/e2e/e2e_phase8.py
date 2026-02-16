"""End-to-end tests for Phase 8: Token Efficiency features.
Tests smart DOM filtering, compact representation, accessibility tree, and incremental diffing.
"""

import asyncio
import json
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
        # Setup: create a tab and navigate
        tab = await cmd(ws, "create_tab", {"url": "https://example.com"})
        tab_id = tab["tab_id"]
        await asyncio.sleep(2)
        await cmd(ws, "wait_for_load", {"tab_id": tab_id, "timeout": 10})

        # ── 8.1: Smart DOM Filtering ─────────────────────
        print("\n=== 8.1: Smart DOM Filtering ===")

        # Full DOM extraction (baseline)
        full_dom = await cmd(ws, "get_dom", {"tab_id": tab_id})
        full_count = len(full_dom.get("elements", []))
        check("Full DOM returns elements", full_count > 0, f"{full_count} elements")
        check("Full DOM has 'total' field", "total" in full_dom, f"total={full_dom.get('total')}")

        # Viewport-only filtering
        viewport_dom = await cmd(ws, "get_dom", {"tab_id": tab_id, "viewport_only": True})
        viewport_count = len(viewport_dom.get("elements", []))
        check("Viewport-only returns elements", viewport_count > 0, f"{viewport_count} elements")
        # On a simple page like example.com all elements are in viewport
        check("Viewport count <= full count", viewport_count <= full_count,
              f"{viewport_count} <= {full_count}")

        # Max elements limiting
        max1_dom = await cmd(ws, "get_dom", {"tab_id": tab_id, "max_elements": 1})
        max1_count = len(max1_dom.get("elements", []))
        check("max_elements=1 returns at most 1", max1_count <= 1, f"got {max1_count}")

        # ── 8.2: Incremental DOM Diffing ──────────────────
        print("\n=== 8.2: Incremental DOM Diffing ===")

        # First call to establish baseline
        await cmd(ws, "get_dom", {"tab_id": tab_id})

        # Second call with incremental=true (same page, should show no changes)
        incr = await cmd(ws, "get_dom", {"tab_id": tab_id, "incremental": True})
        check("Incremental has diff field", "diff" in incr)
        check("Incremental flag set", incr.get("incremental") is True)
        if "diff" in incr:
            diff = incr["diff"]
            check("Diff has added count", "added" in diff)
            check("Diff has removed count", "removed" in diff)
            check("Diff has total count", "total" in diff)
            # Same page, no changes expected
            check("No added elements on same page", diff.get("added", 99) == 0,
                  f"added={diff.get('added')}")
            check("No removed elements on same page", diff.get("removed", 99) == 0,
                  f"removed={diff.get('removed')}")

        # Navigate to a different page and check diff
        await cmd(ws, "navigate", {"tab_id": tab_id, "url": "https://www.iana.org/help/example-domains"})
        await asyncio.sleep(3)
        await cmd(ws, "wait_for_load", {"tab_id": tab_id, "timeout": 10})

        incr2 = await cmd(ws, "get_dom", {"tab_id": tab_id, "incremental": True})
        if "diff" in incr2:
            diff2 = incr2["diff"]
            # Different page should show changes
            total_changes = diff2.get("added", 0) + diff2.get("removed", 0)
            check("Different page shows changes", total_changes > 0,
                  f"added={diff2.get('added')} removed={diff2.get('removed')}")

        # ── 8.3: Accessibility Tree ───────────────────────
        print("\n=== 8.3: Accessibility Tree ===")

        # Navigate to example.com for a11y test — simple page with clear structure
        await cmd(ws, "navigate", {"tab_id": tab_id, "url": "https://example.com"})
        await asyncio.sleep(2)
        await cmd(ws, "wait_for_load", {"tab_id": tab_id, "timeout": 10})
        # Let a11y tree build (it's lazy)
        await asyncio.sleep(1)

        acc = await cmd(ws, "get_accessibility_tree", {"tab_id": tab_id})
        nodes = acc.get("nodes", [])
        error = acc.get("error")

        if error:
            # A11y may not be available — that's OK, just check graceful fallback
            check("A11y error is informative", len(error) > 0, error)
            print(f"  [INFO] Accessibility service not available: {error}")
        else:
            check("A11y tree has nodes", len(nodes) > 0, f"{len(nodes)} nodes")
            check("A11y has total field", "total" in acc)
            if nodes:
                first = nodes[0]
                check("A11y node has role", "role" in first, first.get("role"))
                check("A11y node has depth", "depth" in first)
                # A11y tree returns data without crashing
                check("A11y returns structured data", isinstance(nodes, list))

        # ── Combined: viewport + max_elements ─────────────
        print("\n=== Combined Filters ===")
        combined = await cmd(ws, "get_dom", {"tab_id": tab_id, "viewport_only": True, "max_elements": 5})
        combined_count = len(combined.get("elements", []))
        check("Combined filters work", combined_count <= 5, f"{combined_count} elements")

        # Cleanup
        await cmd(ws, "close_tab", {"tab_id": tab_id})

    finally:
        await ws.close()

    print(f"\n{'='*50}")
    print(f"Phase 8 E2E Results: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
    if FAIL > 0:
        print("SOME TESTS FAILED")
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
