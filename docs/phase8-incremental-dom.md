# Phase 8: Incremental DOM Diffing

## Overview

Track changes between DOM snapshots to detect new/removed elements without re-processing the entire page. Useful for monitoring dynamic content (SPAs, real-time updates, AJAX-loaded content).

## Usage

```
# First call establishes baseline
browser_get_dom()

# Subsequent calls with incremental=true show changes
browser_get_dom(incremental=true)
# → Changes: +3 -1
# → Added: [5] <button>New</button>
# → Removed: <a>Old Link</a>
```

## How It Works

1. Each `get_dom` call stores a snapshot of element metadata (tag, text, href, name)
2. When `incremental=true`, the current elements are compared against the previous snapshot
3. Elements are keyed by `tag|text|href|name` for identity matching
4. The diff includes counts and up to 20 added/removed elements
5. The full element list is still returned alongside the diff

## Response Format

```json
{
  "elements": [...],
  "url": "https://example.com",
  "title": "Example",
  "total": 15,
  "incremental": true,
  "diff": {
    "added": 3,
    "removed": 1,
    "total": 15,
    "added_elements": [{"index": 12, "tag": "button", "text": "New"}],
    "removed_elements": [{"tag": "a", "text": "Old Link"}]
  }
}
```

## Limitations

- **Per-actor state**: The previous DOM snapshot is stored per-actor instance. If the page navigates (actor destroyed), the baseline is lost.
- **Key collisions**: Two elements with the same tag+text+href+name are considered identical. This can cause false negatives for duplicate elements.
- **Added/removed cap**: Only the first 20 added/removed elements are listed to limit response size.
