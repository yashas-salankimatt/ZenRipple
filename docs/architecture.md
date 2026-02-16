# Zen AI Agent — Architecture

## System Overview

```
+-------------------------------------------------------+
|  Claude Code / AI Agent (client)                      |
|  - Sends MCP tool calls (e.g. browser_navigate)      |
+------------------------+------------------------------+
                         | stdio (MCP protocol)
                         v
+-------------------------------------------------------+
|  Python MCP Server (zenleap_mcp_server.py)            |
|  - FastMCP framework                                  |
|  - 55+ tool definitions                               |
|  - WebSocket client to browser                        |
|  - Session ID tracking + reconnection logic           |
+------------------------+------------------------------+
                         | WebSocket (localhost:9876)
                         | JSON-RPC: {id, method, params}
                         v
+-------------------------------------------------------+
|  Browser Extension (zenleap_agent.uc.js)              |
|  - RFC 6455 WebSocket server on port 9876             |
|  - Multi-session model with tab ownership             |
|  - 50+ command handlers                               |
|  - XPCOM: sockets, cookies, downloads, prefs          |
|  - Loaded by fx-autoconfig with full chrome privilege  |
+------------------------+------------------------------+
                         | sendQuery() / receiveMessage()
                         v
+-------------------------------------------------------+
|  JSWindowActors (content process)                     |
|  - ZenLeapAgentChild.sys.mjs (1,192 lines)           |
|    DOM extraction, interaction, console capture,      |
|    file upload, keyboard input (nsITextInputProcessor)|
|  - ZenLeapAgentParent.sys.mjs (minimal relay)        |
+-------------------------------------------------------+
```

## Component Details

### 1. MCP Server (`mcp/zenleap_mcp_server.py`)

**Role**: Bridge between MCP clients (Claude Code) and the browser.

- Built on `FastMCP` — handles MCP protocol negotiation, tool registration, stdio transport
- Maintains a persistent WebSocket connection to the browser
- Session-aware: preserves `session_id` across reconnections
- Retries once on connection errors (not on browser-level errors like "Tab not found")
- Keepalive tuning: `ping_interval=30, ping_timeout=120` (avoids false disconnects during heavy rendering)

**Key functions**:
- `get_ws()` — connect/reconnect to the browser WebSocket server
- `browser_command(method, params)` — send JSON-RPC command and await response
- `text_result(data)` / `image_result(data)` — format results for MCP

### 2. Browser Extension (`browser/zenleap_agent.uc.js`)

**Role**: WebSocket server + command dispatcher inside Zen Browser.

Loaded by fx-autoconfig as a `.uc.js` userscript in the browser's chrome context (full XPCOM privilege).

**Key subsystems**:

- **WebSocket Server**: RFC 6455 compliant, built on `nsIServerSocket` + `nsIInputStreamPump`. Handles handshake, frame parsing/construction, binary chunking (8192 bytes), 64-bit frame lengths.

- **Session Model**: Multiple concurrent sessions, each with:
  - `agentTabs` (Set) — tabs owned by this session
  - `tabEvents` (array, max 200) — tab open/close event log
  - `connections` (Map) — WebSocket connections in this session
  - `recording` state — for action replay
  - Grace timer (5 min) + stale sweep (30 min inactivity)

- **Command Handlers**: 50+ async handlers in `commandHandlers` object. Each receives `(params, ctx)` where `ctx = {session, connection, resolveTab}`.

- **Tab Resolution**: `resolveTabScoped(tabId, sessionId)` filters by `data-agent-session-id` attribute. Traverses all workspaces via `gZenWorkspaces.allStoredTabs`. No fallback to `gBrowser.selectedTab` (prevents hijacking user tabs).

- **Workspace**: Ensures a dedicated "Zen AI Agent" workspace exists. All session tabs are moved there.

### 3. JSWindowActors

**Role**: Cross-process content access (required under Fission process isolation).

- **ZenLeapAgentChild** (content process): DOM indexing with self-healing selectors, trusted keyboard input via `nsITextInputProcessor`, form interaction, console capture via `Cu.exportFunction`, screenshot support, file upload via DataTransfer, accessibility tree traversal
- **ZenLeapAgentParent** (chrome process): Minimal relay for `sendQuery`/`receiveMessage`

Registered via `resource://` URI scheme (not `file://` — Firefox doesn't trust it for actor modules).

## Protocol

### WebSocket Handshake

```
GET /new HTTP/1.1          → Create new session
GET /session/<uuid>        → Join existing session
GET /                      → Create new (backward compat)
```

### JSON-RPC Messages

Request:
```json
{"id": 1, "method": "navigate", "params": {"url": "https://example.com"}}
```

Response:
```json
{"id": 1, "result": {"url": "https://example.com/", "title": "Example"}}
```

Error:
```json
{"id": 1, "error": "Tab not found: abc123"}
```

## Security Model

- **Localhost only**: WebSocket server binds to `127.0.0.1:9876`
- **No authentication**: Any localhost process can connect (acceptable for local dev)
- **Content isolation**: JSWindowActors enforce same-origin via `contentWindow.eval()` at content principal
- **Chrome eval**: `browser_eval_chrome` runs with system principal — powerful but localhost-only

## Key Technical Decisions

| Decision | Rationale |
|----------|-----------|
| XPCOM sockets over WebSocket API | Full control over binary framing, no content-security restrictions |
| `nsITextInputProcessor` for keyboard | Produces `isTrusted:true` events; works in Google Sheets, contenteditable |
| `nsIBinaryInputStream` over `nsIScriptableInputStream` | Avoids silent truncation at 0x00 bytes |
| `resource://` for actor URIs | `file://` silently fails actor registration in Firefox |
| `globalThis[key]` for singletons | fx-autoconfig loads per-window; prevents duplicate servers |
| 8192-byte binary chunking | Prevents stack overflow on large payloads (>64KB) |
| Session tab ownership via DOM attributes | Survives workspace switches, cross-workspace tab resolution |
