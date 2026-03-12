"""Shared session file helpers for ZenRipple auto-session management.

Terminal-keyed session persistence: each terminal (tmux pane, iTerm tab, etc.)
gets its own session file in ~/.zenripple/sessions/, keyed by a hash of the
terminal's identifying env var.
"""

import hashlib
import os
from pathlib import Path

SESSIONS_DIR = Path.home() / ".zenripple" / "sessions"

# Env vars checked (in priority order) to identify the calling terminal.
_CALLER_ENV_VARS = (
    "ZENRIPPLE_CALLER_ID",  # explicit override for sub-agents
    "TMUX_PANE",            # tmux: unique per pane (%0, %1, ...)
    "ITERM_SESSION_ID",     # iTerm2: unique per tab
    "TERM_SESSION_ID",      # Terminal.app: unique per window/tab
    "VSCODE_PID",           # VS Code: unique per instance
    "WINDOWID",             # X11: unique per terminal window
)


_caller_key_cache: str | None = None


def get_caller_key() -> str:
    """Stable identifier for the calling terminal/environment.

    Checks terminal-specific env vars that are unique per terminal window/pane/tab.
    Returns a 16-char hex hash, or 'default' if no terminal can be identified.
    Result is cached since env vars don't change within a process.
    """
    global _caller_key_cache
    if _caller_key_cache is not None:
        return _caller_key_cache
    for var in _CALLER_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            _caller_key_cache = hashlib.sha256(f"{var}:{val}".encode()).hexdigest()[:16]
            return _caller_key_cache
    _caller_key_cache = "default"
    return _caller_key_cache


def read_session_file() -> str:
    """Read session ID from this terminal's session file."""
    key = get_caller_key()
    try:
        return (SESSIONS_DIR / key).read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def write_session_file(session_id: str) -> None:
    """Atomically persist session ID to this terminal's session file.

    Uses write-to-temp + replace for cross-platform atomicity.
    """
    key = get_caller_key()
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.chmod(0o700)
        tmp = SESSIONS_DIR / f".{key}.tmp"
        tmp.write_text(session_id + "\n")
        tmp.chmod(0o600)
        tmp.replace(SESSIONS_DIR / key)
    except (OSError, PermissionError) as e:
        import sys
        print(f"Warning: could not write session file: {e}", file=sys.stderr)


def delete_session_file() -> None:
    """Remove this terminal's session file (e.g. on session close)."""
    key = get_caller_key()
    try:
        (SESSIONS_DIR / key).unlink(missing_ok=True)
    except (OSError, PermissionError) as e:
        import sys
        print(f"Warning: could not delete session file: {e}", file=sys.stderr)
