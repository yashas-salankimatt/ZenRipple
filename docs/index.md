# ZenLeap AI — Feature Documentation Index

This directory contains detailed documentation for each feature implemented in the ZenLeap AI browser automation system.

## Architecture

- **Browser Extension**: `JS/zenleap_agent.uc.js` — WebSocket server + command handlers (chrome process)
- **Content Actor**: `JS/actors/ZenLeapAgentChild.sys.mjs` — DOM access + interaction (content process)
- **MCP Server**: `mcp/zenleap_mcp_server.py` — Claude Code bridge (Python)
- **Benchmarks**: `bench/` — Scenario runner + metrics

## Feature Index

### Phase 1-5: Foundation (Complete)
Core browser automation: tabs, navigation, screenshots, DOM extraction, clicking, typing, scrolling, console capture, JS eval, clipboard, workspace scoping, iframes, dialogs, wait commands.

### Phase 6: Core Reliability (Complete)
- Shadow DOM traversal
- iframe support (allFrames + frame_id targeting)
- `wait_for_element` / `wait_for_text`
- Navigation error detection (HTTP status tracking)
- Dialog handling (alert/confirm/prompt)
- New tab/popup detection

### Trusted Keyboard Input (Complete)
- [trusted-keyboard-input.md](trusted-keyboard-input.md) — nsITextInputProcessor for Google Sheets and canvas apps

### Phase 7: Data & Session
- [phase7-cookies-storage.md](phase7-cookies-storage.md) — Cookie management + localStorage/sessionStorage
- [phase7-network.md](phase7-network.md) — Network monitoring + request interception
- [phase7-session.md](phase7-session.md) — Session save/restore

### Phase 8: Token Efficiency
- [phase8-smart-dom.md](phase8-smart-dom.md) — Smart DOM filtering + compressed representation
- [phase8-accessibility.md](phase8-accessibility.md) — Accessibility tree extraction
- [phase8-incremental-dom.md](phase8-incremental-dom.md) — Incremental DOM diffing

### Phase 9: Advanced Intelligence
- [phase9-self-healing.md](phase9-self-healing.md) — Self-healing selectors
- [phase9-multi-tab.md](phase9-multi-tab.md) — Multi-tab coordination
- [phase9-recording.md](phase9-recording.md) — Action recording/replay
- [phase9-visual-grounding.md](phase9-visual-grounding.md) — Element find by description

## Test Coverage

| Phase | pytest | e2e scenarios | Status |
|-------|--------|---------------|--------|
| 1-5   | 82     | 29            | Complete |
| 6     | 82     | 4 scenarios   | Complete |
| TIP   | 82     | verified      | Complete |
| 7     | 104    | 16            | Complete |
| 8     | 118    | 18            | Complete |
| 9     | 131    | 19            | Complete |
