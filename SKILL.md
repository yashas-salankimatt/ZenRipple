---
name: zenripple
description: Use this skill when an agent needs to control Zen Browser — navigate pages, click elements, fill forms, take screenshots, read page content, manage tabs, and perform any browser automation task. ZenRipple gives AI agents full browser control via MCP.
---

# ZenRipple Skill

ZenRipple gives AI agents full control of [Zen Browser](https://zen-browser.app/) — navigation, DOM interaction, screenshots, form filling, JavaScript execution, network monitoring, and 60+ other browser automation tools via MCP.

All agent tabs live in a dedicated "ZenRipple" workspace inside Zen Browser. Each agent session is isolated: your tabs, events, and state are scoped to your session ID.

## Prerequisites

- macOS or Linux with Zen Browser installed and run at least once.
- **Python >= 3.13**, `uv`, `node`, `npm`/`npx` available.
- Zen profile has `fx-autoconfig` (ZenRipple includes this).

## Preflight (Run Every Time)

**Run this single block before doing anything else.** It auto-clones, auto-pulls, auto-installs, and auto-configures. If everything is already up to date, it finishes in seconds and you move on. If something is stale or missing, it fixes it automatically. The only thing it cannot do for you is restart Zen Browser — it will tell you if that's needed.

```bash
REPO_URL="https://github.com/yashas-salankimatt/zenripple.git"
REPO_DIR="${HOME}/zenripple"
NEEDS_RESTART=0

# ── 1. Ensure repo exists and is current ──
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch --quiet 2>/dev/null
  LOCAL=$(git -C "$REPO_DIR" rev-parse HEAD)
  REMOTE=$(git -C "$REPO_DIR" rev-parse @{u} 2>/dev/null || echo "$LOCAL")
  if [ "$LOCAL" != "$REMOTE" ]; then
    if git -C "$REPO_DIR" pull --ff-only 2>/dev/null; then
      echo "REPO: updated to $(git -C "$REPO_DIR" rev-parse --short HEAD)"
    else
      echo "REPO: WARNING — pull failed (local changes?). Run 'git -C $REPO_DIR status' to investigate."
    fi
  else
    echo "REPO: up to date"
  fi
else
  git clone "$REPO_URL" "$REPO_DIR"
  echo "REPO: cloned"
fi
REPO="$REPO_DIR"

# ── 2. Ensure Python dependencies ──
uv sync --project "$REPO/mcp" --quiet 2>/dev/null
echo "DEPS: synced"

# ── 3. Ensure MCPorter knows about zenripple ──
if npx -y mcporter list --json 2>/dev/null | grep -q zenripple; then
  echo "MCPORTER: configured"
else
  npx -y mcporter config add zenripple \
    --stdio uv --arg run --arg --project --arg "$REPO/mcp" \
    --arg python --arg "$REPO/mcp/zenripple_mcp_server.py" --scope home
  echo "MCPORTER: configured (was missing — added)"
fi

# ── 4. Ensure browser agent is installed and matches repo ──
# Use find instead of ls globs — zsh aborts ls if any glob has zero matches
INSTALLED_UC=$(
  find ~/Library/Application\ Support/zen/Profiles 2>/dev/null -path "*/chrome/JS/zenripple_agent.uc.js" -print -quit
  find ~/Library/Application\ Support/zen/Profiles 2>/dev/null -path "*/sine-mods/zenripple/JS/zenripple_agent.uc.js" -print -quit
  find ~/.zen 2>/dev/null -path "*/chrome/JS/zenripple_agent.uc.js" -print -quit
)
INSTALLED_UC=$(echo "$INSTALLED_UC" | head -1)

if [ -z "$INSTALLED_UC" ]; then
  echo "BROWSER AGENT: not found — installing..."
  "$REPO/install.sh" --yes
  NEEDS_RESTART=1
  echo "BROWSER AGENT: installed"
else
  STALE=0
  diff -q "$REPO/browser/zenripple_agent.uc.js" "$INSTALLED_UC" >/dev/null 2>&1 || STALE=1
  INSTALLED_ACTOR=$(find "$(dirname "$(dirname "$INSTALLED_UC")")" -path "*/actors/ZenRippleAgentChild.sys.mjs" 2>/dev/null | head -1)
  if [ -n "$INSTALLED_ACTOR" ] && [ -f "$REPO/browser/actors/ZenRippleAgentChild.sys.mjs" ]; then
    diff -q "$REPO/browser/actors/ZenRippleAgentChild.sys.mjs" "$INSTALLED_ACTOR" >/dev/null 2>&1 || STALE=1
  fi
  if [ "$STALE" -eq 1 ]; then
    echo "BROWSER AGENT: out of date — reinstalling..."
    "$REPO/install.sh" --yes
    NEEDS_RESTART=1
    echo "BROWSER AGENT: updated"
  else
    echo "BROWSER AGENT: up to date"
  fi
fi

# ── 5. Ensure installed skill file matches repo ──
SKILL_DEST=""
if [ -d "$HOME/.claude/skills" ]; then
  SKILL_DEST="$HOME/.claude/skills/zenripple"
elif [ -d "$HOME/.codex/skills" ]; then
  SKILL_DEST="$HOME/.codex/skills/zenripple"
fi
if [ -n "$SKILL_DEST" ]; then
  mkdir -p "$SKILL_DEST"
  if diff -q "$REPO/SKILL.md" "$SKILL_DEST/SKILL.md" >/dev/null 2>&1; then
    echo "SKILL: up to date"
  else
    cp "$REPO/SKILL.md" "$SKILL_DEST/SKILL.md"
    echo "SKILL: updated"
  fi
fi

# ── 6. Connectivity check or restart notice ──
if [ "$NEEDS_RESTART" -eq 1 ]; then
  echo ""
  echo "ACTION REQUIRED: Restart Zen Browser to load the updated agent, then proceed."
else
  if MCPORTER_CALL_TIMEOUT=10000 npx -y mcporter call zenripple.browser_ping --output json 2>/dev/null | grep -q pong; then
    echo "PING: connected — ready to use"
  else
    echo "PING: no connection — is Zen Browser running?"
  fi
fi
```

**After preflight completes:**
- If it says "ready to use" — skip to Sessions.
- If it says "ACTION REQUIRED: Restart Zen Browser" — tell the user to restart, then re-run the ping check.
- If it says "no connection" — Zen Browser may not be running or may need a restart.

**Compaction safeguard:** After preflight, check if `~/.claude/CLAUDE.md` contains a ZenRipple compaction reminder. If not, append one:

```bash
if ! grep -q 'zenripple/SKILL.md' ~/.claude/CLAUDE.md 2>/dev/null; then
  cat >> ~/.claude/CLAUDE.md <<'ZENRIPPLE_EOF'

## ZenRipple — Re-read after compaction
If this conversation has been compacted, re-read the ZenRipple skill file before making browser tool calls: `Read ~/.claude/skills/zenripple/SKILL.md`
ZENRIPPLE_EOF
  echo "CLAUDE.md: added ZenRipple compaction reminder"
else
  echo "CLAUDE.md: ZenRipple reminder already present"
fi
```

### Authentication

WebSocket connections are protected by a shared auth token. **This is fully automatic — no configuration needed.**

- On first startup, the browser agent generates a random token and saves it to `~/.zenripple/auth` (permissions 0600).
- The MCP server reads this token automatically and sends it as an `Authorization: Bearer` header on every connection.
- Connections without a valid token are rejected with `401 Unauthorized`.

**Override:** Set `ZENRIPPLE_AUTH_TOKEN` env var to use a custom token (useful for CI/Docker).

**Troubleshooting:** If you get `401 Unauthorized` errors, delete `~/.zenripple/auth` and restart Zen Browser to regenerate the token.

## Sessions

Sessions are **automatic**. Each terminal (tmux pane, iTerm tab, VS Code terminal, etc.) automatically gets its own browser session — just start using tools. No env vars or manual setup needed.

The MCP server identifies your terminal via env vars like `TMUX_PANE`, `ITERM_SESSION_ID`, `TERM_SESSION_ID`, or `VSCODE_PID`, and persists your session ID to `~/.zenripple/sessions/`. Subsequent calls from the same terminal reuse the same session.

```bash
# Just start using tools — session is created automatically on first call
npx -y mcporter call zenripple.browser_create_tab --args '{"url":"https://example.com"}' --output json

# Name the session (do this right after your first tool call)
# The name appears as a sublabel under each tab title in Zen's sidebar,
# so you can see which agent owns which tabs at a glance.
# First check what names other sessions are using, then pick a unique name.
npx -y mcporter call zenripple.browser_list_sessions --output json
# ^ Check the "name" field on each session to avoid duplicates
npx -y mcporter call zenripple.browser_set_session_name --args '{"name":"researcher"}' --output json
# ^ Returns: {"name": "researcher", "other_session_names": ["coder", ...]}

# Close when done
npx -y mcporter call zenripple.browser_session_close --output json
```

### Sub-Agent Isolation — IMPORTANT

> **Every sub-agent that uses the browser MUST get its own session.**
> Sharing a session between agents causes tab conflicts, race conditions, and corrupted replay logs. This is non-negotiable.

**Why this matters:**
- Each session has its own tabs, replay log, and screenshots
- Two agents sharing a session will step on each other's tabs and produce an interleaved, unusable replay log
- The replay viewer (Ctrl+Shift+E) shows one session at a time — separate sessions = separate replay histories

**How to spawn a sub-agent with its own session:**

1. **Generate a fresh session ID** before spawning the sub-agent:
```bash
SUB_SID="$(uv run --project ~/zenripple/mcp python ~/zenripple/mcp/zenripple_session.py new)"
```

2. **Tell the sub-agent its session ID** in the Agent tool prompt, and instruct it to prefix every MCPorter call with it:

```
Your ZenRipple session ID is: <the $SUB_SID value>

Prefix EVERY MCPorter browser call with your session ID:

  ZENRIPPLE_SESSION_ID="<SID>" npx -y mcporter call zenripple.<tool> --args '...' --output json

Before doing anything else, name your session:
  ZENRIPPLE_SESSION_ID="<SID>" npx -y mcporter call zenripple.browser_set_session_name --args '{"name":"research-agent"}' --output json

When finished, close your session:
  ZENRIPPLE_SESSION_ID="<SID>" npx -y mcporter call zenripple.browser_session_close --output json
```

The parent's session is unaffected — the `ZENRIPPLE_SESSION_ID` prefix only applies to that one MCPorter process.

**Common mistakes:**
- ❌ Sub-agent calling MCPorter without the `ZENRIPPLE_SESSION_ID` prefix — it will get the parent's session
- ❌ Not naming the sub-agent's session — makes it hard to tell tabs apart in the sidebar
- ❌ Not closing the sub-agent's session — leaves stale tabs and resources

### Pinned Session (Advanced)

To force all calls to use a specific session ID (e.g., to share a session across different machines or scripts):

```bash
export ZENRIPPLE_SESSION_ID="$(uv run --project "$REPO/mcp" python "$REPO/mcp/zenripple_session.py" new)"
```

When `ZENRIPPLE_SESSION_ID` is set, it takes priority over auto-session and no session file is written.

### Session Naming

Every session should be named immediately after creation. The name is displayed as a sublabel under each tab's title in the sidebar, making it easy to tell which agent session owns which tabs. Names are max 32 characters.

**Naming workflow:**
1. Call `browser_list_sessions` to see existing session names.
2. Pick a unique, descriptive name (e.g., `"researcher"`, `"code-reviewer"`, `"bug-fixer"`).
3. Call `browser_set_session_name(name)` — returns the set name and a list of other active session names.
4. To rename, simply call `browser_set_session_name` again with the new name.

**MCP (tool call):**
```
browser_set_session_name(name="researcher")
```

**MCPorter CLI:**
```bash
npx -y mcporter call zenripple.browser_set_session_name --args '{"name":"researcher"}' --output json
```

The name also appears in `browser_session_info` and `browser_list_sessions` responses.

### Session Management Tools

- `browser_ping` — health check. Verifies the browser agent is alive, returns version info for both MCP server and browser agent, and warns on version mismatch.
- `browser_session_info` — get current session ID, name, workspace, connection count, tab count.
- `browser_set_session_name(name)` — set or change the session's display name. Shown as sublabel on all session tabs. Max 32 chars. Pass an empty string to clear the name.
- `browser_session_close` — close the session: created tabs are closed, claimed tabs are released back to unclaimed.
- `browser_list_sessions` — list all active sessions with their names (admin/debug).
- `browser_session_save` / `browser_session_restore` — save/restore open tabs and cookies to a JSON file.

### Tab Status Indicators (Visual Feedback)

Agent tabs in Zen's sidebar show visual indicators so the user can see agent activity at a glance. These are automatic — no agent action is needed.

- **Active** (session accent color, bright) — An agent interacted with the tab in the last 60 seconds. Shows a colored gradient wash with breathing animation, a bright left accent stripe, and a glowing presence dot on the favicon. The sublabel appears in the session's accent color.
- **Claimed** (session accent color, dim) — An agent owns the tab but hasn't used it recently (>60s idle). Shows a dimmed colored gradient wash, a dimmed left accent stripe, and a smaller presence dot. The sublabel appears in a dimmed accent color.
- **Regular** — No agent association. Normal Zen tab appearance.

Each session is automatically assigned a unique accent color from a palette of 8 hues (cyan, violet, rose, lime, sky blue, fuchsia, orange, yellow). The color identifies the session; the brightness distinguishes active vs idle. Tabs are also auto-grouped by session in the sidebar so each session's tabs are adjacent.

Tabs automatically transition from active → claimed after 60 seconds of inactivity. When a session is destroyed, all indicators and sublabels are cleared.

The session name (set via `browser_set_session_name`) appears as a sublabel under each tab's title in the sidebar, colored to match the session's accent color. The `color_index` field in `session_info` and `list_sessions` responses indicates which palette color was assigned.

---

## OpenRouter API Key (for Grounded Clicks)

`browser_grounded_click` uses a VLM (Qwen3-VL-235B-A22B via OpenRouter) for pixel-accurate coordinate prediction. It needs an OpenRouter API key.

**The key only needs to be provided once.** On first use, pass it as an env var. The MCP server stores it in Firefox prefs automatically. All subsequent calls (even without the env var) will load it from the browser.

```bash
# First time — provide the key via env var:
OPENROUTER_API_KEY="sk-or-v1-..." npx -y mcporter call zenripple.browser_grounded_click --args '{"description": "the Submit button"}'

# Every time after — no env var needed, key is remembered:
npx -y mcporter call zenripple.browser_grounded_click --args '{"description": "the Submit button"}'
```

You can also read/write the stored key directly via `browser_eval_chrome` (the config commands are internal browser commands, not separate MCP tools):

```bash
# Check if a key is stored:
npx -y mcporter call zenripple.browser_eval_chrome --args '{"expression": "Services.prefs.getStringPref(\"zenripple.openrouter_api_key\", \"\")"}'

# Store a key manually:
npx -y mcporter call zenripple.browser_eval_chrome --args '{"expression": "Services.prefs.setStringPref(\"zenripple.openrouter_api_key\", \"sk-or-v1-...\"); \"ok\""}'
```

The grounding model, API URL, and coordinate mode are configurable via env vars, but the defaults work out of the box — you shouldn't need to change any of these:
- `ZENRIPPLE_GROUNDING_MODEL` — default: `qwen/qwen3-vl-235b-a22b-instruct`
- `ZENRIPPLE_GROUNDING_API_URL` — default: `https://openrouter.ai/api/v1/chat/completions`
- `ZENRIPPLE_GROUNDING_COORD_MODE` — default: `norm1000` (matches Qwen3-VL). Set to `absolute` only if switching to a model that outputs raw pixel coordinates (e.g., Qwen2.5-VL, UI-TARS).

---

## Click Strategy (IMPORTANT)

When you need to click something on a page, use this priority order. **Always start with DOM-based methods** and only fall back to coordinate-based methods when DOM methods fail.

### Priority 1: DOM Clicks (most reliable)

Use `browser_get_dom` or `browser_get_elements_compact` to find the element's index, then `browser_click(index)`.

```bash
# Get interactive elements
npx -y mcporter call zenripple.browser_get_elements_compact --output json
# Click element at index 5
npx -y mcporter call zenripple.browser_click --args '{"index": 5}'
```

Also try `browser_find_element_by_description` for fuzzy matching:

```bash
npx -y mcporter call zenripple.browser_find_element_by_description --args '{"description": "login button"}'
```

DOM clicks are pixel-perfect and never miss. Use them whenever the target is an interactive element visible in the DOM.

### Priority 2: Grounded Clicks (for visual/non-standard targets)

If the element isn't in the DOM index list (e.g., canvas content, custom-rendered UI, elements inside complex frameworks), use `browser_grounded_click(description)`. This takes a screenshot, sends it to a VLM for coordinate prediction, and clicks.

```bash
npx -y mcporter call zenripple.browser_grounded_click --args '{"description": "the circular target on the blue background"}'
```

Grounded clicks are very accurate for medium-to-large targets (buttons, icons, headings, links) but can miss on very dense UIs like spreadsheet cells where rows are only ~11px tall. Requires an OpenRouter API key (see above).

### Priority 3: Coordinate Clicks (last resort)

Only use `browser_click_coordinates(x, y)` as a last resort when both DOM clicks and grounded clicks are unavailable. Coordinates are estimated from screenshots and are the least reliable method.

```bash
npx -y mcporter call zenripple.browser_click_coordinates --args '{"x": 500, "y": 300}'
```

If screenshot and viewport dimensions differ, coordinates are auto-scaled from screenshot-space to viewport-space. Take a fresh screenshot before clicking to ensure the dimension cache is current.

### Visual distinction

- **Red crosshair** = regular `browser_click_coordinates` (manual coordinate click)
- **Cyan crosshair** = `browser_grounded_click` (VLM-grounded click)
- **No crosshair** = `browser_click` (DOM index click — always preferred)

---

## How To Use Your Tools

All tools are prefixed `browser_`. Most accept an optional `tab_id` (defaults to active tab) and `frame_id` (defaults to 0, the top frame).

### MCPorter CLI Syntax (CRITICAL)

**Always use `--args` with a JSON object** for tool parameters. Never use positional arguments or `-- --param` syntax — these break on values containing colons (like URLs).

```bash
# CORRECT — always use --args with JSON:
npx -y mcporter call zenripple.browser_create_tab --args '{"url":"https://example.com"}' --output json
npx -y mcporter call zenripple.browser_navigate --args '{"url":"https://example.com"}' --output json
npx -y mcporter call zenripple.browser_wait_for_load --args '{"timeout":15}' --output json
npx -y mcporter call zenripple.browser_click --args '{"index":5}' --output json
npx -y mcporter call zenripple.browser_fill --args '{"index":3,"value":"hello"}' --output json

# WRONG — do NOT use these formats:
# npx -y mcporter call zenripple.browser_navigate "https://example.com"          # URL colon parsed as key:value
# npx -y mcporter call zenripple.browser_navigate -- --url "https://example.com" # --url becomes literal text
```

Sessions are automatic — no need to export `ZENRIPPLE_SESSION_ID`. Each terminal gets its own session on first tool call.

Recommended shell alias:
```bash
alias mc='npx -y mcporter call'
# Then: mc zenripple.browser_navigate --args '{"url":"https://example.com"}' --output json
```

### Opening & Navigating Pages

To visit a URL, create a tab and wait for it to load:

- `browser_create_tab(url, persist)` — open a new tab with a URL (defaults to `about:blank`). **Always use `persist=true`** unless the tab is purely for agent scratch work that no human will ever need to see (see Tab Persistence section below).
- `browser_navigate(url)` — navigate the active (or specified) tab to a URL.
- `browser_go_back` / `browser_go_forward` — history navigation.
- `browser_reload` — refresh the page.
- `browser_batch_navigate(urls, persist)` — open multiple URLs at once (comma-separated). Returns tab IDs. **Always use `persist=true`** unless all tabs are throwaway agent scratch work.
- `browser_wait_for_load(timeout)` — wait until the page finishes loading. **Always use this after navigation instead of fixed sleeps.**
- `browser_get_navigation_status` — check HTTP status code, error code, and loading state after navigation. Useful for detecting 404s or network failures.

### Understanding a Page

Before interacting, you need to see what's on the page:

- `browser_screenshot` — take a visual screenshot. Returns the image inline — works when called as a direct MCP tool (the model sees the image natively). **MCPorter CLI note:** `browser_screenshot` returns base64 image data in JSON, which is unusable in terminal output. When using MCPorter CLI, use `browser_save_screenshot(file_path)` to save to disk, then read the file with your file-reading tool to view it.
- `browser_reflect(goal)` — get a screenshot + page text + metadata in one call. Best for getting a full picture before making decisions. Pass an optional `goal` to focus the analysis. Same MCPorter caveat as `browser_screenshot` — the inline image won't render in terminal output.
- `browser_get_page_info` — get URL, title, loading state, and navigation history.
- `browser_get_page_text` — get all visible text on the page. Good for reading content.
- `browser_get_page_html` — get full HTML source. Use when you need raw markup.
- `browser_get_dom` — get all interactive elements (buttons, links, inputs, etc.) with index numbers, attributes, and bounding boxes. **This is how you find elements to click/fill.** Supports `viewport_only`, `max_elements`, and `incremental` (diff against last call) options.
- `browser_get_elements_compact` — same interactive elements but 5-10x fewer tokens. Returns `[index] text (tag)` per line. Use when you just need indices, not full details.
- `browser_get_accessibility_tree` — get the semantic accessibility tree (role, name, value, depth). Useful for understanding structure without visual rendering.
- `browser_find_element_by_description(description)` — fuzzy-find elements by natural language (e.g., "login button", "search input"). Returns top 5 candidates with indices.

### Interacting with Elements

The general pattern: call `browser_get_dom` (or `browser_get_elements_compact`) to get element indices, then use those indices to interact.

- `browser_click(index)` — click an element by its index. **Always prefer this over coordinate methods.**
- `browser_grounded_click(description)` — click an element by natural language description using VLM grounding. Use when DOM index isn't available. Shows a cyan crosshair. Requires OpenRouter API key (stored automatically after first use).
- `browser_click_coordinates(x, y)` — click at exact pixel coordinates. **Last resort** — only use when DOM and grounded clicks both fail. Shows a red crosshair.
- `browser_fill(index, value)` — clear a form field and set a new value. Dispatches input/change events.
- `browser_select_option(index, value)` — select a dropdown option by value or visible text.
- `browser_type(text)` — type character-by-character into the focused element. Click an element first to focus it.
- `browser_press_key(key)` — press a key (Enter, Tab, Escape, ArrowDown, etc.) with optional `ctrl`/`shift`/`alt`/`meta` modifiers.
- `browser_scroll(direction, amount)` — scroll the page up/down/left/right by pixel amount (default: 500px down).
- `browser_scroll_at_point(x, y, direction, amount)` — scroll a specific element at the given coordinates. Use for overflow containers, dropdowns, or scrollable panels that aren't the main page. Auto-routes into iframes.
- `browser_grounded_scroll(description, direction, amount)` — scroll at an element described in natural language. Uses VLM grounding to find the coordinates, then scrolls there.
- `browser_hover(index)` — hover over an element by DOM index to reveal tooltips or dropdown menus.
- `browser_hover_coordinates(x, y)` — hover at exact pixel coordinates. Use for targets not in the DOM index. Shows a cursor overlay. Auto-routes into iframes.
- `browser_grounded_hover(description)` — hover at an element described in natural language. Uses VLM grounding to find the coordinates, then hovers. Useful for revealing tooltips, sub-menus, or hover-dependent UI.
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

- `browser_list_workspace_tabs` — list ALL tabs in the "ZenRipple" workspace, regardless of who owns them. Each tab includes:
  - `tab_id`, `title`, `url`
  - `ownership`: `"unclaimed"` (user-opened, no agent owns it), `"owned"` (active agent session), or `"stale"` (owner agent disconnected for 2+ minutes)
  - `is_mine`: `true` if you own this tab
  - `owner_session_id`: included for tabs owned by other agents (not for your own)
  - `claimed`: (only for your own tabs) `true` if the tab will survive session close (acquired via `browser_claim_tab` or created with `persist=true`), `false` if it will be destroyed on session close.

- `browser_claim_tab(tab_id)` — claim an unclaimed or stale tab into your session. You can pass either the tab ID or the tab's URL.
  - **Unclaimed tabs** (user-opened): claimed immediately.
  - **Stale tabs** (agent disconnected 2+ min): claimed and the previous owner is notified via a `tab_claimed_away` event.
  - **Actively owned tabs**: rejected with an error. You cannot steal tabs from active agents.
  - **Already yours**: returns success with `already_owned: true`.

After claiming, the tab is fully accessible — you can navigate, read DOM, take screenshots, interact, etc. using its `tab_id`. The key difference from created tabs: when your session closes, claimed tabs are released back to unclaimed status (not destroyed), so they persist in the workspace for future use.

**Typical workflow:**
1. Call `browser_list_workspace_tabs` to see what's available.
2. Find tabs with `ownership: "unclaimed"` or `ownership: "stale"` that are relevant to your task.
3. Call `browser_claim_tab(tab_id)` to take ownership.
4. Use the tab normally with any other tool.

### Tab Persistence (Keeping Tabs Alive After Session Close)

By default, all tabs created by your session are **closed** when the session is destroyed (explicit close, grace timer, or stale sweep). There are two ways to make tabs survive session destruction:

1. **`persist=true` on creation** — create a tab that will be released to unclaimed on session close instead of destroyed.
2. **`browser_claim_tab`** — claim an existing tab from the workspace. Claimed tabs are always released (not destroyed) on session close.

Both mechanisms work the same way internally. When your session closes, persistent/claimed tabs have their session ownership removed and revert to "unclaimed" status in the workspace, where they can be re-claimed by a future session.

**Default to `persist=true`.** The browser workspace is shared with the human user. If there is any chance a human will want to see, review, or continue working with a tab, it must persist. Tabs that vanish unexpectedly are confusing and disruptive.

**When to use `persist=true` (almost always):**
- Any tab the user asked you to open or find.
- Any result page, document, dashboard, or reference material.
- Any page you navigated to as part of fulfilling the user's request.
- Any tab the user might want to review after your session ends.
- When in doubt, persist. It's always safer.

**When to skip `persist` (rare):**
- A temporary intermediate page you'll close immediately (e.g., a Google search you navigate away from right after).
- A throwaway scratch tab used purely for agent-internal work that no human will ever need to see.

**If you realize a non-persistent tab should persist:** You cannot retroactively add persistence. Instead, claim the tab via `browser_claim_tab(tab_id)` — this re-acquires it with persistence enabled (claimed tabs always persist through session close).

**MCP (tool call):**
```
browser_create_tab(url="https://example.com", persist=true)
browser_batch_navigate(urls="https://a.com,https://b.com", persist=true)
```

**MCPorter CLI:**
```bash
npx -y mcporter call zenripple.browser_create_tab --args '{"url":"https://example.com","persist":true}' --output json
npx -y mcporter call zenripple.browser_batch_navigate --args '{"urls":"https://a.com,https://b.com","persist":true}' --output json
```

**Checking persistence status:**
Call `browser_list_workspace_tabs` — your tabs will have `claimed: true` if they will survive session close, `claimed: false` if they will be destroyed.

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

### Session Replay (Tool Call Log)

Automatically logs every tool call with a screenshot, arguments, and result. **Always-on by default** — auto-initializes when a session is active (no manual start needed). Works with MCPorter because state is stored on disk (JSONL + JPEG files), not in memory.

**How it works:**

Every tool call is automatically logged to `$TMPDIR/zenripple_replay_{session_id}/tool_log.jsonl`. Each log entry contains the tool name, arguments, result, timestamp, duration, and a reference to a JPEG screenshot captured at the time of the call. Screenshots are saved as individual JPEG files in the same directory.

**Viewing the replay:**

Press **Ctrl+Shift+E** while in the ZenRipple workspace in Zen Browser. On agent-owned tabs, it opens that session's replay directly. On other tabs, it attempts smart matching by URL or opens a session browser. This opens a three-panel modal:
- **Left:** Screenshot viewer — shows the screenshot from the selected tool call.
- **Center:** Tool call details — tool name, duration, timestamp, arguments, and result JSON.
- **Right:** Tool call list (fixed 280px) — most recent first, with timestamps, navigable with arrow keys or j/k (vim).

**Playback:** Press **Space** to play/pause auto-advance through entries. Use **[** / **]** to change speed (0.5×, 1×, 2×, 4×, 8×, 16×, 32×). A progress bar in the footer shows current position and supports click-to-seek.

Press Ctrl+Shift+E again or Esc to close.

**Opt-out:** Set `ZENRIPPLE_NO_REPLAY=1` to disable automatic tool call logging.

**Tools:**

- `browser_replay_status` — check if replay logging is active, tool call count, and storage directory.

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

## Shared Browser — Human-In-The-Loop Awareness

**This is a shared browser.** A human user is actively using the same Zen Browser instance. The agent workspace tabs are visible to them, and they may interact with pages at any time. This is by design — ZenRipple is a human-in-the-loop system, not a headless automation tool.

### Detecting User Activity

If you notice unexpected changes to a page or tab — a different URL than expected, new content that appeared, a form that's been filled in, a tab that was navigated somewhere else — **the user may have interacted with it.** Do not assume something is broken.

When you detect unexpected state:
1. **Evaluate the change.** Does it help or hinder your current task?
2. **If it helps or is neutral** — incorporate it and keep going. For example, if the user navigated to a more specific page than what you were looking for, use that.
3. **If it seems to hinder your task** — do NOT immediately stop. Most unexpected changes are incidental (the user browsing, clicking around, or doing their own thing in parallel). Only pause and defer to the user if you are confident that **both** of the following are true:
   - The change was clearly caused by the user (not a page redirect, ad, or script).
   - The user's intent was to take over or redirect the task — e.g., they navigated your tab to a completely different site, closed a tab you were using, or are actively typing into a form you were filling out.
   If both conditions are met, stop and let the user finish what they're doing. Then ask if they want you to resume.
4. **Otherwise, work around it.** If the user just happened to interact with a page but wasn't trying to take over, find another path to your goal — open a new tab, re-navigate, or adapt your approach. The default is to keep going, not to stop.

### Escalation

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
PYTHONPATH=./mcp uv run --project ./mcp pytest tests/test_zenripple_mcp.py -q
uv run --project ./bench pytest bench/tests -q
./scripts/test_mcporter_parallel_sessions.sh  # expect PARALLEL_ISOLATION_TEST=PASS
```

## Uninstall / Cleanup

```bash
REPO="${REPO:-$HOME/zenripple}"
cd "$REPO"

# Remove from profiles
./install.sh --uninstall --yes

# Remove MCPorter config
npx -y mcporter --config ~/.mcporter/mcporter.json config remove zenripple

# Close remaining sessions
export ZENRIPPLE_SESSION_ID="$(uv run --project "$REPO/mcp" python "$REPO/mcp/zenripple_session.py" new)"
npx -y mcporter call zenripple.browser_session_close --output json
```

## Guardrails

- **Name your session** early — call `browser_set_session_name` with a unique, descriptive name after your first tool call.
- **Default to `persist=true`** when creating tabs. Only skip persist for throwaway scratch tabs the user will never need.
- **Respect user activity.** If page state changed unexpectedly, the user may have acted. Evaluate whether it helps or blocks you, and adapt accordingly (see Shared Browser section above).
- Do not claim actively-owned tabs — only unclaimed or stale ones.
- Close your session (`browser_session_close`) when done to prevent stale resources.
- **Sub-agents MUST have their own sessions.** See "Sub-Agent Isolation" above — generate a fresh `ZENRIPPLE_SESSION_ID` before spawning any sub-agent that will use the browser.
- **Stale sessions / wrong tabs**: `rm ~/.zenripple/sessions/*` and retry to force fresh sessions.
- Close tabs you no longer need (`browser_close_tab`).
- Do not force-send messages or bypass verification gates.
- If blocked by a human-required step, stop and ask for human action.
