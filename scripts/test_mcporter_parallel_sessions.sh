#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_PROJECT="$ROOT_DIR/mcp"
MCP_SERVER_PY="$MCP_PROJECT/zenleap_mcp_server.py"
SESSION_HELPER_PY="$MCP_PROJECT/zenleap_session.py"
STDIO_CMD="uv run --project $MCP_PROJECT python $MCP_SERVER_PY"

new_session_id() {
  uv run --project "$MCP_PROJECT" python "$SESSION_HELPER_PY" new
}

json_get() {
  local key="$1"
  uv run --project "$MCP_PROJECT" python -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$key"
}

json_len() {
  uv run --project "$MCP_PROJECT" python -c 'import json,sys; print(len(json.load(sys.stdin)))'
}

mcall() {
  local sid="$1"
  shift
  npx -y mcporter call \
    --stdio "$STDIO_CMD" \
    --env "ZENLEAP_SESSION_ID=$sid" \
    "$@" \
    --output json
}

SID_A=""
SID_B=""
cleanup() {
  if [[ -n "${SID_A:-}" ]]; then
    mcall "$SID_A" browser_session_close >/dev/null 2>&1 || true
  fi
  if [[ -n "${SID_B:-}" ]]; then
    mcall "$SID_B" browser_session_close >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

SID_A="$(new_session_id)"
SID_B="$(new_session_id)"

echo "Session A: $SID_A"
echo "Session B: $SID_B"

A_CREATE="$(mcall "$SID_A" browser_create_tab url=https://example.com)"
A_TAB="$(echo "$A_CREATE" | json_get tab_id)"
mcall "$SID_A" browser_wait_for_load tab_id="$A_TAB" timeout=20000 >/dev/null

B_BASE="$(mcall "$SID_B" browser_list_tabs)"
B_BASE_COUNT="$(echo "$B_BASE" | json_len)"
if [[ "$B_BASE_COUNT" -ne 0 ]]; then
  echo "FAIL: session B can see tabs before opening its own" >&2
  exit 1
fi

B_CREATE="$(mcall "$SID_B" browser_create_tab url=https://www.wikipedia.org)"
B_TAB="$(echo "$B_CREATE" | json_get tab_id)"
mcall "$SID_B" browser_wait_for_load tab_id="$B_TAB" timeout=20000 >/dev/null

A_INFO="$(mcall "$SID_A" browser_get_page_info tab_id="$A_TAB")"
B_INFO="$(mcall "$SID_B" browser_get_page_info tab_id="$B_TAB")"
A_URL="$(echo "$A_INFO" | json_get url)"
B_URL="$(echo "$B_INFO" | json_get url)"

if ! echo "$A_URL" | rg -qi "example.com"; then
  echo "FAIL: session A URL mismatch ($A_URL)" >&2
  exit 1
fi
if ! echo "$B_URL" | rg -qi "wikipedia.org"; then
  echo "FAIL: session B URL mismatch ($B_URL)" >&2
  exit 1
fi

A_TABS="$(mcall "$SID_A" browser_list_tabs)"
B_TABS="$(mcall "$SID_B" browser_list_tabs)"
A_COUNT="$(echo "$A_TABS" | json_len)"
B_COUNT="$(echo "$B_TABS" | json_len)"

if [[ "$A_COUNT" -ne 1 ]]; then
  echo "FAIL: session A tab count should be 1 (got $A_COUNT)" >&2
  exit 1
fi
if [[ "$B_COUNT" -ne 1 ]]; then
  echo "FAIL: session B tab count should be 1 (got $B_COUNT)" >&2
  exit 1
fi

echo "PARALLEL_ISOLATION_TEST=PASS"
