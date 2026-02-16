# Phase 8: Accessibility Tree Extraction

## Overview

Extract the browser's accessibility tree for the current page. Provides semantic understanding of page structure (roles, names, values) without relying on visual rendering or CSS.

## MCP Tool

### `browser_get_accessibility_tree(tab_id?, frame_id?)`

Returns a tree of accessible nodes with:
- `role`: semantic role (document, heading, link, pushbutton, entry, etc.)
- `name`: accessible name (text content, aria-label, etc.)
- `value`: current value (for form fields)
- `depth`: nesting depth for tree structure

Example output:
```
Accessibility tree (12 nodes):
[document] Example Domain
  [heading] Example Domain
  [paragraph]
    [text_leaf] This domain is for use...
  [link] More information...
```

## Architecture

Uses `nsIAccessibilityService` (Gecko-specific) from the content actor:

```javascript
const accService = Cc['@mozilla.org/accessibilityService;1']
  ?.getService(Ci.nsIAccessibilityService);
const accDoc = accService.getAccessibleFor(win.document);
// Walk tree: acc.role, acc.name, acc.value, acc.childCount, acc.getChildAt(i)
```

Role IDs are mapped to human-readable names (button, link, heading, etc.).

## Limitations

- **Service availability**: The accessibility service may not be available in all builds or configurations. The tool returns a graceful error message in this case.
- **Lazy initialization**: The a11y tree is built lazily — the first call may return incomplete results. Wait a moment after page load for best results.
- **Max 500 nodes**: Output is capped to prevent excessive token usage on complex pages.
- **Whitespace nodes filtered**: `whitespace` and empty `text_leaf` nodes are excluded for cleaner output.
