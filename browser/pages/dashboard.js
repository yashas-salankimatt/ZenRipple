/* === ZenRipple Dashboard Client === */
/* Runs in content process. Communicates with chrome via WebSocket bridge. */

'use strict';

// ── WebSocket Bridge Client ────────────────────────────────

let _bridgeReady = false;
let _ws = null;
let _pendingRequests = new Map();
let _reqCounter = 0;
let _reconnectTimer = null;

async function _getToken() {
  const urlToken = new URLSearchParams(window.location.search).get('token');
  if (urlToken) return urlToken;
  try {
    const resp = await fetch('auth.txt');
    if (resp.ok) return (await resp.text()).trim();
  } catch (_) {}
  return '';
}

async function _connectWebSocket() {
  const token = await _getToken();
  const wsUrl = 'ws://localhost:9876/dashboard' + (token ? '?token=' + encodeURIComponent(token) : '');
  try { _ws = new WebSocket(wsUrl); } catch (e) { _scheduleReconnect(); return; }

  _ws.onopen = () => {
    console.log('[ZenRipple] WebSocket connected');
    if (!_bridgeReady) { _bridgeReady = true; _init(); }
  };
  _ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.id && _pendingRequests.has(msg.id)) {
        const { resolve, reject, timeout } = _pendingRequests.get(msg.id);
        clearTimeout(timeout);
        _pendingRequests.delete(msg.id);
        if (msg.error) reject(new Error(typeof msg.error === 'object' ? msg.error.message : String(msg.error)));
        else resolve(msg.result);
      }
    } catch (_) {}
  };
  _ws.onclose = () => { _ws = null; _scheduleReconnect(); };
  _ws.onerror = () => {};
}

function _scheduleReconnect() {
  if (_reconnectTimer) return;
  _reconnectTimer = setTimeout(() => { _reconnectTimer = null; _connectWebSocket(); }, 3000);
}

function bridgeCall(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (!_ws || _ws.readyState !== WebSocket.OPEN) { reject(new Error('Not connected')); return; }
    const id = 'req_' + (++_reqCounter);
    const timeout = setTimeout(() => { _pendingRequests.delete(id); reject(new Error('Timeout: ' + method)); }, 30000);
    _pendingRequests.set(id, { resolve, reject, timeout });
    _ws.send(JSON.stringify({ id, method: 'dashboard_' + method, params }));
  });
}

// ── Utilities ──────────────────────────────────────────────

function escapeHTML(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function extractTime(ts) {
  if (!ts) return '';
  try { const d = new Date(ts); return isNaN(d) ? '' : d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}); } catch(_) { return ''; }
}

function formatDuration(s) {
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

function syntaxHighlightJSON(str) {
  if (typeof str !== 'string') return '';
  const escaped = str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return escaped.replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|(-?\b\d+\.?\d*(?:[eE][+-]?\d+)?\b)|\b(true|false)\b|\b(null)\b/g,
    function(match, s, colon, num, bool, nul) {
      if (s) return colon ? '<span class="zr-key">'+s+'</span>'+colon : '<span class="zr-str">'+s+'</span>';
      if (num) return '<span class="zr-num">'+num+'</span>';
      if (bool) return '<span class="zr-bool">'+bool+'</span>';
      if (nul) return '<span class="zr-null">'+nul+'</span>';
      return match;
    }
  );
}

function formatJSON(val) {
  if (typeof val === 'string') { try { return JSON.stringify(JSON.parse(val), null, 2); } catch(_) { return val; } }
  if (typeof val === 'object' && val !== null) return JSON.stringify(val, null, 2);
  return String(val ?? '');
}

function _highlightBashCmd(cmd) {
  let html = escapeHTML(cmd);
  // Highlight 'zenripple' keyword with cyan
  html = html.replace(/\bzenripple\b/g, '<span class="zd-bash-zenripple">zenripple</span>');
  // Highlight common shell operators
  html = html.replace(/(\||\&amp;\&amp;|&gt;|2&gt;&amp;1|2&gt;\/dev\/null)/g, '<span class="zd-bash-op">$1</span>');
  // Highlight flags (--word or -x)
  html = html.replace(/((?:^|\s))(--?[a-zA-Z][\w-]*)/g, '$1<span class="zd-bash-flag">$2</span>');
  // Highlight quoted strings
  html = html.replace(/(&apos;[^&]*?&apos;|&#39;[^&]*?&#39;|'[^']*'|&quot;[^&]*?&quot;)/g, '<span class="zr-str">$1</span>');
  return html;
}

// ── Page Type Detection ────────────────────────────────────

const _params = new URLSearchParams(window.location.search);
const _pageType = _params.has('id') ? 'session' : _params.has('merged') ? 'merged' : 'overview';
let _sessionId = _params.get('id') || '';
const _mergedConvoPath = _params.get('merged') || '';

// ── State ──────────────────────────────────────────────────

let _replayEntries = [];
let _conversationEntries = [];
let _selectedReplayIdx = -1;
let _pollTimer = null;
let _lastReplayCount = -1;
let _lastConvoCount = -1;
let _screenshotCache = new Map();
let _scrollSyncRafId = null;
let _scrollSyncPaused = false;
let _scrollSyncListener = null;
const _POLL_MS = 2000;

// Lazy loading
let _convoReadBytes = 2 * 1024 * 1024; // Start with 2MB from end
const _CONVO_CHUNK_SIZE = 2 * 1024 * 1024;
let _convoHasMore = false;
let _convoLoadingMore = false;

// Playback
let _playbackTimer = null;
let _playbackPlaying = false;
let _playbackSpeedIdx = 1;
const _PLAYBACK_SPEEDS = [0.5, 1, 2, 4, 8, 16];
const _PLAYBACK_BASE_MS = 2000;

// ── Overview ───────────────────────────────────────────────

function statusLabel(s) { return {active:'Active',thinking:'Thinking',approval:'Waiting for Approval',idle:'Idle',ended:'Ended'}[s]||s; }
function statusClass(s) { return 'status-' + s; }

function buildCardHTML(card) {
  const cls = ['zd-card'];
  if (card.status === 'active') cls.push('zd-card-active');
  if (card.hasPendingApproval) cls.push('zd-card-approval');
  const dur = card.duration > 0 ? formatDuration(card.duration) : '';
  return `<div class="${cls.join(' ')}" data-session-id="${escapeHTML(card.sessionId)}" style="--card-sh:${card.color}">
    <div class="zd-card-header">
      <span class="zd-card-dot" style="background:rgb(${card.color})"></span>
      <span class="zd-card-name">${escapeHTML(card.name)}</span>
      <span class="zd-card-status ${statusClass(card.status)}">${escapeHTML(statusLabel(card.status))}</span>
    </div>
    ${card.lastAction ? `<span class="zd-card-last-action">Last: ${escapeHTML(card.lastAction.slice(0,60))}</span>` : ''}
    <div class="zd-card-stats"><span>${card.toolCount} calls</span>${dur?`<span>${escapeHTML(dur)}</span>`:''}</div>
    ${card.hasPendingApproval ? '<div class="zd-card-approval-badge">&#x26A0; Pending approval</div>' : ''}
  </div>`;
}

function buildOverviewHTML(cards) {
  let spawnBtn = '<div class="zd-spawn-btn" id="zd-spawn-btn">+ New Agent</div>';
  if (!cards.length) return spawnBtn + '<div class="zd-empty">No agent sessions found.</div>';
  const groups = new Map(), ungrouped = [];
  for (const c of cards) {
    if (c.conversationPath) {
      if (!groups.has(c.conversationPath)) {
        const parts = c.conversationPath.split('/');
        const pp = (parts[parts.length-2]||'').split('-').filter(Boolean);
        groups.set(c.conversationPath, { name: pp[pp.length-1]||'Project', convoId: (parts[parts.length-1]||'').replace('.jsonl','').slice(0,8), cards: [], hasActive: false });
      }
      const g = groups.get(c.conversationPath);
      g.cards.push(c);
      if (c.status !== 'ended') g.hasActive = true;
    } else ungrouped.push(c);
  }
  let html = spawnBtn;
  const sorted = [...groups.entries()].sort((a,b) => (a[1].hasActive===b[1].hasActive?0:a[1].hasActive?-1:1) || Math.max(0,...b[1].cards.map(c=>c.lastActivity||0))-Math.max(0,...a[1].cards.map(c=>c.lastActivity||0)));
  for (const [cp, g] of sorted) {
    html += '<div class="zd-convo-group"><div class="zd-convo-group-header">';
    html += '<span class="zd-convo-group-name">'+escapeHTML(g.name)+'</span>';
    html += '<span class="zd-convo-group-id">'+escapeHTML(g.convoId)+'</span>';
    html += '<span class="zd-convo-group-count">'+g.cards.length+' session'+(g.cards.length!==1?'s':'')+'</span>';
    if (g.cards.length > 1) html += '<div class="zd-convo-group-viewall" data-convo-path="'+escapeHTML(cp)+'">View All</div>';
    html += '</div><div class="zd-cards">';
    for (const c of g.cards) html += buildCardHTML(c);
    html += '</div></div>';
  }
  if (ungrouped.length) {
    html += '<div class="zd-section-label">Unlinked Sessions</div><div class="zd-cards">';
    for (const c of ungrouped) html += buildCardHTML(c);
    html += '</div>';
  }
  return html;
}

async function refreshOverview() {
  try {
    const cards = await bridgeCall('getSessionCards');
    const el = document.getElementById('zd-overview');
    if (!el) return;
    el.innerHTML = buildOverviewHTML(cards);
    for (const c of el.querySelectorAll('.zd-card'))
      c.addEventListener('click', () => { const sid = c.dataset.sessionId; if (sid) bridgeCall('openSessionTab', {sessionId:sid}); });
    for (const b of el.querySelectorAll('.zd-convo-group-viewall'))
      b.addEventListener('click', (e) => { e.stopPropagation(); bridgeCall('openMergedTab', {convoPath:b.dataset.convoPath}); });
    // Wire spawn button
    const spawnBtn = document.getElementById('zd-spawn-btn');
    if (spawnBtn) spawnBtn.addEventListener('click', _showSpawnDialog);
    const footer = document.getElementById('zd-footer');
    if (footer) {
      const active = cards.filter(c=>c.status!=='ended').length;
      footer.innerHTML = `<span class="zd-footer-item">${active} active</span>`;
    }

    // Auto-name unnamed sessions (fire-and-forget, one at a time to avoid API spam)
    _autoNameUnnamed(cards);
  } catch (e) { console.error('[ZenRipple] Overview error:', e); }
}

let _autoNamingInProgress = false;
async function _autoNameUnnamed(cards) {
  if (_autoNamingInProgress) return;
  // Find sessions with UUID-like or agent-hash names
  const unnamed = cards.filter(c =>
    /^[0-9a-f]{8}-/.test(c.name) || c.name.startsWith('agent-')
  );
  if (!unnamed.length) return;
  _autoNamingInProgress = true;
  try {
    // Name one at a time (each uses claude -p which costs tokens)
    for (const card of unnamed.slice(0, 3)) {
      try {
        const result = await bridgeCall('autoNameSession', { sessionId: card.sessionId });
        if (result?.name && !result.cached) {
          // Update the card in the DOM
          const cardEl = document.querySelector(`[data-session-id="${card.sessionId}"] .zd-card-name`);
          if (cardEl) cardEl.textContent = result.name;
        }
      } catch (_) {}
    }
  } finally {
    _autoNamingInProgress = false;
  }
}

// ── Spawn Dialog ──────────────────────────────────────────

function _showSpawnDialog() {
  // Remove existing dialog if any
  const existing = document.getElementById('zd-spawn-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'zd-spawn-overlay';
  overlay.className = 'zd-spawn-overlay';
  overlay.innerHTML = `
    <div class="zd-spawn-dialog">
      <div class="zd-spawn-header">
        <span class="zd-spawn-title">Spawn New Agent</span>
        <div class="zd-spawn-close" id="zd-spawn-close">&times;</div>
      </div>
      <div class="zd-spawn-body">
        <div class="zd-spawn-field">
          <label class="zd-spawn-label">Prompt</label>
          <textarea class="zd-spawn-textarea" id="zd-spawn-prompt" rows="4" maxlength="50000" placeholder="What should the agent do?"></textarea>
        </div>
        <div class="zd-spawn-row">
          <div class="zd-spawn-field" style="flex:1">
            <label class="zd-spawn-label">Name (optional)</label>
            <input class="zd-spawn-input" id="zd-spawn-name" type="text" placeholder="my-agent">
          </div>
          <div class="zd-spawn-field" style="flex:1">
            <label class="zd-spawn-label">Mode</label>
            <select class="zd-spawn-select" id="zd-spawn-mode">
              <option value="tmux">tmux (interactive)</option>
              <option value="headless">headless (background)</option>
            </select>
          </div>
        </div>
        <div class="zd-spawn-field">
          <label class="zd-spawn-label">Working Directory (optional)</label>
          <input class="zd-spawn-input" id="zd-spawn-workdir" type="text" placeholder="~/.zenripple/workspaces/...">
        </div>
      </div>
      <div class="zd-spawn-footer">
        <div class="zd-spawn-cancel" id="zd-spawn-cancel">Cancel</div>
        <div class="zd-spawn-submit" id="zd-spawn-submit">Spawn Agent</div>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  // Wire events
  const close = () => overlay.remove();
  overlay.querySelector('#zd-spawn-close').addEventListener('click', close);
  overlay.querySelector('#zd-spawn-cancel').addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

  overlay.querySelector('#zd-spawn-submit').addEventListener('click', async () => {
    const prompt = document.getElementById('zd-spawn-prompt').value.trim();
    if (!prompt) return;
    const name = document.getElementById('zd-spawn-name').value.trim() || undefined;
    const mode = document.getElementById('zd-spawn-mode').value;
    const workdir = document.getElementById('zd-spawn-workdir').value.trim() || undefined;

    const submitBtn = overlay.querySelector('#zd-spawn-submit');
    submitBtn.textContent = 'Spawning...';
    submitBtn.style.pointerEvents = 'none';

    try {
      const result = await bridgeCall('spawnAgent', { prompt, mode, name, workdir });
      console.log('[ZenRipple] Agent spawned:', result);
      close();
      // The bridge opens a session tab automatically — refresh overview after a delay
      setTimeout(() => refreshOverview(), 3000);
    } catch (e) {
      console.error('[ZenRipple] Spawn failed:', e);
      submitBtn.textContent = 'Failed — Retry';
      submitBtn.style.pointerEvents = '';
    }
  });

  // Focus prompt
  setTimeout(() => document.getElementById('zd-spawn-prompt')?.focus(), 100);
}

// ── Tmux Terminal View ────────────────────────────────────

function _initTerminalView(body, manifest) {
  body.innerHTML = `
    <div class="zd-terminal-view" id="zd-terminal-view">
      <div class="zd-terminal-header">
        <span class="zd-terminal-label">Terminal</span>
        <span class="zd-terminal-session">${escapeHTML(manifest.tmuxSession || '')}</span>
        <span class="zd-header-spacer"></span>
        <span class="zd-terminal-dims" id="zd-term-dims">--x--</span>
        <div class="zd-terminal-focus-hint" id="zd-term-focus-hint">Click to interact</div>
      </div>
      <pre class="zd-terminal-output" id="zd-term-output" tabindex="0"></pre>
      <div class="zd-terminal-status" id="zd-term-status">Connecting...</div>
    </div>
  `;

  const termEl = document.getElementById('zd-term-output');
  if (!termEl) return;

  // Focus/blur tracking
  termEl.addEventListener('focus', () => {
    _tmuxFocused = true;
    termEl.classList.add('focused');
    const hint = document.getElementById('zd-term-focus-hint');
    if (hint) hint.style.display = 'none';
  });
  termEl.addEventListener('blur', () => {
    _tmuxFocused = false;
    termEl.classList.remove('focused');
    const hint = document.getElementById('zd-term-focus-hint');
    if (hint) hint.style.display = '';
  });

  // Key handling — forward all keys to tmux when focused
  termEl.addEventListener('keydown', _handleTerminalKey);

  // Resize observer — resize tmux pane to match terminal element
  const resizeObs = new ResizeObserver(_debounce(() => _syncTerminalSize(termEl), 200));
  resizeObs.observe(termEl);

  // Initial size sync
  setTimeout(() => _syncTerminalSize(termEl), 500);

  // Start polling
  _startTmuxPolling();

  const statusEl = document.getElementById('zd-term-status');
  if (statusEl) statusEl.textContent = manifest.prompt?.slice(0, 100) || '';
}

function _debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

async function _syncTerminalSize(termEl) {
  if (!_tmuxSession || !termEl) return;
  // Calculate cols/rows from element size and font metrics
  const style = getComputedStyle(termEl);
  const fontSize = parseFloat(style.fontSize) || 12;
  const charWidth = fontSize * 0.6; // monospace approximation
  const lineHeight = parseFloat(style.lineHeight) || fontSize * 1.3;
  const width = Math.floor(termEl.clientWidth / charWidth);
  const height = Math.floor(termEl.clientHeight / lineHeight);
  if (width < 10 || height < 3) return;
  try {
    await bridgeCall('resizeTmuxPane', { tmuxSession: _tmuxSession, width, height });
    const dimsEl = document.getElementById('zd-term-dims');
    if (dimsEl) dimsEl.textContent = width + 'x' + height;
  } catch (_) {}
}

function _startTmuxPolling() {
  _stopTmuxPolling();
  _pollTmuxPane(); // immediate
  _tmuxPollTimer = setInterval(_pollTmuxPane, 300);
}

function _stopTmuxPolling() {
  if (_tmuxPollTimer) { clearInterval(_tmuxPollTimer); _tmuxPollTimer = null; }
}

let _tmuxPolling = false;
async function _pollTmuxPane() {
  if (!_tmuxSession || _tmuxPolling) return;
  _tmuxPolling = true;
  try {
    const result = await bridgeCall('captureTmuxPane', { tmuxSession: _tmuxSession });
    if (!result?.content && result?.content !== '') return;
    // Only update DOM if content changed
    if (result.content === _tmuxLastContent) return;
    _tmuxLastContent = result.content;
    const termEl = document.getElementById('zd-term-output');
    if (!termEl) return;
    termEl.innerHTML = _ansiToHtml(result.content);
    // Auto-scroll to bottom
    termEl.scrollTop = termEl.scrollHeight;
  } catch (_) {} finally {
    _tmuxPolling = false;
  }
}

// Map KeyboardEvent to tmux key name
function _mapKeyToTmux(e) {
  // Modifier prefixes
  const ctrl = e.ctrlKey, alt = e.altKey, shift = e.shiftKey, meta = e.metaKey;

  // Special keys
  const specialMap = {
    'Enter': 'Enter', 'Backspace': 'BSpace', 'Tab': 'Tab',
    'Escape': 'Escape', 'Delete': 'DC',
    'ArrowUp': 'Up', 'ArrowDown': 'Down', 'ArrowLeft': 'Left', 'ArrowRight': 'Right',
    'Home': 'Home', 'End': 'End', 'PageUp': 'PPage', 'PageDown': 'NPage',
    'F1': 'F1', 'F2': 'F2', 'F3': 'F3', 'F4': 'F4', 'F5': 'F5',
    'F6': 'F6', 'F7': 'F7', 'F8': 'F8', 'F9': 'F9', 'F10': 'F10',
    'F11': 'F11', 'F12': 'F12',
  };
  if (specialMap[e.key]) {
    let k = specialMap[e.key];
    if (shift && k.length > 1) k = 'S-' + k;
    if (alt) k = 'M-' + k;
    if (ctrl) k = 'C-' + k;
    return { key: k, literal: false };
  }
  // Ctrl+letter
  if (ctrl && e.key.length === 1 && /[a-z\\[\]^_@]/.test(e.key)) {
    return { key: 'C-' + e.key, literal: false };
  }
  // Alt+letter
  if (alt && e.key.length === 1) {
    return { key: 'M-' + e.key, literal: false };
  }
  // Space
  if (e.key === ' ') return { key: ' ', literal: true };
  // Regular printable character
  if (e.key.length === 1 && !ctrl && !alt && !meta) {
    return { key: e.key, literal: true };
  }
  return null;
}

async function _handleTerminalKey(e) {
  if (!_tmuxSession || !_tmuxFocused) return;
  // Don't intercept browser shortcuts (Cmd+C, Cmd+V, etc.)
  if (e.metaKey) return;

  const mapped = _mapKeyToTmux(e);
  if (!mapped) return;
  e.preventDefault();
  e.stopPropagation();

  try {
    await bridgeCall('sendKeysToTmux', {
      tmuxSession: _tmuxSession,
      keys: mapped.key,
      literal: mapped.literal,
    });
    // Poll immediately after key press for responsiveness
    setTimeout(_pollTmuxPane, 50);
  } catch (err) {
    console.error('[ZenRipple] sendKeys error:', err);
  }
}

// ── ANSI to HTML Converter ────────────────────────────────

const _ANSI_COLORS = [
  '#1e1e2e','#f38ba8','#a6e3a1','#f9e2af','#89b4fa','#cba6f7','#94e2d5','#cdd6f4', // 0-7
  '#585b70','#f38ba8','#a6e3a1','#f9e2af','#89b4fa','#cba6f7','#94e2d5','#ffffff', // 8-15
];

function _ansi256Color(n) {
  if (n < 16) return _ANSI_COLORS[n] || '#cdd6f4';
  if (n < 232) {
    n -= 16;
    const r = Math.floor(n / 36) * 51, g = Math.floor((n % 36) / 6) * 51, b = (n % 6) * 51;
    return `rgb(${r},${g},${b})`;
  }
  const v = (n - 232) * 10 + 8;
  return `rgb(${v},${v},${v})`;
}

function _ansiToHtml(text) {
  let html = '';
  let fg = '', bg = '', bold = false, dim = false, italic = false, underline = false, strikethrough = false, inverse = false;
  let spanOpen = false;

  function openSpan() {
    if (spanOpen) html += '</span>';
    const styles = [];
    let fgColor = fg, bgColor = bg;
    if (inverse) { const tmp = fgColor; fgColor = bgColor || '#1e1e2e'; bgColor = tmp || '#cdd6f4'; }
    if (fgColor) styles.push('color:' + fgColor);
    if (bgColor) styles.push('background:' + bgColor);
    if (bold) styles.push('font-weight:bold');
    if (dim) styles.push('opacity:0.5');
    if (italic) styles.push('font-style:italic');
    if (underline) styles.push('text-decoration:underline');
    if (strikethrough) styles.push('text-decoration:line-through');
    if (styles.length) {
      html += '<span style="' + styles.join(';') + '">';
      spanOpen = true;
    } else {
      spanOpen = false;
    }
  }

  const re = /\x1b\[([0-9;]*)m/g;
  let lastIdx = 0, match;
  while ((match = re.exec(text)) !== null) {
    // Output text before this escape
    const chunk = text.slice(lastIdx, match.index);
    if (chunk) html += escapeHTML(chunk);
    lastIdx = re.lastIndex;

    // Parse SGR codes
    const codes = match[1] ? match[1].split(';').map(Number) : [0];
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) { fg = ''; bg = ''; bold = false; dim = false; italic = false; underline = false; strikethrough = false; inverse = false; }
      else if (c === 1) bold = true;
      else if (c === 2) dim = true;
      else if (c === 3) italic = true;
      else if (c === 4) underline = true;
      else if (c === 7) inverse = true;
      else if (c === 9) strikethrough = true;
      else if (c === 22) { bold = false; dim = false; }
      else if (c === 23) italic = false;
      else if (c === 24) underline = false;
      else if (c === 27) inverse = false;
      else if (c === 29) strikethrough = false;
      else if (c >= 30 && c <= 37) fg = _ANSI_COLORS[c - 30];
      else if (c === 38 && codes[i+1] === 5) { fg = _ansi256Color(codes[i+2]); i += 2; }
      else if (c === 38 && codes[i+1] === 2) { fg = `rgb(${codes[i+2]},${codes[i+3]},${codes[i+4]})`; i += 4; }
      else if (c === 39) fg = '';
      else if (c >= 40 && c <= 47) bg = _ANSI_COLORS[c - 40];
      else if (c === 48 && codes[i+1] === 5) { bg = _ansi256Color(codes[i+2]); i += 2; }
      else if (c === 48 && codes[i+1] === 2) { bg = `rgb(${codes[i+2]},${codes[i+3]},${codes[i+4]})`; i += 4; }
      else if (c === 49) bg = '';
      else if (c >= 90 && c <= 97) fg = _ANSI_COLORS[c - 90 + 8];
      else if (c >= 100 && c <= 107) bg = _ANSI_COLORS[c - 100 + 8];
    }
    openSpan();
  }
  // Remaining text
  const tail = text.slice(lastIdx);
  if (tail) html += escapeHTML(tail);
  if (spanOpen) html += '</span>';

  // Strip other escape sequences (cursor, window title, etc.)
  html = html.replace(/\x1b\[[0-9;]*[A-HJKSTfhlmn]/g, '');
  html = html.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '');
  html = html.replace(/\x1b[()][AB012]/g, '');
  html = html.replace(/\x1b\[[\?]?[0-9;]*[hlrm]/g, '');

  return html;
}

// ── Headless Output View ──────────────────────────────────

let _headlessName = null;
let _headlessPollTimer = null;
let _headlessLastCount = -1;

function _initHeadlessView(body, manifest) {
  _headlessName = manifest.name;
  body.innerHTML = `
    <div class="zd-headless-view" id="zd-headless-view">
      <div class="zd-terminal-header">
        <span class="zd-terminal-label">Headless Agent</span>
        <span class="zd-terminal-session">${escapeHTML(manifest.name || '')}</span>
        <span class="zd-header-spacer"></span>
        <span class="zd-headless-status" id="zd-headless-status">Checking...</span>
      </div>
      <div class="zd-headless-output" id="zd-headless-output"><div class="zd-empty">Loading output...</div></div>
      <div class="zd-terminal-status" id="zd-headless-prompt">${escapeHTML((manifest.prompt || '').slice(0, 200))}</div>
    </div>
  `;
  _startHeadlessPolling();
}

function _startHeadlessPolling() {
  _stopHeadlessPolling();
  _pollHeadlessOutput();
  _headlessPollTimer = setInterval(_pollHeadlessOutput, 2000);
}

function _stopHeadlessPolling() {
  if (_headlessPollTimer) { clearInterval(_headlessPollTimer); _headlessPollTimer = null; }
}

async function _pollHeadlessOutput() {
  if (!_headlessName) return;
  try {
    const data = await bridgeCall('getHeadlessOutput', { name: _headlessName });
    if (!data) return;

    const statusEl = document.getElementById('zd-headless-status') || document.getElementById('zd-claude-status');
    if (statusEl) {
      statusEl.textContent = data.alive ? 'Headless agent running...' : 'Headless agent completed';
    }

    // Only re-render if line count changed
    if (data.lineCount === _headlessLastCount) return;
    _headlessLastCount = data.lineCount;

    const el = document.getElementById('zd-headless-output') || document.getElementById('zd-conversation');
    if (!el || !data.entries?.length) {
      if (el && !data.entries?.length) el.innerHTML = '<div class="zd-empty">No output yet...</div>';
      return;
    }

    let html = '';
    for (const entry of data.entries) {
      if (entry.role === 'assistant' && entry.type === 'text') {
        html += '<div class="zd-msg zd-msg-assistant"><div class="zd-msg-label agent-label">Agent</div><div class="zd-msg-content">' + escapeHTML(entry.content.slice(0, 2000)) + '</div></div>';
      } else if (entry.role === 'assistant' && entry.type === 'tool_use') {
        const inputStr = entry.input ? JSON.stringify(entry.input, null, 2).slice(0, 500) : '';
        html += '<div class="zd-tool-block zd-other-tool"><div class="zd-tool-header"><span class="zd-tool-name">' + escapeHTML(entry.name) + '</span></div>';
        if (inputStr) html += '<div class="zd-tool-body expanded"><div class="zd-tool-json">' + syntaxHighlightJSON(inputStr) + '</div></div>';
        html += '</div>';
      } else if (entry.role === 'result') {
        html += '<div class="zd-msg zd-msg-assistant" style="border-left:3px solid var(--zr-success);"><div class="zd-msg-label agent-label">Result</div><div class="zd-msg-content">' + escapeHTML(entry.content.slice(0, 2000)) + '</div></div>';
      }
    }
    el.innerHTML = html;
    el.scrollTop = el.scrollHeight;

    // Stop polling once complete
    if (!data.alive) _stopHeadlessPolling();
  } catch (_) {}
}

// ── Session Detail ─────────────────────────────────────────

// ── Tmux Terminal View State ───────────────────────────────
let _tmuxSession = null; // Set when session has a tmux pane to capture
let _tmuxPollTimer = null;
let _tmuxLastContent = '';
let _tmuxFocused = false;

async function initSessionDetail() {
  const back = document.getElementById('zd-back');
  if (back) back.addEventListener('click', () => bridgeCall('openDashboardTab'));

  const body = document.getElementById('zd-body');
  if (!body) return;

  let info = null;
  try {
    const method = _pageType === 'merged' ? 'getMergedSessionInfo' : 'getSessionInfo';
    const params = _pageType === 'merged' ? { convoPath: _mergedConvoPath } : { sessionId: _sessionId };
    info = await bridgeCall(method, params);
  } catch (_) {}

  // For merged views, resolve _sessionId from the first session that links to this conversation
  if (_pageType === 'merged' && !_sessionId && _mergedConvoPath) {
    try {
      const cards = await bridgeCall('getSessionCards');
      const match = cards.find(c => c.conversationPath === _mergedConvoPath);
      if (match) _sessionId = match.sessionId;
    } catch (_) {}
  }

  // Check if this is a spawned tmux session
  let manifest = null;
  if (_pageType === 'session' && _sessionId) {
    try { manifest = await bridgeCall('getSessionManifest', { sessionId: _sessionId }); } catch (_) {}
  }

  const title = document.getElementById('zd-title');
  const badge = document.getElementById('zd-badge');
  const displayName = manifest?.name || info?.name || _sessionId?.slice(0,12) || 'Session';
  if (title) title.textContent = displayName;
  if (badge) badge.textContent = manifest?.mode === 'tmux' ? 'tmux' : (info?.status || '');
  // Set tab title for easy identification
  if (_pageType === 'merged') {
    document.title = 'ZR- ' + (_mergedConvoPath.split('/').pop()?.replace('.jsonl','').slice(0,8) || 'merged');
  } else {
    document.title = 'ZR- ' + (displayName || _sessionId?.slice(0,8) || 'session');
  }

  // For spawned tmux sessions, track the FULL tmux session name for terminal rendering
  // Use tmuxSession from manifest directly (not manifest.name which can be renamed)
  if (manifest?.tmuxSession) {
    _tmuxSession = manifest.tmuxSession;
  }

  body.innerHTML = `
    <div class="zd-detail">
      <div class="zd-replay-col" id="zd-replay-col">
        <div class="zd-replay-screenshot" id="zd-replay-ss">
          <span class="zd-no-screenshot">No screenshot</span>
        </div>
        <div class="zd-replay-transport">
          <div class="zd-transport-btn" id="zd-play-btn" title="Play (Space)">&#x25B6;</div>
          <div class="zd-transport-btn" id="zd-slower-btn" title="Slower ([)">&#x2BC7;</div>
          <span class="zd-transport-speed" id="zd-speed">1x</span>
          <div class="zd-transport-btn" id="zd-faster-btn" title="Faster (])">&#x2BC8;</div>
          <div class="zd-transport-progress" id="zd-progress">
            <div class="zd-transport-progress-fill" id="zd-progress-fill"></div>
          </div>
          <span class="zd-transport-count" id="zd-count">0</span>
          <div class="zd-transport-btn" id="zd-replay-toggle" title="Toggle tool list">&#x25BC;</div>
        </div>
        <div class="zd-splitter zd-splitter-h" data-split="replay-inner"></div>
        <div class="zd-replay-list" id="zd-replay-entries"></div>
      </div>
      <div class="zd-splitter zd-splitter-v" data-split="left"></div>
      <div class="zd-conversation-col" id="zd-conversation-col">
        <div class="zd-convo-tabs">
          <div class="zd-convo-tab active" data-tab="conversation">Conversation</div>
          <div class="zd-convo-tab" data-tab="tool-detail">Tool Details</div>
        </div>
        <div class="zd-conversation-scroll" id="zd-conversation"><div class="zd-empty">Loading...</div></div>
        <div class="zd-tool-detail-view" id="zd-tool-detail"></div>
        <div class="zd-claude-status" id="zd-claude-status"></div>
        <div class="zd-claude-input-wrapper">
          <input class="zd-claude-input" id="zd-claude-input" type="text" placeholder="Send to Claude Code...">
          <div class="zd-claude-send" id="zd-claude-send">&#x2192;</div>
          <div class="zd-claude-stop" id="zd-claude-stop" style="display:none" title="Stop Claude">&#x25A0;</div>
        </div>
      </div>
      <div class="zd-splitter zd-splitter-v" data-split="right"></div>
      <div class="zd-right-col" id="zd-right-col">
        <div class="zd-right-section zd-approvals-section">
          <div class="zd-right-section-header">Approvals</div>
          <div class="zd-approvals-scroll" id="zd-approvals"></div>
        </div>
        <div class="zd-right-section zd-messages-section">
          <div class="zd-right-section-header">Messages</div>
          <div class="zd-messages-scroll" id="zd-messages"></div>
          <div class="zd-message-input-wrapper">
            <input class="zd-message-input" id="zd-msg-input" type="text" placeholder="Send message to agent...">
            <div class="zd-message-send" id="zd-msg-send">&#x2192;</div>
          </div>
        </div>
      </div>
    </div>
  `;

  // Tool list collapse/expand toggle (applies to all session types)
  const replayToggle = document.getElementById('zd-replay-toggle');
  if (replayToggle) {
    const replayListForToggle = document.getElementById('zd-replay-entries');
    const replaySplitterForToggle = document.querySelector('.zd-splitter-h[data-split="replay-inner"]');

    const setCollapsed = (collapsed) => {
      if (replayListForToggle) replayListForToggle.style.display = collapsed ? 'none' : '';
      if (replaySplitterForToggle) replaySplitterForToggle.style.display = collapsed ? 'none' : '';
      replayToggle.textContent = collapsed ? '\u25B6' : '\u25BC';
      replayToggle.title = collapsed ? 'Expand tool list' : 'Collapse tool list';
      const state = _loadSplitterState();
      state.toolListCollapsed = collapsed;
      _saveSplitterState(state);
    };

    const savedToggle = _loadSplitterState();
    if (savedToggle.toolListCollapsed) setCollapsed(true);

    replayToggle.addEventListener('click', () => {
      setCollapsed(replayListForToggle?.style.display !== 'none');
    });
  }

  // For tmux sessions: replace conversation column with terminal view
  if (_tmuxSession) {
    // Replace conversation scroll with terminal pre
    const convoScroll = document.getElementById('zd-conversation');
    if (convoScroll) {
      convoScroll.outerHTML = '<pre class="zd-terminal-output" id="zd-term-output" tabindex="0"></pre>';
    }
    // Rename "Conversation" tab to "Terminal", keep "Tool Details" tab
    const convoTab = document.querySelector('.zd-convo-tab[data-tab="conversation"]');
    if (convoTab) convoTab.textContent = 'Terminal';

    // Hide the bottom input bar (interaction goes through the terminal directly)
    const claudeInputWrapper = document.querySelector('.zd-claude-input-wrapper');
    if (claudeInputWrapper) claudeInputWrapper.style.display = 'none';
    const claudeStatus = document.getElementById('zd-claude-status');
    if (claudeStatus) claudeStatus.style.display = 'none';

    // Wire terminal keyboard interactivity (when <pre> is focused)
    const termEl = document.getElementById('zd-term-output');
    if (termEl) {
      termEl.addEventListener('focus', () => { _tmuxFocused = true; termEl.classList.add('focused'); });
      termEl.addEventListener('blur', () => { _tmuxFocused = false; termEl.classList.remove('focused'); });
      termEl.addEventListener('keydown', _handleTerminalKey);
      // Resize observer
      const resizeObs = new ResizeObserver(_debounce(() => _syncTerminalSize(termEl), 200));
      resizeObs.observe(termEl);
      setTimeout(() => _syncTerminalSize(termEl), 500);
    }

    // Tab switching — handle terminal vs tool-detail
    for (const tab of document.querySelectorAll('.zd-convo-tab')) {
      tab.addEventListener('click', () => {
        for (const t of document.querySelectorAll('.zd-convo-tab')) t.classList.remove('active');
        tab.classList.add('active');
        const which = tab.dataset.tab;
        const termOutput = document.getElementById('zd-term-output');
        const td = document.getElementById('zd-tool-detail');
        if (termOutput) termOutput.style.display = which === 'conversation' ? '' : 'none';
        if (td) { td.style.display = which === 'tool-detail' ? 'block' : 'none'; if (which === 'tool-detail') _updateToolDetailView(); }
      });
    }

    _wireInput('zd-msg-input', 'zd-msg-send', (text) => bridgeCall('sendHumanMessage', { sessionId: _sessionId, text }));
    _setupTransportControls();
    _setupSplitters();
    _convoReadBytes = _CONVO_CHUNK_SIZE;
    await _loadSessionData();
    _startTmuxPolling();
    return;
  }

  // Wire inputs (standard conversation mode — works for headless too via conversation.link)
  _wireInput('zd-claude-input', 'zd-claude-send', (text) => bridgeCall('sendToClaudeCode', { sessionId: _sessionId, text }));
  _wireInput('zd-msg-input', 'zd-msg-send', (text) => bridgeCall('sendHumanMessage', { sessionId: _sessionId, text }));

  // Wire stop button
  const stopBtn = document.getElementById('zd-claude-stop');
  if (stopBtn) stopBtn.addEventListener('click', async () => {
    try {
      await bridgeCall('stopClaude', { sessionId: _sessionId });
      const statusEl = document.getElementById('zd-claude-status');
      if (statusEl) statusEl.textContent = 'Stopped';
      stopBtn.style.display = 'none';
    } catch (e) { console.error('[ZenRipple] Stop failed:', e); }
  });

  // Tab switching
  for (const tab of document.querySelectorAll('.zd-convo-tab')) {
    tab.addEventListener('click', () => {
      for (const t of document.querySelectorAll('.zd-convo-tab')) t.classList.remove('active');
      tab.classList.add('active');
      const which = tab.dataset.tab;
      const cs = document.getElementById('zd-conversation');
      const td = document.getElementById('zd-tool-detail');
      if (cs) cs.style.display = which === 'conversation' ? '' : 'none';
      if (td) { td.style.display = which === 'tool-detail' ? 'block' : 'none'; if (which === 'tool-detail') _updateToolDetailView(); }
    });
  }

  // Transport controls
  _setupTransportControls();

  // Splitters
  _setupSplitters();

  // Load data
  _convoReadBytes = _CONVO_CHUNK_SIZE; // Reset on new session
  await _loadSessionData();

  // Scroll sync + lazy loading
  _setupScrollSync();
  _setupScrollUpLoader();
}

function _wireInput(inputId, sendId, handler) {
  const input = document.getElementById(inputId);
  const send = document.getElementById(sendId);
  if (!input || !send) return;
  const doSend = () => { const t = input.value.trim(); if (t) { input.value = ''; handler(t).catch(e => console.error(e)); } };
  send.addEventListener('click', doSend);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSend(); });
}

// ── Data Loading (with change detection) ───────────────────

async function _loadSessionData() {
  try {
    const method = _pageType === 'merged' ? 'getMergedSessionData' : 'getSessionData';
    const params = _pageType === 'merged' ? { convoPath: _mergedConvoPath } : { sessionId: _sessionId };
    const data = await bridgeCall(method, params);

    // Only re-render replay if data changed
    if (data.replayEntries && data.replayEntries.length !== _lastReplayCount) {
      const isFirstLoad = _lastReplayCount < 0;
      const wasEmpty = _lastReplayCount === 0;
      _replayEntries = data.replayEntries;
      _lastReplayCount = data.replayEntries.length;
      _renderReplayList();
      // Auto-select latest on first load or when replay first appears
      if ((isFirstLoad || wasEmpty) && _replayEntries.length > 0) {
        _selectReplayEntry(_replayEntries.length - 1, false);
      }
    }

    // Load conversation
    if (_pageType === 'merged' && data.conversationEntries) {
      // Merged view: use conversation entries from getMergedSessionData
      if (data.conversationEntries.length !== _lastConvoCount) {
        _conversationEntries = data.conversationEntries;
        _lastConvoCount = data.conversationEntries.length;
        _convoHasMore = false;
        _renderConversation();
      }
    } else {
      // Standard: incremental conversation reads
      if (_lastConvoCount === 0) _lastConvoCount = -1;
      await _loadConversation();
    }

    if (data.approvals) _renderApprovals(data.approvals);
    if (data.messages) _renderMessages(data.messages);

    // Poll Claude status for stop button
    _pollClaudeStatus();

    const footer = document.getElementById('zd-footer');
    if (footer) footer.innerHTML = `<span class="zd-footer-item">${_replayEntries.length} calls</span><span class="zd-footer-item">${escapeHTML(data.status||'')}</span>`;
  } catch (e) { console.error('[ZenRipple] Load error:', e); }
}

async function _loadConversation() {
  try {
    const sid = _pageType === 'merged' ? '' : _sessionId;
    const data = await bridgeCall('getConversation', { sessionId: sid || _sessionId, readBytes: _convoReadBytes });
    if (!data) return;

    // Only re-render if count changed
    if (data.entries && data.entries.length !== _lastConvoCount) {
      _conversationEntries = data.entries;
      _lastConvoCount = data.entries.length;
      _convoHasMore = data.hasMore;
      _renderConversation();
    }
  } catch (_) {}
}

// ── Scroll-Up Lazy Loading ─────────────────────────────────

function _setupScrollUpLoader() {
  const scrollEl = document.getElementById('zd-conversation');
  if (!scrollEl) return;
  scrollEl.addEventListener('scroll', async () => {
    if (scrollEl.scrollTop < 100 && _convoHasMore && !_convoLoadingMore) {
      _convoLoadingMore = true;
      const oldHeight = scrollEl.scrollHeight;
      _convoReadBytes += _CONVO_CHUNK_SIZE;
      _lastConvoCount = -1; // Force re-render
      await _loadConversation();
      // Preserve scroll position
      const newHeight = scrollEl.scrollHeight;
      scrollEl.scrollTop += (newHeight - oldHeight);
      _convoLoadingMore = false;
    }
  });
}

// ── Auto-Load Conversation on Replay Click ─────────────────

async function _loadConversationUntilMatch(replayEntry) {
  for (let attempt = 0; attempt < 10; attempt++) {
    if (!_convoHasMore) break;
    _convoReadBytes += _CONVO_CHUNK_SIZE;
    _lastConvoCount = -1;
    await _loadConversation();

    // Check if we now have a matching conversation entry by timestamp
    const reTime = replayEntry.timestamp ? new Date(replayEntry.timestamp).getTime() : 0;
    if (!reTime) break;
    for (let ci = 0; ci < _conversationEntries.length; ci++) {
      const ceTime = _conversationEntries[ci].timestamp ? new Date(_conversationEntries[ci].timestamp).getTime() : 0;
      if (ceTime && Math.abs(ceTime - reTime) < 10000) {
        // Found it — scroll to it
        const block = document.querySelector(`[data-conv-idx="${ci}"]`);
        if (block) {
          block.scrollIntoView({ behavior: 'smooth', block: 'center' });
          block.classList.add('zd-sync-highlight');
          setTimeout(() => block.classList.remove('zd-sync-highlight'), 500);
        }
        return;
      }
    }
  }
}

// ── Replay List ────────────────────────────────────────────

function _isZenrippleTool(name, args) {
  if (!name) return false;
  if (name.startsWith('browser_') || name.includes('zenripple')) return true;
  if (name === 'Bash' || name === 'bash') {
    const cmd = args?.command || (typeof args === 'string' ? args : '');
    return cmd.includes('zenripple');
  }
  return false;
}

function _toolSubtitle(tool, args) {
  if (!args || typeof args !== 'object') return '';
  if (tool === 'Bash' || tool === 'bash') return (args.command || '').replace(/^source [^;]+;\s*/, '').slice(0, 60);
  if (tool === 'Read' || tool === 'Write') return (args.file_path || '').split('/').pop();
  if (tool === 'Edit') return (args.file_path || '').split('/').pop();
  if (tool === 'Grep') return (args.pattern || '').slice(0, 40);
  if (tool === 'Glob') return (args.pattern || '').slice(0, 40);
  if (args.url) return args.url.replace(/^https?:\/\//, '').slice(0, 50);
  if (args.description) return (args.description || '').slice(0, 50);
  if (args.index != null) return 'index ' + args.index;
  if (args.text) return (args.text || '').slice(0, 40);
  for (const v of Object.values(args)) {
    if (typeof v === 'string' && v.length > 0 && v.length <= 60) return v.slice(0, 50);
  }
  return '';
}

function _renderReplayList() {
  const listEl = document.getElementById('zd-replay-entries');
  if (!listEl) return;
  listEl.innerHTML = '';

  for (let i = _replayEntries.length - 1; i >= 0; i--) {
    const entry = _replayEntries[i];
    const toolName = (entry.tool || '').replace(/^browser_/, '');
    const isZR = _isZenrippleTool(entry.tool, entry.args);
    const subtitle = _toolSubtitle(entry.tool, entry.args);

    const el = document.createElement('div');
    el.className = 'zd-replay-entry' + (_selectedReplayIdx === i ? ' selected' : '') + (isZR ? ' zr-tool' : '');
    el.dataset.idx = String(i);

    // Session color dot (merged view)
    if (entry._sourceColor) {
      const dot = document.createElement('span');
      dot.className = 'zd-replay-entry-session-dot';
      dot.style.background = 'rgb(' + entry._sourceColor + ')';
      el.appendChild(dot);
    }

    const seq = document.createElement('span');
    seq.className = 'zd-replay-entry-seq';
    seq.textContent = '#' + (entry.seq ?? i);
    el.appendChild(seq);

    const dot = document.createElement('span');
    dot.className = 'zd-replay-entry-dot' + (entry.error ? ' error' : '');
    el.appendChild(dot);

    // Two-line content column: name + subtitle
    const col = document.createElement('span');
    col.className = 'zd-replay-entry-col';
    const nameEl = document.createElement('span');
    nameEl.className = 'zd-replay-entry-name';
    nameEl.textContent = toolName;
    col.appendChild(nameEl);
    if (subtitle) {
      const subEl = document.createElement('span');
      subEl.className = 'zd-replay-entry-subtitle';
      subEl.textContent = subtitle;
      col.appendChild(subEl);
    }
    el.appendChild(col);

    // Time
    const timeEl = document.createElement('span');
    timeEl.className = 'zd-replay-entry-time';
    timeEl.textContent = extractTime(entry.timestamp);
    el.appendChild(timeEl);

    el.addEventListener('click', () => _selectReplayEntry(i, true));
    listEl.appendChild(el);
  }

  _updateTransport();
}

// scrollConversation: true = scroll conversation to matching block (user clicked replay)
//                     false = don't scroll conversation (called from scroll sync)
async function _selectReplayEntry(idx, scrollConversation = true) {
  if (idx < 0 || idx >= _replayEntries.length) return;
  _selectedReplayIdx = idx;
  const entry = _replayEntries[idx];

  // Update selection visuals + scroll into view
  for (const el of document.querySelectorAll('.zd-replay-entry')) {
    const isSelected = parseInt(el.dataset.idx) === idx;
    el.classList.toggle('selected', isSelected);
    if (isSelected) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  // Load screenshot
  const ssContainer = document.getElementById('zd-replay-ss');
  if (ssContainer && entry.screenshot) {
    const cacheKey = (entry._sourceReplayDir || entry._replayDir || '') + '/' + entry.screenshot;
    let url = _screenshotCache.get(cacheKey);
    if (!url) {
      try {
        const result = await bridgeCall('getScreenshot', { replayDir: entry._sourceReplayDir || entry._replayDir || '', filename: entry.screenshot });
        url = result?.url || null;
        if (url) {
          // Cap cache at 100 entries to prevent unbounded memory growth
          if (_screenshotCache.size >= 100) {
            const oldest = _screenshotCache.keys().next().value;
            _screenshotCache.delete(oldest);
          }
          _screenshotCache.set(cacheKey, url);
        }
      } catch (_) {}
    }
    if (url) {
      let img = ssContainer.querySelector('img');
      if (img) img.src = url;
      else { ssContainer.innerHTML = ''; img = document.createElement('img'); img.alt = 'Screenshot'; img.src = url; ssContainer.appendChild(img); }
    } else {
      ssContainer.innerHTML = '<span class="zd-no-screenshot">Screenshot unavailable</span>';
    }
  } else if (ssContainer) {
    ssContainer.innerHTML = '<span class="zd-no-screenshot">No screenshot</span>';
  }

  _updateTransport();
  _updateToolDetailView();

  // Sync: scroll conversation to matching tool block (only when user clicked replay)
  if (scrollConversation && entry.timestamp) {
    _pauseScrollSync();
    const reTime = new Date(entry.timestamp).getTime();
    let bestBlock = null, bestDist = Infinity;
    for (const block of document.querySelectorAll('.zd-tool-block[data-tool-ts]')) {
      const ts = block.dataset.toolTs;
      if (!ts) continue;
      const dist = Math.abs(new Date(ts).getTime() - reTime);
      if (dist < bestDist) { bestDist = dist; bestBlock = block; }
    }
    if (bestBlock && bestDist < 30000) {
      bestBlock.scrollIntoView({ behavior: 'smooth', block: 'center' });
      bestBlock.classList.add('zd-sync-highlight');
      setTimeout(() => bestBlock.classList.remove('zd-sync-highlight'), 500);
    } else if (_convoHasMore) {
      _loadConversationUntilMatch(entry);
    }
  }
}

// ── Tool Detail View ───────────────────────────────────────

function _updateToolDetailView() {
  const el = document.getElementById('zd-tool-detail');
  if (!el || el.style.display === 'none') return;
  if (_selectedReplayIdx < 0 || _selectedReplayIdx >= _replayEntries.length) {
    el.innerHTML = '<div class="zd-empty">Select a tool call</div>';
    return;
  }
  const entry = _replayEntries[_selectedReplayIdx];
  let html = `<div class="zd-detail-tool-name">${escapeHTML(entry.tool || '')}</div>`;
  html += '<div class="zd-detail-meta">';
  html += `<span class="zd-detail-meta-item">#${entry.seq ?? _selectedReplayIdx}</span>`;
  html += `<span class="zd-detail-meta-item">${escapeHTML(extractTime(entry.timestamp))}</span>`;
  if (entry.duration_ms != null) html += `<span class="zd-detail-meta-item">${Math.round(entry.duration_ms)}ms</span>`;
  if (entry.error) html += '<span class="zd-detail-meta-item error">ERROR</span>';
  html += '</div>';
  if (entry.args && Object.keys(entry.args).length) {
    html += '<div class="zd-detail-section-label">Arguments</div>';
    html += '<div class="zd-detail-json">' + syntaxHighlightJSON(formatJSON(entry.args)) + '</div>';
  }
  if (entry.result) {
    html += '<div class="zd-detail-section-label">Result</div>';
    const rs = typeof entry.result === 'string' ? entry.result : formatJSON(entry.result);
    html += '<div class="zd-detail-json">' + syntaxHighlightJSON(rs.slice(0, 5000)) + '</div>';
  }
  el.innerHTML = html;
}

// ── Conversation Rendering ─────────────────────────────────

function _renderConversation() {
  const scrollEl = document.getElementById('zd-conversation');
  if (!scrollEl) return;
  if (!_conversationEntries.length) { scrollEl.innerHTML = '<div class="zd-empty">No conversation linked.</div>'; return; }

  const wasAtBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 50;
  let html = '';
  if (_convoHasMore) {
    html += '<div class="zd-load-more">&#x2191; Scroll up to load earlier messages</div>';
  }

  for (let ci = 0; ci < _conversationEntries.length; ci++) {
    const entry = _conversationEntries[ci];
    const msg = entry.message;
    if (!msg) continue;
    const role = msg.role || entry.type;
    const content = msg.content;

    if (role === 'user') {
      if (typeof content === 'string' && content.trim() &&
          !content.includes('<system-reminder>') && !content.includes('<local-command-caveat>') && !content.includes('<command-name>')) {
        html += `<div class="zd-msg zd-msg-user" data-conv-idx="${ci}"><div class="zd-msg-label user-label">You</div><div class="zd-msg-content">${escapeHTML(content.slice(0,2000))}</div></div>`;
      }
    } else if (role === 'assistant' && Array.isArray(content)) {
      for (const block of content) {
        if (block.type === 'thinking') continue;
        if (block.type === 'text' && (block.text||'').trim()) {
          html += `<div class="zd-msg zd-msg-assistant" data-conv-idx="${ci}"><div class="zd-msg-label agent-label">Agent</div><div class="zd-msg-content">${escapeHTML(block.text.slice(0,2000))}</div></div>`;
        } else if (block.type === 'tool_use') {
          const name = block.name || '?';
          const isZR = name.startsWith('browser_') || name.includes('zenripple') || (name === 'Bash' && (block.input?.command||'').includes('zenripple'));
          const toolClass = isZR ? 'zd-zenripple-tool' : 'zd-other-tool';
          const inputStr = block.input ? (typeof block.input === 'string' ? block.input : JSON.stringify(block.input, null, 2)) : '';
          let inlineContent = '';
          if (name === 'Bash' && block.input?.command) {
            inlineContent = `<div class="zd-tool-json zd-bash-cmd" style="margin-top:4px"><span class="zd-bash-prompt">$</span> ${_highlightBashCmd(block.input.command.slice(0,500))}</div>`;
          } else if (name === 'Edit' && block.input?.file_path) {
            const file = block.input.file_path.split('/').pop();
            inlineContent = `<div class="zd-tool-json" style="margin-top:4px"><span class="zr-key">${escapeHTML(file)}</span></div>`;
          } else if (name === 'Read' && block.input?.file_path) {
            const file = block.input.file_path.split('/').pop();
            inlineContent = `<div class="zd-tool-json" style="margin-top:4px">${escapeHTML(file)}</div>`;
          }
          html += `<div class="zd-tool-block ${toolClass}" data-conv-idx="${ci}" data-tool-ts="${escapeHTML(entry.timestamp||'')}">
            <div class="zd-tool-header" data-toggle="tool">
              <span class="zd-tool-name">${escapeHTML(name)}</span>
              <span class="zd-tool-toggle">&#x25B6;</span>
            </div>
            ${inlineContent}
            <div class="zd-tool-body">
              ${inputStr ? '<div class="zd-tool-json">'+syntaxHighlightJSON(inputStr.slice(0,2000))+'</div>' : ''}
            </div>
          </div>`;
        }
      }
    } else if (role === 'assistant' && typeof content === 'string' && content.trim()) {
      html += `<div class="zd-msg zd-msg-assistant" data-conv-idx="${ci}"><div class="zd-msg-label agent-label">Agent</div><div class="zd-msg-content">${escapeHTML(content.slice(0,2000))}</div></div>`;
    }
  }

  scrollEl.innerHTML = html;

  // Wire expand/collapse on tool blocks
  for (const header of scrollEl.querySelectorAll('[data-toggle="tool"]')) {
    header.addEventListener('click', () => {
      const body = header.parentElement.querySelector('.zd-tool-body');
      const arrow = header.querySelector('.zd-tool-toggle');
      if (body) body.classList.toggle('expanded');
      if (arrow) arrow.classList.toggle('expanded');
    });
  }

  if (wasAtBottom) scrollEl.scrollTop = scrollEl.scrollHeight;
}

// ── Scroll Sync (conversation → replay) ────────────────────

function _pauseScrollSync() {
  _scrollSyncPaused = true;
  setTimeout(() => { _scrollSyncPaused = false; }, 800);
}

// Exact modal pattern: remove old listener, use RAF, scrollConversation: false
function _setupScrollSync() {
  const scrollEl = document.getElementById('zd-conversation');
  if (!scrollEl) return;

  // Remove previous listener to prevent accumulation across re-renders
  if (_scrollSyncListener) scrollEl.removeEventListener('scroll', _scrollSyncListener);

  _scrollSyncListener = () => {
    if (_scrollSyncPaused) return;
    if (_scrollSyncRafId) return; // Already scheduled
    _scrollSyncRafId = requestAnimationFrame(() => {
      _scrollSyncRafId = null;
      _doScrollSync(scrollEl);
    });
  };
  scrollEl.addEventListener('scroll', _scrollSyncListener);
}

function _doScrollSync(scrollEl) {
  if (_scrollSyncPaused) return;
  if (!_replayEntries.length) return;

  // Find the tool block closest to viewport BOTTOM (newest visible drives sync)
  const allBlocks = scrollEl.querySelectorAll('.zd-tool-block[data-conv-idx]');
  if (!allBlocks.length) return;

  const scrollRect = scrollEl.getBoundingClientRect();
  const viewBottom = scrollRect.bottom;

  let bestBlock = null, bestDist = Infinity;
  for (const block of allBlocks) {
    const rect = block.getBoundingClientRect();
    if (rect.bottom < scrollRect.top || rect.top > scrollRect.bottom) continue;
    const blockCenter = rect.top + rect.height / 2;
    const dist = Math.abs(blockCenter - viewBottom);
    if (dist < bestDist) { bestDist = dist; bestBlock = block; }
  }
  if (!bestBlock) return;

  const convIdx = parseInt(bestBlock.getAttribute('data-conv-idx'), 10);
  if (isNaN(convIdx) || convIdx >= _conversationEntries.length) return;

  const entry = _conversationEntries[convIdx];
  const entryTime = entry?.timestamp ? new Date(entry.timestamp).getTime() : 0;
  if (!entryTime) return;

  // Find closest replay entry by timestamp
  let bestReplayIdx = -1, bestReplayDist = Infinity;
  for (let ri = 0; ri < _replayEntries.length; ri++) {
    const reTime = _replayEntries[ri].timestamp ? new Date(_replayEntries[ri].timestamp).getTime() : 0;
    if (!reTime) continue;
    const dist = Math.abs(reTime - entryTime);
    if (dist < bestReplayDist) { bestReplayDist = dist; bestReplayIdx = ri; }
  }

  // Only sync within 30s and if different from current selection
  if (bestReplayIdx >= 0 && bestReplayDist < 30000 && bestReplayIdx !== _selectedReplayIdx) {
    _selectReplayEntry(bestReplayIdx, false); // false = don't scroll conversation back
  }
}

// ── Transport Controls ─────────────────────────────────────

function _setupTransportControls() {
  const play = document.getElementById('zd-play-btn');
  const slower = document.getElementById('zd-slower-btn');
  const faster = document.getElementById('zd-faster-btn');
  const progress = document.getElementById('zd-progress');

  if (play) play.addEventListener('click', _togglePlayback);
  if (slower) slower.addEventListener('click', () => _setSpeed(_playbackSpeedIdx - 1));
  if (faster) faster.addEventListener('click', () => _setSpeed(_playbackSpeedIdx + 1));
  if (progress) progress.addEventListener('click', (ev) => {
    const r = progress.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    _stopPlayback();
    _selectReplayEntry(Math.round(pct * (_replayEntries.length - 1)));
  });
}

function _togglePlayback() { _playbackPlaying ? _stopPlayback() : _startPlayback(); }

function _startPlayback() {
  _stopPlayback();
  _playbackPlaying = true;
  _updateTransport();
  _playbackTimer = setInterval(() => {
    if (!_replayEntries.length) { _stopPlayback(); return; }
    let next = _selectedReplayIdx + 1;
    if (next >= _replayEntries.length) next = 0;
    _selectReplayEntry(next);
  }, _PLAYBACK_BASE_MS / _PLAYBACK_SPEEDS[_playbackSpeedIdx]);
}

function _stopPlayback() {
  if (_playbackTimer) { clearInterval(_playbackTimer); _playbackTimer = null; }
  _playbackPlaying = false;
  _updateTransport();
}

function _setSpeed(idx) {
  _playbackSpeedIdx = Math.max(0, Math.min(_PLAYBACK_SPEEDS.length - 1, idx));
  _updateTransport();
  if (_playbackPlaying) _startPlayback();
}

function _updateTransport() {
  const play = document.getElementById('zd-play-btn');
  if (play) { play.textContent = _playbackPlaying ? '\u23F8' : '\u25B6'; play.classList.toggle('active', _playbackPlaying); }
  const speed = document.getElementById('zd-speed');
  if (speed) { const s = _PLAYBACK_SPEEDS[_playbackSpeedIdx]; speed.textContent = s+'x'; speed.classList.toggle('highlight', s!==1); }
  const fill = document.getElementById('zd-progress-fill');
  if (fill) fill.style.width = _replayEntries.length > 0 ? ((_selectedReplayIdx+1)/_replayEntries.length*100)+'%' : '0%';
  const count = document.getElementById('zd-count');
  if (count) count.textContent = _replayEntries.length + ' calls';
}

// ── Splitters ──────────────────────────────────────────────

const _SPLITTER_KEY = 'zenripple_dashboard_splitters';

function _loadSplitterState() {
  try { return JSON.parse(localStorage.getItem(_SPLITTER_KEY)) || {}; } catch (_) { return {}; }
}

function _saveSplitterState(state) {
  try { localStorage.setItem(_SPLITTER_KEY, JSON.stringify(state)); } catch (_) {}
}

function _setupSplitters() {
  const detail = document.querySelector('.zd-detail');
  if (!detail) return;
  const replayCol = document.getElementById('zd-replay-col');
  const rightCol = document.getElementById('zd-right-col');
  const ssPanel = document.getElementById('zd-replay-ss');
  let dragTarget = null;

  // Restore saved positions
  const saved = _loadSplitterState();
  const isNarrow = document.getElementById('zenripple-dashboard-container')?.classList.contains('zd-narrow');
  if (isNarrow) {
    if (saved.narrowReplayHeight && replayCol) {
      replayCol.style.setProperty('height', saved.narrowReplayHeight + 'px', 'important');
      replayCol.style.setProperty('flex', '0 0 ' + saved.narrowReplayHeight + 'px', 'important');
    }
    if (saved.narrowRightHeight && rightCol) {
      rightCol.style.height = saved.narrowRightHeight + 'px';
      rightCol.style.flex = '0 0 ' + saved.narrowRightHeight + 'px';
    }
    if (saved.narrowSsHeight && ssPanel) {
      ssPanel.style.flex = 'none';
      ssPanel.style.setProperty('height', saved.narrowSsHeight + 'px', 'important');
    }
  } else {
    if (saved.leftWidth && replayCol) replayCol.style.width = saved.leftWidth + 'px';
    if (saved.rightWidth && rightCol) rightCol.style.width = saved.rightWidth + 'px';
    if (saved.ssHeight && ssPanel) { ssPanel.style.flex = 'none'; ssPanel.style.height = saved.ssHeight + 'px'; }
  }

  for (const s of detail.querySelectorAll('.zd-splitter')) {
    s.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragTarget = e.currentTarget.dataset.split;
      e.currentTarget.classList.add('dragging');
      const narrow = document.getElementById('zenripple-dashboard-container')?.classList.contains('zd-narrow');
      document.body.style.cursor = (dragTarget === 'replay-inner' || narrow) ? 'row-resize' : 'col-resize';
      document.body.style.userSelect = 'none';
    });
  }

  document.addEventListener('mousemove', (e) => {
    if (!dragTarget) return;
    const rect = detail.getBoundingClientRect();
    const narrow = document.getElementById('zenripple-dashboard-container')?.classList.contains('zd-narrow');
    if (dragTarget === 'left' && replayCol) {
      if (narrow) {
        const h = Math.max(40, Math.min(rect.height * 0.8, e.clientY - rect.top));
        replayCol.style.setProperty('height', h + 'px', 'important');
        replayCol.style.setProperty('flex', '0 0 ' + h + 'px', 'important');
      } else {
        replayCol.style.width = Math.max(100, Math.min(rect.width*0.6, e.clientX - rect.left)) + 'px';
      }
    } else if (dragTarget === 'right' && rightCol) {
      if (narrow) {
        const h = Math.max(80, Math.min(rect.height * 0.5, rect.bottom - e.clientY));
        rightCol.style.height = h + 'px';
        rightCol.style.flex = '0 0 ' + h + 'px';
      } else {
        rightCol.style.width = Math.max(150, Math.min(rect.width*0.5, rect.right - e.clientX)) + 'px';
      }
    } else if (dragTarget === 'replay-inner' && ssPanel && replayCol) {
      const cr = replayCol.getBoundingClientRect();
      const h = Math.max(40, Math.min(cr.height - 60, e.clientY - cr.top));
      ssPanel.style.setProperty('flex', '0 0 ' + h + 'px', 'important');
      ssPanel.style.setProperty('height', h + 'px', 'important');
    }
  });

  document.addEventListener('mouseup', () => {
    if (!dragTarget) return;
    // Save positions on release — merge with existing state
    const state = _loadSplitterState();
    const narrowNow = document.getElementById('zenripple-dashboard-container')?.classList.contains('zd-narrow');
    if (narrowNow) {
      if (replayCol) state.narrowReplayHeight = Math.round(replayCol.getBoundingClientRect().height);
      if (rightCol) state.narrowRightHeight = Math.round(rightCol.getBoundingClientRect().height);
      if (ssPanel && ssPanel.style.height) state.narrowSsHeight = parseInt(ssPanel.style.height, 10);
    } else {
      if (replayCol) state.leftWidth = Math.round(replayCol.getBoundingClientRect().width);
      if (rightCol) state.rightWidth = Math.round(rightCol.getBoundingClientRect().width);
      if (ssPanel && ssPanel.style.height) state.ssHeight = parseInt(ssPanel.style.height, 10);
    }
    _saveSplitterState(state);

    for (const s of detail.querySelectorAll('.zd-splitter')) s.classList.remove('dragging');
    dragTarget = null;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
}

// ── Approvals & Messages ───────────────────────────────────

function _renderApprovals(entries) {
  const el = document.getElementById('zd-approvals');
  if (!el) return;
  const approvals = new Map();
  for (const e of entries) {
    if (e.status === 'pending') approvals.set(e.id, {...e, resolved: false});
    else if ((e.status === 'approved' || e.status === 'denied') && approvals.has(e.id)) {
      const a = approvals.get(e.id);
      a.resolved = true; a.resolution = e.status; a.resolution_message = e.message || '';
    }
  }
  if (!approvals.size) { el.innerHTML = '<div class="zd-empty">No approvals</div>'; return; }
  let html = '';
  for (const [id, a] of approvals) {
    if (a.resolved) {
      html += `<div class="zd-approval-card resolved"><div class="zd-approval-desc">${escapeHTML(a.description||id)}</div><div class="zd-approval-resolved">${escapeHTML(a.resolution==='approved'?'Approved':'Denied')}</div></div>`;
    } else {
      html += `<div class="zd-approval-card pending"><div class="zd-approval-desc">${escapeHTML(a.description||id)}</div><div class="zd-approval-actions"><div class="zd-approval-btn approve" data-id="${escapeHTML(id)}">Approve</div><div class="zd-approval-btn deny" data-id="${escapeHTML(id)}">Deny</div></div></div>`;
    }
  }
  el.innerHTML = html;
  for (const btn of el.querySelectorAll('.zd-approval-btn')) {
    btn.addEventListener('click', () => {
      const action = btn.classList.contains('approve') ? 'approved' : 'denied';
      bridgeCall('resolveApproval', { sessionId: _sessionId, approvalId: btn.dataset.id, status: action, message: '' });
    });
  }
}

function _renderMessages(entries) {
  const el = document.getElementById('zd-messages');
  if (!el) return;
  const messages = entries.filter(e => e.direction && !e.delivered);
  if (!messages.length) { el.innerHTML = '<div class="zd-empty">No messages yet</div>'; return; }
  let html = '';
  for (const m of messages) {
    const isAgent = m.direction === 'agent_to_human';
    html += `<div class="zd-chat-msg ${isAgent?'agent':'human'}"><div>${escapeHTML(m.text||'')}</div><div class="zd-chat-msg-time">${escapeHTML(extractTime(m.timestamp))}</div></div>`;
  }
  el.innerHTML = html;
  el.scrollTop = el.scrollHeight;
}

// ── Claude Status Polling ──────────────────────────────────

let _claudeStatusPollCount = 0;

async function _pollClaudeStatus() {
  if (_pageType === 'overview' || _pageType === 'merged') return;
  // Throttle: only poll every 3rd cycle (~6 seconds instead of ~2)
  if (++_claudeStatusPollCount % 3 !== 0) return;
  try {
    const result = await bridgeCall('getClaudeStatus', { sessionId: _sessionId });
    const statusEl = document.getElementById('zd-claude-status');
    const stopBtn = document.getElementById('zd-claude-stop');
    if (!statusEl || !stopBtn) return;

    if (result.status === 'fork_running') {
      statusEl.textContent = 'Fork running (PID ' + result.pid + ')';
      stopBtn.style.display = '';
    } else if (result.status === 'tmux_running') {
      statusEl.textContent = 'Running in tmux ' + (result.tmuxSession || result.tmuxPane || '');
      stopBtn.style.display = '';
    } else if (result.status === 'headless_running') {
      statusEl.textContent = 'Headless agent running (PID ' + result.pid + ')';
      stopBtn.style.display = '';
    } else {
      // idle or no_conversation — only clear if not showing "Stopped"
      if (statusEl.textContent !== 'Stopped') statusEl.textContent = '';
      stopBtn.style.display = 'none';
    }
  } catch (_) {}
}

// ── Keyboard Shortcuts ─────────────────────────────────────

document.addEventListener('keydown', (e) => {
  if (_pageType === 'overview') return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT' || e.target.isContentEditable) return;

  if (e.key === ' ') { e.preventDefault(); _togglePlayback(); }
  else if (e.key === '[') _setSpeed(_playbackSpeedIdx - 1);
  else if (e.key === ']') _setSpeed(_playbackSpeedIdx + 1);
  else if (e.key === 'j' || e.key === 'ArrowDown') { _stopPlayback(); if (_replayEntries.length) _selectReplayEntry(Math.max(0, _selectedReplayIdx - 1)); }
  else if (e.key === 'k' || e.key === 'ArrowUp') { _stopPlayback(); if (_replayEntries.length) _selectReplayEntry(Math.min(_replayEntries.length-1, _selectedReplayIdx + 1)); }
  else if (e.key === 'g') { _stopPlayback(); if (_replayEntries.length) _selectReplayEntry(0); }
  else if (e.key === 'G') { _stopPlayback(); if (_replayEntries.length) _selectReplayEntry(_replayEntries.length-1); }
});

// ── Polling ────────────────────────────────────────────────

function startPolling() {
  stopPolling();
  _pollTimer = setInterval(async () => {
    if (document.hidden) return;
    if (_tmuxSession) return; // Terminal view has its own polling (no conversation to load)
    if (_pageType === 'overview') await refreshOverview();
    else await _loadSessionData();
  }, _POLL_MS);
}

function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  _stopTmuxPolling();
  _stopHeadlessPolling();
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
  } else {
    startPolling();
    if (_tmuxSession) _startTmuxPolling();
    if (_headlessName) _startHeadlessPolling();
  }
});

// ── Init ───────────────────────────────────────────────────

async function _init() {
  console.log('[ZenRipple] Dashboard init, type=' + _pageType);
  if (_pageType === 'overview') await refreshOverview();
  else await initSessionDetail();
  startPolling();
}

_connectWebSocket();

// ── Responsive: toggle .zd-narrow class based on container width ──
(() => {
  const container = document.getElementById('zenripple-dashboard-container');
  if (!container) return;
  const obs = new ResizeObserver((entries) => {
    for (const entry of entries) {
      entry.target.classList.toggle('zd-narrow', entry.contentRect.width < 900);
    }
  });
  obs.observe(container);
})();
