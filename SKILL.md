---
name: zen-ai-agent
description: Use this skill when an agent must install, configure, and operate Zen AI Agent through canonical MCP and MCPorter CLI, including multi-agent session isolation, sub-agent coordination, safe browser automation, and clean uninstall.
---

# Zen AI Agent Skill

This skill defines how to set up and operate `zen-ai-agent` with canonical MCP server + MCPorter CLI.

## Zero-Touch Bootstrap (Raw SKILL.md Link Workflow)

If this skill is loaded from a raw URL on a fresh machine, do not assume local files exist. Bootstrap from GitHub first:

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

Then continue with setup/testing sections below.

To persist this skill locally (optional but recommended), copy `SKILL.md` into your agent skill directory:

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

## When To Use

Use this skill when you need to:
- install Zen browser automation into Zen profiles,
- run browser tools via MCP or MCPorter CLI,
- run from any directory on the machine,
- coordinate multiple agents/sub-agents safely,
- avoid session/tab leaks and unsafe autonomous actions.

## Prerequisites

- macOS or Linux with Zen Browser installed and run at least once.
- `uv`, `python`, `node`, `npm`/`npx` available.
- Zen profile has `fx-autoconfig` (ZenLeap includes this).

## Fresh Setup (Canonical MCP + MCPorter)

1. Enter repo:

```bash
cd "${HOME}/zen-ai-agent"
REPO="$(pwd)"
```

2. Install Python dependencies:

```bash
uv sync --project ./mcp
```

3. Install browser agent into Zen profiles.
Single profile:

```bash
./install.sh --profile 1 --yes
```

All detected profiles:

```bash
if [ "$(uname -s)" = "Darwin" ]; then
  PROFILE_BASE="$HOME/Library/Application Support/zen/Profiles"
else
  PROFILE_BASE="$HOME/.zen"
fi
count=$(find "$PROFILE_BASE" -maxdepth 1 -type d ! -name "Profiles" ! -path "$PROFILE_BASE" | wc -l | tr -d ' ')
for i in $(seq 1 "$count"); do ./install.sh --profile "$i" --yes; done
```

4. Configure MCPorter globally (usable from anywhere).
Use absolute paths and home scope:

```bash
# REPO should already be set from step 1.
npx -y mcporter config add zenleap \
  --stdio uv \
  --arg run \
  --arg --project \
  --arg "$REPO/mcp" \
  --arg python \
  --arg "$REPO/mcp/zenleap_mcp_server.py" \
  --scope home
```

5. Verify server is reachable:

```bash
npx -y mcporter list --json
npx -y mcporter list zenleap --schema
```

## Session Model (Required For Parallel Agents)

Session isolation is based on `ZENLEAP_SESSION_ID`.

- One top-level agent process = one unique session ID.
- Parent + its sub-agents should share the same session ID.
- Different top-level agents must not share a session ID.

Create/export a session for the current agent process:

```bash
export ZENLEAP_SESSION_ID="$(uv run --project "$REPO/mcp" python "$REPO/mcp/zenleap_session.py" new)"
```

If child processes do not inherit environment automatically, pass it explicitly:

```bash
ZENLEAP_SESSION_ID="$ZENLEAP_SESSION_ID" <sub-agent-command>
```

At end of workflow, cleanly close the session:

```bash
npx -y mcporter call zenleap.browser_session_close --output json
```

## Running MCPorter CLI Anywhere

After global config, run from any folder:

```bash
npx -y mcporter call zenleap.browser_create_tab url=https://www.wikipedia.org --output json
npx -y mcporter call zenleap.browser_wait_for_load timeout=20 --output json
npx -y mcporter call zenleap.browser_get_page_info --output json
```

Recommended output mode:
- use `--output json` for deterministic parsing in scripts.

## Tool Use Best Practices

- Always establish session first (`ZENLEAP_SESSION_ID`).
- Prefer `browser_wait_for_load`/`browser_wait_for_element` over fixed sleeps.
- For UI actions: `browser_get_dom` -> choose element index -> click/fill/select.
- Use `tab_id` explicitly in multi-tab flows.
- Close tabs you opened (`browser_close_tab`) when done.
- Close session (`browser_session_close`) at end to prevent stale resources.
- Use `browser_get_dialogs`/`browser_handle_dialog` to manage prompts.
- Use `browser_screenshot` or `browser_reflect` before risky actions.

## Human-In-The-Loop Escalation Rules

Pause and notify the human before continuing when any of these appear:
- CAPTCHA, anti-bot, or human verification challenges.
- 2FA/MFA prompts or passkey/security-key approvals.
- OAuth/SSO consent screens with scope grants.
- Actions with irreversible consequences (send DM/email, publish, purchase, delete).
- Permission prompts (notifications, camera, microphone, clipboard/system access).
- Legal/terms acceptance dialogs or policy acknowledgments.

When escalating, provide:
- current URL,
- tab title,
- concise action needed from human,
- screenshot reference (if available),
- exact resume condition (what you are waiting for).

## Validation / Smoke Tests

Canonical MCP tests:

```bash
PYTHONPATH=./mcp uv run --project ./mcp pytest tests/test_zenleap_mcp.py -q
uv run --project ./bench pytest bench/tests -q
```

Parallel isolation smoke test:

```bash
./scripts/test_mcporter_parallel_sessions.sh
```

Expected result:
- `PARALLEL_ISOLATION_TEST=PASS`

## Uninstall / Cleanup

Set repo path (if not already set):

```bash
REPO="${REPO:-$HOME/zen-ai-agent}"
cd "$REPO"
```

1. Remove browser agent from Zen profiles.
Single profile:

```bash
./install.sh --uninstall --profile 1 --yes
```

All profiles:

```bash
if [ "$(uname -s)" = "Darwin" ]; then
  PROFILE_BASE="$HOME/Library/Application Support/zen/Profiles"
else
  PROFILE_BASE="$HOME/.zen"
fi
count=$(find "$PROFILE_BASE" -maxdepth 1 -type d ! -name "Profiles" ! -path "$PROFILE_BASE" | wc -l | tr -d ' ')
for i in $(seq 1 "$count"); do ./install.sh --uninstall --profile "$i" --yes; done
```

2. Remove MCPorter config entries:

```bash
# Remove from project-local config (if used)
npx -y mcporter --config "$REPO/config/mcporter.json" config remove zenleap

# Remove from home/global config (if used)
npx -y mcporter --config ~/.mcporter/mcporter.json config remove zenleap
```

3. Close any remaining sessions (optional cleanup run):

```bash
export ZENLEAP_SESSION_ID="$(uv run --project "$REPO/mcp" python "$REPO/mcp/zenleap_session.py" new)"
npx -y mcporter call zenleap.browser_list_sessions --output json
npx -y mcporter call zenleap.browser_session_close --output json
```

4. Optionally remove repo:

```bash
cd ..
rm -rf zen-ai-agent
```

## Guardrails

- Do not reuse another active agent's `ZENLEAP_SESSION_ID`.
- Do not force-send messages or bypass verification gates.
- If blocked by human-required step, stop and ask for human action.
