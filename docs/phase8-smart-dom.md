# Phase 8: Smart DOM Filtering & Compact Representation

## Overview

Reduce token usage when working with complex pages by filtering DOM elements and using a compact output format. The full `browser_get_dom` output includes bounding boxes and full attributes, which is expensive for large pages. These features provide alternatives.

## MCP Tools

### `browser_get_dom` (Enhanced)

New optional parameters:
- `viewport_only: bool` — only return elements visible in the current viewport (skips elements where `rect.bottom < 0` or `rect.top > viewportHeight`)
- `max_elements: int` — limit the number of elements returned (0 = unlimited)
- `incremental: bool` — return a diff against the previous `get_dom` call

### `browser_get_elements_compact`

A token-efficient alternative to `browser_get_dom`. Returns one line per element:

```
URL: example.com | Title: Example Domain
[0] Example Domain (a →https://www.iana.org/domains/examples)
[1] More information... (a →https://www.iana.org/domains/reserved)
```

Accepts `viewport_only` and `max_elements` parameters. 5-10x fewer tokens than full DOM output.

### Incremental DOM Diffing

With `incremental=true`, `browser_get_dom` includes a `diff` section showing what changed since the last call:

```
Changes: +3 -1
Added:
  [5] <button>New Button</button>
  [6] <input type="text">
Removed:
  <a>Old Link</a>
```

Elements are keyed by `tag|text|href|name` for stable identity across calls.

## Architecture

### Viewport Filtering (Actor)
```javascript
// In #extractDOM(), before indexing an element:
if (viewportOnly && (rect.bottom < 0 || rect.top > viewportH)) {
  // Skip this element but still recurse children
}
```

### Compact Format (MCP-only)
The `browser_get_elements_compact` tool calls the same `get_dom` browser command but formats the result as a single-line-per-element compact view. No browser-side changes needed.

### Incremental Diffing (Actor)
The actor stores the previous DOM snapshot (`#previousDOM`). When `incremental=true`, it compares the new elements against the previous set and returns counts and lists of added/removed elements. The previous snapshot is always updated (even without incremental flag).
