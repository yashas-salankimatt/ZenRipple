# Phase 9: Multi-Tab Coordination

## Overview

Multi-tab coordination provides higher-level commands for working with multiple browser tabs simultaneously. Instead of switching between tabs one at a time, agents can open multiple URLs at once and compare content across tabs in a single call.

## Commands

### `batch_navigate`

Opens multiple URLs in new tabs simultaneously.

**MCP Tool**: `browser_batch_navigate(urls: str)`

- `urls`: comma-separated list of URLs to open
- All tabs are created in the ZenLeap AI workspace
- Returns tab IDs for all opened tabs

**Example**:
```
browser_batch_navigate("https://example.com, https://iana.org, https://mozilla.org")
→ {success: true, tabs: [{tab_id: "...", url: "..."}, ...]}
```

### `compare_tabs`

Gets a content summary from multiple tabs for side-by-side comparison.

**MCP Tool**: `browser_compare_tabs(tab_ids: str)`

- `tab_ids`: comma-separated tab IDs (at least 2)
- Returns URL, title, and first 500 characters of text for each tab
- Useful for comparing search results, A/B testing, or verifying data across pages

**Example**:
```
browser_compare_tabs("panel-1-1, panel-1-2")
→ [{tab_id: "panel-1-1", url: "...", title: "...", text_preview: "..."}, ...]
```

## Architecture

Both commands are implemented in `zenleap_agent.uc.js` as command handlers:

- `batch_navigate`: Iterates through URLs, creates tabs via `gBrowser.addTab()`, moves to agent workspace
- `compare_tabs`: Iterates through tab IDs, gets page text via actor's `GetPageText` message

The MCP server (`zenleap_mcp_server.py`) accepts comma-separated strings and splits them into arrays before sending to the browser.

## Use Cases

1. **Research**: Open multiple search results simultaneously, then compare content
2. **A/B Testing**: Load two versions of a page and compare their content
3. **Data Gathering**: Open multiple product/listing pages and extract data from each
4. **Verification**: Compare a page before and after an action across different tabs
