# Phase 7: Cookie Management & Web Storage

## Overview

Cookie management and localStorage/sessionStorage access for browser automation. Cookies are set via `document.cookie` through the content actor to correctly handle Total Cookie Protection (TCP) partitioning.

## MCP Tools

### `browser_get_cookies(url?, name?, tab_id?)`
Get cookies for the current tab's domain or a specific URL. Optionally filter by name.

### `browser_set_cookie(name, value?, path?, secure?, httpOnly?, sameSite?, expires?, tab_id?, frame_id?)`
Set a cookie on the current page. The tab must be navigated to the target domain first. Uses `document.cookie` via the content actor.

### `browser_delete_cookies(url?, name?, tab_id?)`
Delete cookies for the current tab's domain. If name provided, deletes only that cookie.

### `browser_get_storage(storage_type, key?, tab_id?, frame_id?)`
Get localStorage or sessionStorage data. Omit key to dump all entries.

### `browser_set_storage(storage_type, key, value, tab_id?, frame_id?)`
Set a key-value pair in localStorage or sessionStorage.

### `browser_delete_storage(storage_type, key?, tab_id?, frame_id?)`
Delete a key or clear all storage. Omit key to clear everything.

## Architecture

### Cookie Pipeline

```
MCP tool → browser_command("set_cookie") → chrome handler
  → builds cookie string (name=value; path; Secure; etc.)
  → actor.sendQuery("ZenLeapAgent:SetCookie", {cookie: str})
  → ZenLeapAgentChild.#setCookie(str) → document.cookie = str
```

### Why document.cookie Instead of Services.cookies.add()

Zen Browser uses **Total Cookie Protection** (cookie behavior 5 = `BEHAVIOR_REJECT_TRACKER_AND_PARTITION_FOREIGN`). Under TCP:

- `Services.cookies.add()` does NOT go through the content pipeline and cookies are **silently rejected** — the API returns success but `cookieExists()` returns false
- `document.cookie` from the content actor works because it goes through the browser's full cookie pipeline including TCP partitioning
- `getCookiesFromHost()` requires the tab's `contentPrincipal.originAttributes` to match the correct TCP partition — passing empty `{}` returns nothing

### Storage Access

Storage methods run in the content process via the JSWindowActor:

```
MCP tool → browser_command("get_storage") → chrome handler
  → actor.sendQuery("ZenLeapAgent:GetStorage")
  → ZenLeapAgentChild.#getStorage() → contentWindow.localStorage/sessionStorage
```

## Key Gotchas

- **CSP can block document.cookie** — sites like httpbin.org have Content-Security-Policy headers that prevent cookie manipulation
- **Cookie expiry edge case** — `cookie.expiry * 1000` can produce invalid dates; wrapped in try/catch with 'session' fallback
- **Origin attributes** — All cookie queries (get, delete, session_save) use `tab.linkedBrowser.contentPrincipal.originAttributes` for TCP compatibility
