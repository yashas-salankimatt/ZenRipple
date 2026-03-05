# ZenRipple — Claude Code Instructions

## After Compaction

When this conversation has been compacted (you see a summary of prior messages rather than the full history), **re-read SKILL.md** before making any browser tool calls or editing browser/MCP code:

```
Read SKILL.md
```

The SKILL.md contains critical details about MCPorter CLI syntax, session management, sub-agent isolation, tab persistence defaults, click strategy, and all 60+ tool docs. These details are lost during compaction summaries.

## Key References

- `SKILL.md` — Canonical tool docs, session management, MCPorter CLI usage, sub-agent isolation
- `browser/zenripple_agent.uc.js` — Browser-side chrome script (WebSocket server, replay viewer, tab management)
- `mcp/zenripple_mcp_server.py` — MCP server (tool implementations, replay recording)
- `tests/test_zenripple_mcp.py` — Test suite (run with `PYTHONPATH=./mcp uv run --project ./mcp pytest tests/test_zenripple_mcp.py -q`)

## Firefox Chrome Context Constraints

When editing `zenripple_agent.uc.js`:
- `<button>`, `<input>`, `<textarea>` are **silently stripped** from innerHTML — use `<div>`/`<span>` only
- Scrollbar CSS: `scrollbar-width: thin; scrollbar-color:` — NOT `::-webkit-scrollbar`
- XUL elements: use `document.createXULElement('menuitem')` for tab context menus
- `querySelector(':scope > ...')` for direct children in context menus (nested menuseparators cause insertBefore crashes)
- Wrap `init()` in try/catch — sine's `observe()` has no error handling, so uncaught throws prevent other mods from loading
