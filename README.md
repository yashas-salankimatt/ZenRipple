# ZenRipple

Give your AI agent full control of [Zen Browser](https://zen-browser.app/). Navigate pages, click elements, fill forms, take screenshots, read page content, execute JavaScript, monitor network traffic, and more — 80+ browser automation tools exposed via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Table of Contents

- [Quick Start](#quick-start-recommended)
- [Prerequisites](#prerequisites)
- [Manual Install](#manual-install)
- [Architecture](#architecture)
- [Available MCP Tools](#available-mcp-tools-80)
- [Session Model](#session-model)
- [MCPorter CLI Usage](#mcporter-cli-usage)
- [Install Script Options](#install-script-options)
- [Running Tests](#running-tests)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [License](#license)

## Quick Start (Recommended)

Tell your AI agent (Claude Code, Codex, etc.) to load the ZenRipple skill:

```
Load the skill from https://raw.githubusercontent.com/yashas-salankimatt/ZenRipple/main/SKILL.md and set it up.
```

The skill's Preflight section will automatically clone the repo, install dependencies, configure the MCP server, install the browser agent, and verify connectivity. The agent handles everything — you just need to restart Zen Browser if prompted.

## Prerequisites

- [Zen Browser](https://zen-browser.app/) (Firefox-based)
- [fx-autoconfig](https://github.com/MrOtherGuy/fx-autoconfig) installed in the target profile
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Node.js / npm (for MCPorter CLI)
- `ffmpeg` in PATH (optional, for session replay video export)

## Manual Install

If you prefer to set things up yourself instead of using the skill:

```bash
git clone https://github.com/yashas-salankimatt/ZenRipple.git
cd ZenRipple

# Install browser agent into your Zen profile
./install.sh

# Set up Python dependencies
cd mcp && uv sync && cd ..
```

Then add to your Claude Code project's `.mcp.json`:

```json
{
  "mcpServers": {
    "zenripple-browser": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/ZenRipple/mcp", "python", "/path/to/ZenRipple/mcp/zenripple_mcp_server.py"]
    }
  }
}
```

Restart Zen Browser and start a new Claude Code session.

<details>
<summary>Manual file copy (without install script)</summary>

1. Copy `browser/zenripple_agent.uc.js` to `<profile>/chrome/JS/`
2. Copy `browser/actors/*.sys.mjs` to `<profile>/chrome/JS/actors/`
3. Clear startup cache and restart Zen Browser

Profile locations:
- **macOS**: `~/Library/Application Support/zen/Profiles/<name>/chrome/`
- **Linux**: `~/.zen/<name>/chrome/`

</details>

## Architecture

```
Claude Code / AI Agent
        |
    MCP Protocol (stdio)
        |
  Python MCP Server (mcp/zenripple_mcp_server.py)
        |
    WebSocket (localhost:9876)
        |
  Zen Browser Agent (browser/zenripple_agent.uc.js)
    |--- JSWindowActors (content process DOM access)
    |--- XPCOM APIs (screenshots, cookies, network, downloads)
    |--- Zen Browser APIs (tabs, workspaces)
```

## Available MCP Tools (80+)

<details>
<summary><strong>Navigation</strong> (9 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_create_tab` | Open a new tab (supports `persist` flag) |
| `browser_close_tab` | Close a tab |
| `browser_switch_tab` | Switch to a tab |
| `browser_list_tabs` | List all open tabs |
| `browser_navigate` | Navigate to a URL |
| `browser_go_back` | Go back in history |
| `browser_go_forward` | Go forward in history |
| `browser_reload` | Reload a tab |
| `browser_get_page_info` | Get tab URL, title, loading state |

</details>

<details>
<summary><strong>DOM & Content</strong> (7 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_get_dom` | Get interactive elements with indices |
| `browser_get_elements_compact` | Token-efficient element list |
| `browser_get_page_text` | Get full page text |
| `browser_get_page_html` | Get page HTML source |
| `browser_get_accessibility_tree` | Get accessibility tree |
| `browser_list_frames` | List iframes |
| `browser_find_element_by_description` | Fuzzy-match elements by description |

</details>

<details>
<summary><strong>Interaction</strong> (13 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_click` | Click an element by index |
| `browser_click_coordinates` | Click at x,y coordinates |
| `browser_fill` | Fill a form field |
| `browser_select_option` | Select a dropdown option |
| `browser_type` | Type text character-by-character |
| `browser_press_key` | Press a keyboard key |
| `browser_scroll` | Scroll the page |
| `browser_scroll_at_point` | Scroll at specific x,y coordinates |
| `browser_hover` | Hover over an element by index |
| `browser_hover_coordinates` | Hover at x,y coordinates |
| `browser_drag` | Drag element to element |
| `browser_drag_coordinates` | Drag between coordinates |
| `browser_file_upload` | Upload a file to an input |

</details>

<details>
<summary><strong>Visual Grounding (VLM)</strong> (3 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_grounded_click` | Click on a UI element described in natural language (uses vision model) |
| `browser_grounded_hover` | Hover over an element described in natural language (uses vision model) |
| `browser_grounded_scroll` | Scroll at a point described in natural language (uses vision model) |

</details>

<details>
<summary><strong>Screenshots & Visual</strong> (3 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_screenshot` | Take a screenshot (returns image) |
| `browser_save_screenshot` | Save screenshot to file |
| `browser_reflect` | Screenshot + page text + metadata |

</details>

<details>
<summary><strong>Console & JavaScript</strong> (6 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_console_setup` | Start capturing console output |
| `browser_console_logs` | Get captured console messages |
| `browser_console_errors` | Get captured errors |
| `browser_console_teardown` | Stop capturing and clean up listeners |
| `browser_console_eval` | Execute JavaScript in page context |
| `browser_eval_chrome` | Execute JavaScript in chrome context |

</details>

<details>
<summary><strong>Cookies & Storage</strong> (6 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_get_cookies` | Get cookies for a domain |
| `browser_set_cookie` | Set a cookie |
| `browser_delete_cookies` | Delete cookies |
| `browser_get_storage` | Get localStorage/sessionStorage |
| `browser_set_storage` | Set storage key-value |
| `browser_delete_storage` | Delete storage keys |

</details>

<details>
<summary><strong>Network</strong> (6 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_network_monitor_start` | Start capturing network requests |
| `browser_network_monitor_stop` | Stop capturing |
| `browser_network_get_log` | Get captured network log |
| `browser_intercept_add_rule` | Add request interception rule |
| `browser_intercept_remove_rule` | Remove interception rule |
| `browser_intercept_list_rules` | List active rules |

</details>

<details>
<summary><strong>Waiting</strong> (5 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_wait` | Wait N seconds |
| `browser_wait_for_element` | Wait for CSS selector to appear |
| `browser_wait_for_text` | Wait for text to appear |
| `browser_wait_for_load` | Wait for page to finish loading |
| `browser_wait_for_download` | Wait for a download to complete |

</details>

<details>
<summary><strong>Tab Claiming & Persistence</strong> (4 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_list_workspace_tabs` | List ALL tabs in workspace (owned, unclaimed, stale) |
| `browser_claim_tab` | Claim an unclaimed/stale tab into your session |
| `browser_create_tab` | Open a new tab (`persist=true` to survive session close) |
| `browser_batch_navigate` | Open multiple URLs at once (`persist=true` to survive session close) |

</details>

<details>
<summary><strong>Sessions</strong> (8 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_session_info` | Get current session info (includes `claimed_tab_count`) |
| `browser_session_save` | Save session to file |
| `browser_session_restore` | Restore saved session |
| `browser_session_close` | Close session; close created tabs, release claimed/persist tabs |
| `browser_list_sessions` | List active sessions |
| `browser_set_session_name` | Set a human-readable name for the current session |
| `browser_compare_tabs` | Compare content across tabs |
| `browser_ping` | Health check with version mismatch detection |

</details>

<details>
<summary><strong>Clipboard</strong> (2 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_clipboard_read` | Read clipboard |
| `browser_clipboard_write` | Write to clipboard |

</details>

<details>
<summary><strong>Dialogs & Popups</strong> (4 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_get_dialogs` | Get pending alert/confirm/prompt dialogs |
| `browser_handle_dialog` | Accept or dismiss a dialog |
| `browser_get_popup_blocked_events` | Get popup-blocked events |
| `browser_allow_blocked_popup` | Allow a blocked popup to open |

</details>

<details>
<summary><strong>Events</strong> (2 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_get_tab_events` | Get tab open/close events |
| `browser_get_navigation_status` | Get HTTP status for last navigation |

</details>

<details>
<summary><strong>Action Recording</strong> (4 tools)</summary>

| Tool | Description |
|------|-------------|
| `browser_record_start` | Start recording user actions |
| `browser_record_stop` | Stop recording |
| `browser_record_save` | Save recording to file |
| `browser_record_replay` | Replay a recording |

</details>

<details>
<summary><strong>Session Replay (Video)</strong> (1 tool)</summary>

Session replay is always-on when `ZENRIPPLE_SESSION_ID` is set. Screenshots are captured automatically for every tool call and stored in `$TMPDIR/zenripple_replay_{session_id}/`. Opt out with `ZENRIPPLE_NO_REPLAY=1`.

| Tool | Description |
|------|-------------|
| `browser_replay_status` | Check replay state, frame count, and directory info |

</details>

## Session Model

The agent supports multiple concurrent AI sessions:

- Each session gets its own set of tabs in a dedicated "ZenRipple" workspace
- Multiple connections can share a session (parallel sub-agents)
- Sessions are identified by UUID and preserved across reconnections
- Stale sessions are automatically cleaned up after 30 minutes of inactivity

Set `ZENRIPPLE_SESSION_ID` environment variable to pin to a specific session across MCP server restarts.

## MCPorter CLI Usage

Use [MCPorter](https://github.com/steipete/mcporter) as a CLI layer on top of the MCP server:

```bash
# List tools exposed by this MCP server
npx -y mcporter list --stdio "uv run --project ./mcp python ./mcp/zenripple_mcp_server.py"

# Call a tool from CLI
npx -y mcporter call zenripple.browser_create_tab --args '{"url":"https://example.com"}' --output json
```

### Parallel Agent Isolation

MCPorter itself does not assign browser sessions for you. To keep each top-level agent isolated, give each agent process its own `ZENRIPPLE_SESSION_ID`.

```bash
# In each top-level Claude/Codex terminal, once per agent run:
export ZENRIPPLE_SESSION_ID="$(uv run --project ./mcp python ./mcp/zenripple_session.py new)"
```

Then use normal MCPorter commands. Every call from that process (and its child sub-agents) will be scoped to the same browser session:

```bash
npx -y mcporter call zenripple.browser_create_tab --args '{"url":"https://www.wikipedia.org"}' --output json
npx -y mcporter call zenripple.browser_list_tabs --output json
```

Notes:
- Different top-level agent instances should use different `ZENRIPPLE_SESSION_ID` values.
- Parent + sub-agents should share the same `ZENRIPPLE_SESSION_ID` to collaborate in one tab/session scope.
- `browser_session_close` destroys that session; after closing, mint a new ID before more calls.

Optional helper test for isolation:

```bash
./scripts/test_mcporter_parallel_sessions.sh
```

## Install Script Options

```bash
./install.sh                          # Interactive install
./install.sh --profile 1 --yes        # Non-interactive, first profile
./install.sh --uninstall --profile 1  # Uninstall from first profile
./install.sh --list                   # Show installation status
```

## Running Tests

```bash
# Unit tests (342 tests)
PYTHONPATH=./mcp uv run --project ./mcp pytest tests/test_zenripple_mcp.py -v

# Benchmarks (requires running browser + Claude Agent SDK)
cd bench && uv run python -m bench run --suite smoke
```

## Troubleshooting

**Agent not starting**: Check the browser console (Ctrl+Shift+J) for `[ZenRipple]` messages. Verify fx-autoconfig is installed.

**Port 9876 already in use**: Another instance may be running. Close all Zen Browser windows and try again.

**MCP server can't connect**: Ensure Zen Browser is running with the agent loaded. Check that nothing is blocking localhost:9876.

**Actor registration failed**: Verify the actor `.sys.mjs` files are in `<profile>/chrome/JS/actors/`. The `resource://` URI scheme requires this exact location.

## Uninstall

```bash
./install.sh --uninstall
```

Or manually remove:
- `<profile>/chrome/JS/zenripple_agent.uc.js`
- `<profile>/chrome/JS/actors/ZenRippleAgentChild.sys.mjs`
- `<profile>/chrome/JS/actors/ZenRippleAgentParent.sys.mjs`

## License

[MIT](LICENSE)
