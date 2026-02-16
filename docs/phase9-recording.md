# Phase 9: Action Recording & Replay

## Overview

Action recording captures browser commands as they execute, allowing workflows to be saved to disk and replayed later. This enables building reusable automation scripts, regression testing, and debugging agent behavior.

## Commands

### `record_start`

Begins recording. All subsequent browser commands (navigation, clicks, typing, etc.) are logged with their parameters and timestamps.

**MCP Tool**: `browser_record_start()`

### `record_stop`

Stops recording and returns the number of actions captured.

**MCP Tool**: `browser_record_stop()` → `{success: true, actions: N}`

### `record_save`

Saves the recorded actions to a JSON file on disk.

**MCP Tool**: `browser_record_save(file_path: str)`

The saved file format:
```json
{
  "actions": [
    {"method": "navigate", "params": {"url": "..."}, "timestamp": "..."},
    {"method": "click_element", "params": {"index": 3}, "timestamp": "..."}
  ],
  "recorded_at": "2025-...",
  "count": 2
}
```

### `record_replay`

Replays a saved recording, executing each action in sequence with a configurable delay.

**MCP Tool**: `browser_record_replay(file_path: str, delay: float = 0.5)`

- `delay`: seconds between each action (default 0.5)
- Returns count of successfully replayed actions and any errors

## Excluded Commands

Meta and read-only commands are excluded from recording to avoid noise:

- `ping`, `get_agent_logs`
- `record_start`, `record_stop`, `record_save`, `record_replay`
- `get_tab_events`, `get_dialogs`
- `list_tabs`, `get_page_info`, `get_navigation_status`
- `network_get_log`, `intercept_list_rules`

## Architecture

Recording happens in `#handleCommand()` in `zenleap_agent.uc.js`:

```javascript
// After successful handler execution:
if (recordingActive && !RECORDING_EXCLUDE.has(msg.method)) {
  recordedActions.push({
    method: msg.method,
    params: msg.params || {},
    timestamp: new Date().toISOString(),
  });
}
```

File I/O uses Gecko's `IOUtils.write/read` (same async API used for session persistence).

Replay iterates through actions and calls the corresponding command handlers directly, catching errors per-action so one failure doesn't abort the entire replay.

## Limitations

- Tab IDs in recorded actions may not match on replay (tabs are ephemeral). Recordings work best for workflows that create their own tabs.
- No conditional logic or branching — recordings are linear sequences.
- Recordings don't capture page state or assertions — they're action-only.
