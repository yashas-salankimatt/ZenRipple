# Trusted Keyboard Input — nsITextInputProcessor

## Problem
Browser automation needs to type into canvas-based apps (Google Sheets) and contenteditable elements where untrusted `dispatchEvent(new KeyboardEvent(...))` is ignored.

Previous approaches that failed:
- `windowUtils.sendKeyEvent()` — removed from this Gecko build
- `sendNativeKeyEvent()` — OS-level, sends to whatever has focus (steals focus from user)
- `dispatchEvent(new KeyboardEvent(...))` — untrusted, ignored by Sheets

## Solution
`nsITextInputProcessor` (TIP) — Gecko's input method API that produces `isTrusted: true` keyboard events through the full event pipeline, targeted at a specific content window.

## Implementation
- **File**: `JS/actors/ZenLeapAgentChild.sys.mjs`
- **Methods**: `#getTextInputProcessor()`, `#typeText()`, `#pressKey()`
- **Key insight**: TIP must be created in the content process actor and initialized with `beginInputTransactionForTests(this.contentWindow)` to target a specific tab

## Critical Details
- `keyCode` must be set explicitly in `KeyboardEvent` constructor — TIP does NOT auto-compute it for printable keys (only for non-printable keys with `KEY_NON_PRINTABLE_KEY` flag)
- Google Sheets checks `keyCode` (not `key`/`code`) for character handling
- Shift modifier activated/deactivated via separate `tip.keydown(shiftEvent)`/`tip.keyup(shiftEvent)` calls
- Tab/Enter/Escape deferred via `setTimeout` to let `sendQuery` response return before actor destruction
- `el.click()` and `sendMouseEvent()` do NOT trigger focus — always call `el.focus()` after

## Tools Affected
- `browser_type` — character-by-character TIP input with Shift handling
- `browser_press_key` — single key press with modifier support
