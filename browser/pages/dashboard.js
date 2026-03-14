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

// ── Page Type Detection ────────────────────────────────────

const _params = new URLSearchParams(window.location.search);
const _pageType = _params.has('id') ? 'session' : _params.has('merged') ? 'merged' : 'overview';
const _sessionId = _params.get('id') || '';
const _mergedConvoPath = _params.get('merged') || '';

// ── State ──────────────────────────────────────────────────

let _replayEntries = [];
let _conversationEntries = [];
let _selectedReplayIdx = -1;
let _pollTimer = null;
let _lastReplayCount = -1; // Track to avoid re-render on unchanged data
let _lastConvoCount = -1;
let _screenshotCache = new Map();
let _scrollSyncRafId = null;
let _scrollSyncPaused = false;
const _POLL_MS = 2000;

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
  if (!cards.length) return '<div class="zd-empty">No agent sessions found.</div>';
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
  let html = '';
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
    const footer = document.getElementById('zd-footer');
    if (footer) {
      const active = cards.filter(c=>c.status!=='ended').length;
      footer.innerHTML = `<span class="zd-footer-item">${active} active</span>`;
    }
  } catch (e) { console.error('[ZenRipple] Overview error:', e); }
}

// ── Session Detail ─────────────────────────────────────────

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

  const title = document.getElementById('zd-title');
  const badge = document.getElementById('zd-badge');
  if (title) title.textContent = info?.name || _sessionId?.slice(0,12) || 'Session';
  if (badge) badge.textContent = info?.status || '';

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
        <div class="zd-claude-input-wrapper">
          <input class="zd-claude-input" id="zd-claude-input" type="text" placeholder="Send to Claude Code...">
          <div class="zd-claude-send" id="zd-claude-send">&#x2192;</div>
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

  // Wire inputs
  _wireInput('zd-claude-input', 'zd-claude-send', (text) => bridgeCall('sendToClaudeCode', { sessionId: _sessionId, text }));
  _wireInput('zd-msg-input', 'zd-msg-send', (text) => bridgeCall('sendHumanMessage', { sessionId: _sessionId, text }));

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
  await _loadSessionData();

  // Scroll sync
  _setupScrollSync();
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
      _replayEntries = data.replayEntries;
      _lastReplayCount = data.replayEntries.length;
      _renderReplayList(false); // false = don't auto-select latest
    }
    // Only re-render conversation if data changed
    if (data.conversationEntries && data.conversationEntries.length !== _lastConvoCount) {
      _conversationEntries = data.conversationEntries;
      _lastConvoCount = data.conversationEntries.length;
      _renderConversation();
    }
    if (data.approvals) _renderApprovals(data.approvals);
    if (data.messages) _renderMessages(data.messages);

    const footer = document.getElementById('zd-footer');
    if (footer) footer.innerHTML = `<span class="zd-footer-item">${_replayEntries.length} calls</span><span class="zd-footer-item">${escapeHTML(data.status||'')}</span>`;
  } catch (e) { console.error('[ZenRipple] Load error:', e); }
}

// ── Replay List ────────────────────────────────────────────

function _renderReplayList(autoSelectLatest = true) {
  const listEl = document.getElementById('zd-replay-entries');
  if (!listEl) return;
  listEl.innerHTML = '';

  for (let i = _replayEntries.length - 1; i >= 0; i--) {
    const entry = _replayEntries[i];
    const el = document.createElement('div');
    el.className = 'zd-replay-entry' + (_selectedReplayIdx === i ? ' selected' : '');
    el.dataset.idx = String(i);

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

    const name = document.createElement('span');
    name.className = 'zd-replay-entry-name';
    name.textContent = (entry.tool || '').replace(/^browser_/, '');
    el.appendChild(name);

    el.addEventListener('click', () => _selectReplayEntry(i));
    listEl.appendChild(el);
  }

  // Auto-select latest only on first load
  if (autoSelectLatest && _selectedReplayIdx < 0 && _replayEntries.length > 0) {
    _selectReplayEntry(_replayEntries.length - 1);
  }

  _updateTransport();
}

async function _selectReplayEntry(idx) {
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
        if (url) _screenshotCache.set(cacheKey, url);
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
            inlineContent = `<div class="zd-tool-json" style="margin-top:4px">$ ${escapeHTML(block.input.command.slice(0,500))}</div>`;
          } else if (name === 'Edit' && block.input?.file_path) {
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

function _setupScrollSync() {
  const scrollEl = document.getElementById('zd-conversation');
  if (!scrollEl) return;
  scrollEl.addEventListener('scroll', () => {
    if (_scrollSyncPaused || _scrollSyncRafId) return;
    _scrollSyncRafId = requestAnimationFrame(() => {
      _scrollSyncRafId = null;
      _doScrollSync(scrollEl);
    });
  });
}

function _doScrollSync(scrollEl) {
  if (_scrollSyncPaused || !_replayEntries.length) return;
  const allBlocks = scrollEl.querySelectorAll('.zd-tool-block[data-conv-idx]');
  if (!allBlocks.length) return;

  const rect = scrollEl.getBoundingClientRect();
  const viewBottom = rect.bottom;
  let bestBlock = null, bestDist = Infinity;

  for (const block of allBlocks) {
    const br = block.getBoundingClientRect();
    if (br.bottom < rect.top || br.top > rect.bottom) continue;
    const dist = Math.abs(br.top + br.height/2 - viewBottom);
    if (dist < bestDist) { bestDist = dist; bestBlock = block; }
  }

  if (!bestBlock) return;
  const convIdx = parseInt(bestBlock.dataset.convIdx);
  if (isNaN(convIdx) || convIdx >= _conversationEntries.length) return;

  const entryTime = _conversationEntries[convIdx]?.timestamp ? new Date(_conversationEntries[convIdx].timestamp).getTime() : 0;
  if (!entryTime) return;

  let bestIdx = -1, bestTimeDist = Infinity;
  for (let ri = 0; ri < _replayEntries.length; ri++) {
    const rt = _replayEntries[ri].timestamp ? new Date(_replayEntries[ri].timestamp).getTime() : 0;
    if (!rt) continue;
    const d = Math.abs(rt - entryTime);
    if (d < bestTimeDist) { bestTimeDist = d; bestIdx = ri; }
  }

  if (bestIdx >= 0 && bestTimeDist < 30000 && bestIdx !== _selectedReplayIdx) {
    _selectReplayEntry(bestIdx);
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

function _setupSplitters() {
  const detail = document.querySelector('.zd-detail');
  if (!detail) return;
  const replayCol = document.getElementById('zd-replay-col');
  const rightCol = document.getElementById('zd-right-col');
  const ssPanel = document.getElementById('zd-replay-ss');
  let dragTarget = null;

  for (const s of detail.querySelectorAll('.zd-splitter')) {
    s.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragTarget = e.currentTarget.dataset.split;
      e.currentTarget.classList.add('dragging');
      document.body.style.cursor = dragTarget === 'replay-inner' ? 'row-resize' : 'col-resize';
      document.body.style.userSelect = 'none';
    });
  }

  document.addEventListener('mousemove', (e) => {
    if (!dragTarget) return;
    const rect = detail.getBoundingClientRect();
    if (dragTarget === 'left' && replayCol) {
      replayCol.style.width = Math.max(100, Math.min(rect.width*0.6, e.clientX - rect.left)) + 'px';
    } else if (dragTarget === 'right' && rightCol) {
      rightCol.style.width = Math.max(150, Math.min(rect.width*0.5, rect.right - e.clientX)) + 'px';
    } else if (dragTarget === 'replay-inner' && ssPanel && replayCol) {
      const cr = replayCol.getBoundingClientRect();
      ssPanel.style.flex = 'none';
      ssPanel.style.height = Math.max(60, Math.min(cr.height-80, e.clientY - cr.top)) + 'px';
    }
  });

  document.addEventListener('mouseup', () => {
    if (!dragTarget) return;
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

// ── Keyboard Shortcuts ─────────────────────────────────────

document.addEventListener('keydown', (e) => {
  if (_pageType === 'overview') return;
  if (e.target.tagName === 'INPUT' || e.target.isContentEditable) return;

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
    if (_pageType === 'overview') await refreshOverview();
    else await _loadSessionData();
  }, _POLL_MS);
}

function stopPolling() { if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; } }

document.addEventListener('visibilitychange', () => { document.hidden ? stopPolling() : startPolling(); });

// ── Init ───────────────────────────────────────────────────

async function _init() {
  console.log('[ZenRipple] Dashboard init, type=' + _pageType);
  if (_pageType === 'overview') await refreshOverview();
  else await initSessionDetail();
  startPolling();
}

_connectWebSocket();
