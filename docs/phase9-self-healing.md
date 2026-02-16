# Phase 9: Self-Healing Selectors

## Overview

Self-healing selectors automatically recover from stale element references. When the DOM changes between a `get_dom` call and a subsequent interaction (click, fill, etc.), the system attempts to re-find the element using stored metadata rather than requiring the agent to re-run `get_dom`.

## How It Works

### Metadata Storage

When `get_dom` extracts interactive elements, it stores rich metadata for each indexed element in `#elementMeta`:

```javascript
#elementMeta.set(index, {
  tag,        // "button", "input", "a", etc.
  text,       // visible text content
  href,       // link target
  name,       // form field name
  type,       // input type ("text", "submit", etc.)
  ariaLabel,  // aria-label attribute
});
```

### Recovery Strategies

When `#getElement(index)` finds a stale reference (WeakRef returns null or element is disconnected), it calls `#tryHealElement(meta)` which tries 4 strategies in order:

1. **aria-label match**: `querySelector('[aria-label="..."]')` — most specific, rarely changes
2. **href match**: `querySelector('TAG[href="..."]')` — stable for links
3. **tag + text match**: Walk the DOM for elements with matching tag and textContent
4. **name match**: `querySelector('TAG[name="..."]')` — stable for form fields

All selectors use `CSS.escape()` to prevent injection through attribute values.

### Transparent to MCP Layer

Self-healing is entirely within the content actor (`ZenLeapAgentChild.sys.mjs`). The MCP server and command handlers see no difference — they just get a successful result instead of a "stale element" error.

## Architecture

```
MCP tool call (browser_click index=3)
  → zenleap_agent.uc.js: click_element handler
    → Actor sendQuery: ZenLeapAgent:ClickElement {index: 3}
      → ZenLeapAgentChild: #getElement(3)
        → WeakRef.deref() returns null (stale!)
        → #tryHealElement(meta) finds matching element
        → Updates WeakRef, returns healed element
      → Click succeeds
```

## Limitations

- Only works when the page has the same logical element (e.g., same button text, same link href)
- If the page has completely changed (full navigation), healing will fail and the agent must call `get_dom` again
- Healing adds a small overhead (~1-5ms) only when the original reference is stale

## Testing

Self-healing is tested in `mcp/e2e_phase9.py` (section 9.3) by clicking elements after a `get_dom` call — verifying that the click succeeds even on a live page where DOM may have mutated slightly.
