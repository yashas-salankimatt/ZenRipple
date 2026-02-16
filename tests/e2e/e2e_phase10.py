"""End-to-end tests for Phase 10: Chrome eval, drag-and-drop, reflection.
Requires Zen Browser running with ZenLeap Agent v1.0.0.
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
        # ── 10.1: Chrome-Context Eval ────────────────────
        print("\n=== 10.1: Chrome-Context Eval ===")

        # Eval app name
        result = await cmd(ws, "eval_chrome", {"expression": "Services.appinfo.name"})
        check("eval_chrome returns app name", "result" in result, f"got {result}")
        app_name = result.get("result", "")
        check("App name is Zen", "zen" in str(app_name).lower() or "Zen" in str(app_name), f"name={app_name}")

        # Eval tab count
        result = await cmd(ws, "eval_chrome", {"expression": "gBrowser.tabs.length"})
        check("eval_chrome returns tab count", "result" in result)
        tab_count = result.get("result", 0)
        check("Tab count is a number", isinstance(tab_count, (int, float)), f"got {type(tab_count).__name__}")

        # Eval error handling
        result = await cmd(ws, "eval_chrome", {"expression": "nonexistentVar.prop"})
        check("eval_chrome error has error field", "error" in result, f"keys={list(result.keys())}")

        # ── 10.2: Drag-and-Drop ──────────────────────────
        print("\n=== 10.2: Drag-and-Drop ===")

        # Open a page for drag testing
        tab = await cmd(ws, "create_tab", {"url": "https://example.com"})
        tab_id = tab.get("tab_id")
        await asyncio.sleep(2)
        await cmd(ws, "wait_for_load", {"tab_id": tab_id, "timeout": 10})

        # Drag coordinates
        result = await cmd(ws, "drag_coordinates", {
            "startX": 100, "startY": 100,
            "endX": 300, "endY": 300,
            "steps": 5,
        })
        check("drag_coordinates succeeds", result.get("success"), f"result={result}")
        check("drag_coordinates has from/to", "from" in result and "to" in result)

        # Clean up
        await cmd(ws, "close_tab", {"tab_id": tab_id})

        # ── 10.3: Reflection ─────────────────────────────
        print("\n=== 10.3: Reflection (via raw commands) ===")

        # Open a page for reflection
        tab = await cmd(ws, "create_tab", {"url": "https://example.com"})
        tab_id = tab.get("tab_id")
        await asyncio.sleep(2)
        await cmd(ws, "wait_for_load", {"tab_id": tab_id, "timeout": 10})

        # Screenshot
        screenshot = await cmd(ws, "screenshot", {"tab_id": tab_id})
        check("Screenshot returns image", bool(screenshot.get("image")))

        # Page text
        text = await cmd(ws, "get_page_text", {"tab_id": tab_id})
        check("Page text contains content", len(text.get("text", "")) > 0, f"len={len(text.get('text', ''))}")
        check("Page text has Example Domain", "Example Domain" in text.get("text", ""))

        # Page info
        info = await cmd(ws, "get_page_info", {"tab_id": tab_id})
        check("Page info has URL", "example.com" in info.get("url", ""))
        check("Page info has title", bool(info.get("title")))

        # Clean up
        await cmd(ws, "close_tab", {"tab_id": tab_id})

    except Exception as e:
        print(f"\n  [ERROR] {e}")
        FAIL += 1
    finally:
        await ws.close()

    print(f"\n{'='*50}")
    print(f"Phase 10 E2E: {PASS} passed, {FAIL} failed")
    print(f"{'='*50}")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    raise SystemExit(0 if ok else 1)
