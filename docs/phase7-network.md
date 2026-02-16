# Phase 7: Network Monitoring & Request Interception

## Overview

HTTP request/response monitoring and interception via `nsIObserverService`. Logs network traffic to a circular buffer and supports URL-pattern-based blocking or header modification.

## MCP Tools

### `browser_network_monitor_start()`
Start recording HTTP requests/responses into a 500-entry circular buffer.

### `browser_network_monitor_stop()`
Stop recording. The log buffer is preserved for querying.

### `browser_network_get_log(url_filter?, method_filter?, status_filter?, limit?)`
Get captured entries. All filters are optional:
- `url_filter`: regex matched against URLs
- `method_filter`: GET, POST, etc.
- `status_filter`: HTTP status code (e.g. 404)
- `limit`: max entries (default 50)

### `browser_intercept_add_rule(pattern, action, headers?)`
Add an interception rule:
- `action: 'block'` — cancels the request with `NS_ERROR_ABORT`
- `action: 'modify_headers'` — sets request headers before sending
- `headers`: JSON string of header key-value pairs (for modify_headers)

### `browser_intercept_remove_rule(rule_id)`
Remove a rule by its ID.

### `browser_intercept_list_rules()`
List all active rules with their patterns and actions.

## Architecture

A single `nsIObserverService` observer handles both monitoring and interception:

```
Services.obs.addObserver(networkObserver, 'http-on-modify-request')
Services.obs.addObserver(networkObserver, 'http-on-examine-response')
```

The observer is registered lazily (on first `network_monitor_start` or `intercept_add_rule` call) and stays active for the session.

### Intercept Rules

Rules are matched in order against each request URL. First matching rule wins:

```javascript
for (const rule of interceptRules) {
  if (rule.pattern.test(url)) {
    if (rule.action === 'block') {
      channel.cancel(Cr.NS_ERROR_ABORT);
      return;
    }
    if (rule.action === 'modify_headers') {
      channel.setRequestHeader(name, value, false);
    }
  }
}
```

### Network Log Format

Each entry contains:
- `url`, `method`, `type` ('request' or 'response')
- `status` (HTTP status code, response only)
- `content_type` (from Content-Type header, response only)
- `timestamp` (ISO 8601)

## Limitations

- **Browser-global scope** — monitors ALL network traffic, not just agent tabs. URL filtering is recommended.
- **No request body capture** — only URL, method, status, and content type are logged.
- **No response body capture** — only headers are accessible via `nsIHttpChannel`.
- **Intercept rules use regex** — complex URL matching may require careful escaping.
