# Phase 9: Visual Grounding (Find Element by Description)

## Overview

Visual grounding allows finding interactive elements using natural language descriptions instead of exact CSS selectors or indices. This is implemented entirely in the MCP server (Python-side) with no browser changes — it leverages the existing `get_dom` data.

## MCP Tool

### `browser_find_element_by_description(description: str, tab_id: str = "", frame_id: int = 0)`

Finds interactive elements matching a natural language description.

**Examples**:
- `"login button"` → finds buttons with "login" in their text
- `"search input"` → finds input fields related to search
- `"navigation menu"` → finds nav-related elements
- `"submit form"` → finds submit buttons

**Returns** top 5 candidates with their indices, scores, and details:
```
Matches for 'login button':
  [3] <button> Log In</button> (score: 2/2)
  [7] <a role=button> Sign In</a> →/auth/login (score: 1/2)
```

## Algorithm

1. **Tokenize** the description into words (filtering out single-character words)
2. **Score** each interactive element by counting how many description words appear in:
   - Element text content
   - Tag name (`button`, `input`, `a`, etc.)
   - ARIA role
   - `href` attribute
   - `name` attribute
   - `type` attribute
3. **Sort** by score (descending) and return top 5

## Architecture

This is a **MCP-only tool** — it calls `get_dom` to get the element list, then does fuzzy matching in Python. No new browser-side commands were needed.

```python
@mcp.tool()
async def browser_find_element_by_description(description, tab_id="", frame_id=0):
    result = await browser_command("get_dom", params)
    elements = result["elements"]
    words = [w.lower() for w in description.split() if len(w) > 1]
    scored = []
    for el in elements:
        searchable = f"{text} {tag} {role} {href} {name} {type}"
        score = sum(1 for w in words if w in searchable)
        if score > 0:
            scored.append((score, el))
    return top 5 by score
```

## Use Cases

1. **Robust automation**: Instead of relying on fragile CSS selectors, describe what you want to click
2. **Dynamic pages**: When element indices change between page loads, descriptions remain stable
3. **Accessibility**: Leverages semantic information (roles, aria-labels) for matching
4. **Discovery**: When you know what you want to interact with but don't know its exact structure

## Limitations

- Matching is simple word-in-string — no semantic understanding (e.g., "log in" won't match "authenticate")
- Scores can be ambiguous when multiple elements contain similar text
- Requires `get_dom` to have been called (elements must be indexed)
- Single-character words are filtered out to reduce false matches
