---
name: zen-ai-agent
description: Use this skill when an agent must install, configure, and operate Zen AI Agent through canonical MCP and MCPorter CLI, including multi-agent session isolation, sub-agent coordination, safe browser automation, and clean uninstall.
---

# Zen AI Agent Skill

This skill defines how to set up and operate `zen-ai-agent` — browser automation for Zen Browser via MCP and MCPorter CLI.

All agent tabs live in a dedicated "Zen AI Agent" workspace inside Zen Browser. Each agent session is isolated: your tabs, events, and state are scoped to your session ID.

## Zero-Touch Bootstrap

If loaded from a raw URL on a fresh machine, clone first:

```bash
REPO_URL="https://github.com/yashas-salankimatt/zen-ai-agent.git"
REPO_DIR="${HOME}/zen-ai-agent"

if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"
REPO="$(pwd)"
```

To persist locally (optional):

```bash
if [ -d "$HOME/.codex/skills" ]; then
  SKILL_DEST="$HOME/.codex/skills/zen-ai-agent"
elif [ -d "$HOME/.claude/skills" ]; then
  SKILL_DEST="$HOME/.claude/skills/zen-ai-agent"
else
  SKILL_DEST="$HOME/.codex/skills/zen-ai-agent"
fi

mkdir -p "$SKILL_DEST"
cp "$REPO/SKILL.md" "$SKILL_DEST/SKILL.md"
```

## Prerequisites

- macOS or Linux with Zen Browser installed and run at least once.
- `uv`, `python`, `node`, `npm`/`npx` available.
- Zen profile has `fx-autoconfig` (ZenLeap includes this).

## Fresh Setup

1. Enter repo and install dependencies:

```bash
cd "${HOME}/zen-ai-agent"
REPO="$(pwd)"
uv sync --project ./mcp
```

2. Install browser agent into Zen profiles:

```bash
# Single profile:
./install.sh --profile 1 --yes

# All profiles (non-interactive):
./install.sh --yes
```

3. Configure MCPorter globally:

```bash
npx -y mcporter config add zenleap \
  --stdio uv \
  --arg run \
  --arg --project \
  --arg "$REPO/mcp" \
  --arg python \
  --arg "$REPO/mcp/zenleap_mcp_server.py" \
  --scope home
```

4. Verify:

```bash
npx -y mcporter list --json
```

## Sessions

Session isolation is based on `ZENLEAP_SESSION_ID`. One top-level agent = one session. Sub-agents share the parent's session. Different agents must use different sessions.

```bash
# Create a session
export ZENLEAP_SESSION_ID="$(uv run --project "$REPO/mcp" python "$REPO/mcp/zenleap_session.py" new)"

# Pass to sub-agents if env isn't inherited
ZENLEAP_SESSION_ID="$ZENLEAP_SESSION_ID" <sub-agent-command>

# Close when done
npx -y mcporter call zenleap.browser_session_close --output json
```

Session management tools:
- `browser_session_info` — get current session ID, workspace, connection count, tab count.
- `browser_session_close` — close the session and release all tabs.
- `browser_list_sessions` — list all active sessions (admin/debug).
- `browser_session_save` / `browser_session_restore` — save/restore open tabs and cookies to a JSON file.

---

## How To Use Your Tools

All tools are prefixed `browser_`. Most accept an optional `tab_id` (defaults to active tab) and `frame_id` (defaults to 0, the top frame). Use `--output json` with MCPorter CLI.

### Opening & Navigating Pages

To visit a URL, create a tab and wait for it to load:

- `browser_create_tab(url)` — open a new tab with a URL (defaults to `about:blank`).
- `browser_navigate(url)` — navigate the active (or specified) tab to a URL.
- `browser_go_back` / `browser_go_forward` — history navigation.
- `browser_reload` — refresh the page.
- `browser_batch_navigate(urls)` — open multiple URLs at once (comma-separated). Returns tab IDs.
- `browser_wait_for_load(timeout)` — wait until the page finishes loading. **Always use this after navigation instead of fixed sleeps.**
- `browser_get_navigation_status` — check HTTP status code, error code, and loading state after navigation. Useful for detecting 404s or network failures.

### Understanding a Page

Before interacting, you need to see what's on the page:

- `browser_screenshot` — take a visual screenshot. Use this to see layouts, verify state, or understand visual content.
- `browser_reflect(goal)` — get a screenshot + page text + metadata in one call. Best for getting a full picture before making decisions. Pass an optional `goal` to focus the analysis.
- `browser_get_page_info` — get URL, title, loading state, and navigation history.
- `browser_get_page_text` — get all visible text on the page. Good for reading content.
- `browser_get_page_html` — get full HTML source. Use when you need raw markup.
- `browser_get_dom` — get all interactive elements (buttons, links, inputs, etc.) with index numbers, attributes, and bounding boxes. **This is how you find elements to click/fill.** Supports `viewport_only`, `max_elements`, and `incremental` (diff against last call) options.
- `browser_get_elements_compact` — same interactive elements but 5-10x fewer tokens. Returns `[index] text (tag)` per line. Use when you just need indices, not full details.
- `browser_get_accessibility_tree` — get the semantic accessibility tree (role, name, value, depth). Useful for understanding structure without visual rendering.
- `browser_find_element_by_description(description)` — fuzzy-find elements by natural language (e.g., "login button", "search input"). Returns top 5 candidates with indices.

### Interacting with Elements

The general pattern: call `browser_get_dom` (or `browser_get_elements_compact`) to get element indices, then use those indices to interact.

- `browser_click(index)` — click an element by its index.
- `browser_click_coordinates(x, y)` — click at exact pixel coordinates. Use with screenshot + DOM for precision.
- `browser_fill(index, value)` — clear a form field and set a new value. Dispatches input/change events.
- `browser_select_option(index, value)` — select a dropdown option by value or visible text.
- `browser_type(text)` — type character-by-character into the focused element. Click an element first to focus it.
- `browser_press_key(key)` — press a key (Enter, Tab, Escape, ArrowDown, etc.) with optional `ctrl`/`shift`/`alt`/`meta` modifiers.
- `browser_scroll(direction, amount)` — scroll up/down/left/right by pixel amount (default: 500px down).
- `browser_hover(index)` — hover over an element to reveal tooltips or dropdown menus.
- `browser_drag(source_index, target_index)` — drag one element to another.
- `browser_drag_coordinates(start_x, start_y, end_x, end_y)` — drag between coordinates.
- `browser_file_upload(file_path, index)` — upload a file to an `<input type="file">` element.

### Waiting for Things

Prefer these over `browser_wait` (fixed sleep):

- `browser_wait_for_load(timeout)` — wait for page load to complete. Use after every navigation.
- `browser_wait_for_element(selector, timeout)` — wait for a CSS selector to appear. Use after actions that dynamically add content.
- `browser_wait_for_text(text, timeout)` — wait for specific text to appear on the page.
- `browser_wait_for_download(timeout, save_to)` — wait for a file download to complete. Returns the file path.
- `browser_wait(seconds)` — fixed sleep. Use only as a last resort for animations or timing-sensitive pages.

### Managing Tabs

Your session's tabs are isolated from other agents. These tools only see tabs you own:

- `browser_list_tabs` — list your session's open tabs with IDs, titles, and URLs.
- `browser_switch_tab(tab_id)` — switch the active tab.
- `browser_close_tab(tab_id)` — close a tab (defaults to active). **Clean up tabs when done.**
- `browser_compare_tabs(tab_ids)` — compare content across multiple tabs (comma-separated IDs). Returns URL, title, and text preview for each.
- `browser_get_tab_events` — drain the queue of tab open/close/claim events since the last call. Useful for detecting popups or tabs opened by links.

### Discovering & Claiming Tabs

The workspace may contain tabs you didn't open — tabs opened by the user or abandoned by other agents. You can see all of them and claim the ones you want to work with.

- `browser_list_workspace_tabs` — list ALL tabs in the "Zen AI Agent" workspace, regardless of who owns them. Each tab includes:
  - `tab_id`, `title`, `url`
  - `ownership`: `"unclaimed"` (user-opened, no agent owns it), `"owned"` (active agent session), or `"stale"` (owner agent disconnected for 2+ minutes)
  - `is_mine`: `true` if you own this tab
  - `owner_session_id`: included for tabs owned by other agents (not for your own)

- `browser_claim_tab(tab_id)` — claim an unclaimed or stale tab into your session. You can pass either the tab ID or the tab's URL.
  - **Unclaimed tabs** (user-opened): claimed immediately.
  - **Stale tabs** (agent disconnected 2+ min): claimed and the previous owner is notified via a `tab_claimed_away` event.
  - **Actively owned tabs**: rejected with an error. You cannot steal tabs from active agents.
  - **Already yours**: returns success with `already_owned: true`.

After claiming, the tab behaves like any tab you created — you can navigate, read DOM, take screenshots, interact, etc. using its `tab_id`.

**Typical workflow:**
1. Call `browser_list_workspace_tabs` to see what's available.
2. Find tabs with `ownership: "unclaimed"` or `ownership: "stale"` that are relevant to your task.
3. Call `browser_claim_tab(tab_id)` to take ownership.
4. Use the tab normally with any other tool.

### Handling Dialogs & Popups

Pages may show alert/confirm/prompt dialogs that block interaction:

- `browser_get_dialogs` — check for pending dialogs. Returns type, message, and default value.
- `browser_handle_dialog(action, text)` — accept or dismiss the oldest dialog. Use `action="accept"` for OK/Yes, `action="dismiss"` for Cancel/No. Pass `text` for prompt dialogs.

### Console & JavaScript

For debugging or running custom logic on a page:

- `browser_console_setup` — start capturing console output. **Must be called first** before reading logs/errors.
- `browser_console_logs` — get captured console.log/warn/info/error messages (up to 500).
- `browser_console_errors` — get captured errors: console.error, uncaught exceptions, unhandled rejections (up to 100).
- `browser_console_teardown` — stop capturing and clean up listeners.
- `browser_console_eval(expression)` — execute JavaScript in the page's global scope and return the result. May be blocked by CSP on some pages.
- `browser_eval_chrome(expression)` — execute JavaScript in Firefox/Zen's privileged chrome context (XPCOM access: Services, gBrowser, IOUtils, etc.). Use for browser-level queries that page context can't do.

### Cookies & Storage

Read and modify cookies, localStorage, and sessionStorage:

- `browser_get_cookies(url, name)` — get cookies for the current domain or a specific URL. Filter by name optionally.
- `browser_set_cookie(name, value, ...)` — set a cookie with optional path, expires, sameSite, secure, httpOnly.
- `browser_delete_cookies(url, name)` — delete a specific cookie or all cookies for a domain.
- `browser_get_storage(storage_type, key)` — read from `localStorage` or `sessionStorage`. Omit key to dump all entries.
- `browser_set_storage(storage_type, key, value)` — write a key-value pair.
- `browser_delete_storage(storage_type, key)` — delete a key, or clear all if no key given.

### Network Monitoring & Interception

Observe and control network traffic:

- `browser_network_monitor_start` — start recording HTTP requests/responses (circular buffer, 500 entries).
- `browser_network_get_log(url_filter, method_filter, status_filter, limit)` — query captured requests. All filters are optional regex/values.
- `browser_network_monitor_stop` — stop recording (log buffer is preserved).
- `browser_intercept_add_rule(pattern, action, headers)` — block requests matching a URL regex, or modify their headers. `action` is `"block"` or `"modify_headers"`.
- `browser_intercept_remove_rule(rule_id)` / `browser_intercept_list_rules` — manage interception rules.

### Recording & Replay

Record a sequence of browser actions and replay them later:

- `browser_record_start` — start recording all subsequent actions (navigation, clicks, typing, etc.).
- `browser_record_stop` — stop recording. Returns the number of actions captured.
- `browser_record_save(file_path)` — save the recording to a JSON file.
- `browser_record_replay(file_path, delay)` — replay a recording with optional delay between actions (default 0.5s).

### Clipboard

- `browser_clipboard_read` — read text from the system clipboard.
- `browser_clipboard_write(text)` — write text to the clipboard. Paste with `browser_press_key("v", meta=True)` on macOS or `ctrl=True` on Linux.

### iframes

Many tools accept a `frame_id` parameter to target content inside iframes:

- `browser_list_frames` — list all frames in a tab with their IDs.
- Then pass `frame_id` to `browser_get_dom`, `browser_click`, `browser_fill`, `browser_console_eval`, etc.

### Saving Screenshots to Disk

- `browser_save_screenshot(file_path)` — capture and save to a file path. Use for visual evidence or reports.

---

## Human-In-The-Loop Escalation

Pause and notify the human when you encounter:
- CAPTCHA, anti-bot, or human verification challenges.
- 2FA/MFA prompts or passkey/security-key approvals.
- OAuth/SSO consent screens with scope grants.
- Irreversible actions (send DM/email, publish, purchase, delete).
- Permission prompts (notifications, camera, microphone, clipboard).
- Legal/terms acceptance dialogs.

When escalating, provide: current URL, tab title, what the human needs to do, screenshot if available, and what you're waiting for before resuming.

## Validation / Smoke Tests

```bash
PYTHONPATH=./mcp uv run --project ./mcp pytest tests/test_zenleap_mcp.py -q
uv run --project ./bench pytest bench/tests -q
./scripts/test_mcporter_parallel_sessions.sh  # expect PARALLEL_ISOLATION_TEST=PASS
```

## Uninstall / Cleanup

```bash
REPO="${REPO:-$HOME/zen-ai-agent}"
cd "$REPO"

# Remove from profiles
./install.sh --uninstall --yes

# Remove MCPorter config
npx -y mcporter --config ~/.mcporter/mcporter.json config remove zenleap

# Close remaining sessions
export ZENLEAP_SESSION_ID="$(uv run --project "$REPO/mcp" python "$REPO/mcp/zenleap_session.py" new)"
npx -y mcporter call zenleap.browser_session_close --output json
```

## Guardrails

- Do not reuse another active agent's `ZENLEAP_SESSION_ID`.
- Do not claim actively-owned tabs — only unclaimed or stale ones.
- Close your session (`browser_session_close`) when done to prevent stale resources.
- Close tabs you no longer need (`browser_close_tab`).
- Do not force-send messages or bypass verification gates.
- If blocked by a human-required step, stop and ask for human action.
