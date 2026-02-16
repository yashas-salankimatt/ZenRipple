# Phase 7: Session Save/Restore

## Overview

Serialize and restore browser sessions (open tabs + cookies) to/from JSON files using `IOUtils`. Enables session persistence across browser restarts.

## MCP Tools

### `browser_session_save(file_path)`
Save the current session to a JSON file. Captures:
- All tab URLs and titles in the agent workspace
- Cookies for all tab domains (using proper TCP origin attributes)

### `browser_session_restore(file_path)`
Restore a session from a JSON file. Recreates:
- Tabs (opened in the agent workspace)
- Cookies (via `Services.cookies.add()`)

## JSON Format

```json
{
  "tabs": [
    {"url": "https://example.com", "title": "Example Domain"},
    {"url": "https://github.com", "title": "GitHub"}
  ],
  "cookies": [
    {
      "host": ".example.com",
      "name": "session",
      "value": "abc123",
      "path": "/",
      "secure": true,
      "httpOnly": true,
      "sameSite": 1,
      "expiry": 1735689600
    }
  ],
  "saved_at": "2025-01-01T00:00:00.000Z"
}
```

## Architecture

### Save

```
session_save → collect workspace tabs → extract URLs
  → for each domain: getCookiesFromHost(host, tab.originAttrs)
  → JSON.stringify → IOUtils.write(path, encoder.encode(json))
```

### Restore

```
session_restore → IOUtils.read(path) → JSON.parse
  → for each cookie: Services.cookies.add(...)
  → for each tab: gBrowser.addTab(url) + move to workspace
```

## Limitations

- **Cookie restore uses Services.cookies.add()** — restored cookies may not survive TCP partitioning. For cross-session cookie persistence, the cookies set this way act as "first-party" unpartitioned cookies.
- **No localStorage/sessionStorage persistence** — only cookies are saved. Storage is tied to the content process and cannot be easily serialized from chrome.
- **No scroll position or form data** — only URL and title are preserved per tab.
- **about:blank tabs are skipped** during restore.
