# Zen AI Agent

Browser automation server for [Zen Browser](https://zen-browser.app/) that enables Claude Code (and other AI agents) to control the browser via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

Runs a WebSocket server inside Zen Browser with 55+ commands for navigation, DOM interaction, screenshots, console access, cookie/storage management, network monitoring, and more.

## Architecture

```
Claude Code / AI Agent
        |
    MCP Protocol (stdio)
        |
  Python MCP Server (mcp/zenleap_mcp_server.py)
        |
    WebSocket (localhost:9876)
        |
  Zen Browser Extension (browser/zenleap_agent.uc.js)
    |--- JSWindowActors (content process DOM access)
    |--- XPCOM APIs (screenshots, cookies, network, downloads)
    |--- Zen Browser APIs (tabs, workspaces)
```

## Prerequisites

- [Zen Browser](https://zen-browser.app/) (Firefox-based)
- [fx-autoconfig](https://github.com/MrOtherGuy/fx-autoconfig) installed in the target profile
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (Python package manager)

## Quick Install

```bash
git clone <this-repo> zen-ai-agent
cd zen-ai-agent

# Install browser extension to your Zen profile
./install.sh

# Set up the MCP server
cd mcp && uv sync && cd ..
```

Then add to your Claude Code project's `.mcp.json`:

```json
{
  "mcpServers": {
    "zenleap-browser": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/zen-ai-agent/mcp", "python", "/path/to/zen-ai-agent/mcp/zenleap_mcp_server.py"]
    }
  }
}
```

Restart Zen Browser and start a new Claude Code session.

## Install Script Options

```bash
./install.sh                          # Interactive install
./install.sh --profile 1 --yes        # Non-interactive, first profile
./install.sh --uninstall --profile 1  # Uninstall from first profile
./install.sh --list                   # Show installation status
```

## Available MCP Tools (55+)

### Navigation
| Tool | Description |
|------|-------------|
| `browser_create_tab` | Open a new tab |
| `browser_close_tab` | Close a tab |
| `browser_switch_tab` | Switch to a tab |
| `browser_list_tabs` | List all open tabs |
| `browser_navigate` | Navigate to a URL |
| `browser_go_back` | Go back in history |
| `browser_go_forward` | Go forward in history |
| `browser_reload` | Reload a tab |
| `browser_get_page_info` | Get tab URL, title, loading state |

### DOM & Content
| Tool | Description |
|------|-------------|
| `browser_get_dom` | Get interactive elements with indices |
| `browser_get_elements_compact` | Token-efficient element list |
| `browser_get_page_text` | Get full page text |
| `browser_get_page_html` | Get page HTML source |
| `browser_get_accessibility_tree` | Get accessibility tree |
| `browser_list_frames` | List iframes |
| `browser_find_element_by_description` | Fuzzy-match elements by description |

### Interaction
| Tool | Description |
|------|-------------|
| `browser_click` | Click an element by index |
| `browser_click_coordinates` | Click at x,y coordinates |
| `browser_fill` | Fill a form field |
| `browser_select_option` | Select a dropdown option |
| `browser_type` | Type text character-by-character |
| `browser_press_key` | Press a keyboard key |
| `browser_scroll` | Scroll the page |
| `browser_hover` | Hover over an element |
| `browser_drag` | Drag element to element |
| `browser_drag_coordinates` | Drag between coordinates |
| `browser_file_upload` | Upload a file to an input |

### Screenshots & Visual
| Tool | Description |
|------|-------------|
| `browser_screenshot` | Take a screenshot (returns image) |
| `browser_save_screenshot` | Save screenshot to file |
| `browser_reflect` | Screenshot + page text + metadata |

### Console & JavaScript
| Tool | Description |
|------|-------------|
| `browser_console_setup` | Start capturing console output |
| `browser_console_logs` | Get captured console messages |
| `browser_console_errors` | Get captured errors |
| `browser_console_eval` | Execute JavaScript in page context |
| `browser_eval_chrome` | Execute JavaScript in chrome context |

### Cookies & Storage
| Tool | Description |
|------|-------------|
| `browser_get_cookies` | Get cookies for a domain |
| `browser_set_cookie` | Set a cookie |
| `browser_delete_cookies` | Delete cookies |
| `browser_get_storage` | Get localStorage/sessionStorage |
| `browser_set_storage` | Set storage key-value |
| `browser_delete_storage` | Delete storage keys |

### Network
| Tool | Description |
|------|-------------|
| `browser_network_monitor_start` | Start capturing network requests |
| `browser_network_monitor_stop` | Stop capturing |
| `browser_network_get_log` | Get captured network log |
| `browser_intercept_add_rule` | Add request interception rule |
| `browser_intercept_remove_rule` | Remove interception rule |
| `browser_intercept_list_rules` | List active rules |

### Waiting
| Tool | Description |
|------|-------------|
| `browser_wait` | Wait N seconds |
| `browser_wait_for_element` | Wait for CSS selector to appear |
| `browser_wait_for_text` | Wait for text to appear |
| `browser_wait_for_load` | Wait for page to finish loading |
| `browser_wait_for_download` | Wait for a download to complete |

### Multi-Tab & Sessions
| Tool | Description |
|------|-------------|
| `browser_compare_tabs` | Compare content across tabs |
| `browser_batch_navigate` | Open multiple URLs at once |
| `browser_session_save` | Save session to file |
| `browser_session_restore` | Restore saved session |
| `browser_session_info` | Get current session info |
| `browser_session_close` | Close session and all tabs |
| `browser_list_sessions` | List active sessions |

### Clipboard
| Tool | Description |
|------|-------------|
| `browser_clipboard_read` | Read clipboard |
| `browser_clipboard_write` | Write to clipboard |

### Dialogs & Events
| Tool | Description |
|------|-------------|
| `browser_get_dialogs` | Get pending alert/confirm/prompt dialogs |
| `browser_handle_dialog` | Accept or dismiss a dialog |
| `browser_get_tab_events` | Get tab open/close events |
| `browser_get_navigation_status` | Get HTTP status for last navigation |

### Recording
| Tool | Description |
|------|-------------|
| `browser_record_start` | Start recording actions |
| `browser_record_stop` | Stop recording |
| `browser_record_save` | Save recording to file |
| `browser_record_replay` | Replay a recording |

## Session Model

The agent supports multiple concurrent AI sessions:

- Each session gets its own set of tabs in a dedicated "Zen AI Agent" workspace
- Multiple connections can share a session (parallel sub-agents)
- Sessions are identified by UUID and preserved across reconnections
- Stale sessions are automatically cleaned up after 30 minutes of inactivity

Set `ZENLEAP_SESSION_ID` environment variable to pin to a specific session across MCP server restarts.

## Manual Installation

If you prefer not to use the install script:

1. Copy `browser/zenleap_agent.uc.js` to `<profile>/chrome/JS/`
2. Copy `browser/actors/*.sys.mjs` to `<profile>/chrome/JS/actors/`
3. Clear startup cache and restart Zen Browser

Profile locations:
- **macOS**: `~/Library/Application Support/zen/Profiles/<name>/chrome/`
- **Linux**: `~/.zen/<name>/chrome/`

## Running Tests

```bash
# Unit tests (173+ tests)
cd mcp && uv run pytest ../tests/test_zenleap_mcp.py -v

# Benchmarks (requires running browser + Claude Agent SDK)
cd bench && uv run python -m bench run --suite smoke
```

## Troubleshooting

**Agent not starting**: Check the browser console (Ctrl+Shift+J) for `[Zen AI Agent]` messages. Verify fx-autoconfig is installed.

**Port 9876 already in use**: Another instance may be running. Close all Zen Browser windows and try again.

**MCP server can't connect**: Ensure Zen Browser is running with the agent loaded. Check that nothing is blocking localhost:9876.

**Actor registration failed**: Verify the actor `.sys.mjs` files are in `<profile>/chrome/JS/actors/`. The `resource://` URI scheme requires this exact location.

## Uninstall

```bash
./install.sh --uninstall
```

Or manually remove:
- `<profile>/chrome/JS/zenleap_agent.uc.js`
- `<profile>/chrome/JS/actors/ZenLeapAgentChild.sys.mjs`
- `<profile>/chrome/JS/actors/ZenLeapAgentParent.sys.mjs`

## License

MIT
