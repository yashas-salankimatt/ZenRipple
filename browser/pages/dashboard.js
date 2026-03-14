/* === ZenRipple Dashboard Client === */
/* Runs in content process. Communicates with chrome via postMessage bridge. */

'use strict';

// ── Bridge Client ──────────────────────────────────────────

let _bridgeReady = false;

function bridgeCall(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (typeof window.__zenrippleBridge !== 'function') {
      reject(new Error('Bridge not ready'));
      return;
    }
    try {
      window.__zenrippleBridge(method, JSON.stringify(params), function(responseStr) {
        try {
          const { result, error } = JSON.parse(responseStr);
          if (error) reject(new Error(error));
          else resolve(result);
        } catch (e) {
          reject(new Error('Bridge parse error: ' + e));
        }
      });
    } catch (e) {
      reject(new Error('Bridge call error: ' + e));
    }
  });
}

// Listen for bridge-ready event from chrome
window.addEventListener('zenripple-bridge-ready', () => {
  if (_bridgeReady) return;
  _bridgeReady = true;
  console.log('[ZenRipple] Bridge connected via Cu.exportFunction');
  _init();
});

// ── Utility Functions ──────────────────────────────────────

function escapeHTML(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function extractTime(timestamp) {
  if (!timestamp) return '';
  try {
    const d = new Date(timestamp);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (_) { return ''; }
}

function formatDuration(totalSeconds) {
  if (totalSeconds < 60) return totalSeconds + 's';
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return minutes + 'm ' + seconds + 's';
  const hours = Math.floor(minutes / 60);
  return hours + 'h ' + (minutes % 60) + 'm';
}

function timeAgo(isoTimestamp) {
  if (!isoTimestamp) return '';
  try {
    const secs = Math.round((Date.now() - new Date(isoTimestamp).getTime()) / 1000);
    if (secs < 5) return 'just now';
    if (secs < 60) return secs + 's ago';
    const mins = Math.round(secs / 60);
    if (mins < 60) return mins + 'm ago';
    const hours = Math.round(mins / 60);
    if (hours < 24) return hours + 'h ago';
    return Math.round(hours / 24) + 'd ago';
  } catch (_) { return ''; }
}

// ── Determine Page Type ────────────────────────────────────

const _params = new URLSearchParams(window.location.search);
const _pageType = _params.has('id') ? 'session' :
                  _params.has('merged') ? 'merged' : 'overview';
const _sessionId = _params.get('id') || '';
const _mergedConvoPath = _params.get('merged') || '';

// ── State ──────────────────────────────────────────────────

let _replayEntries = [];
let _conversationEntries = [];
let _selectedReplayIdx = -1;
let _pollTimer = null;
const _POLL_MS = 2000;
let _screenshotCache = new Map();

// ── Overview Rendering ─────────────────────────────────────

function statusLabel(status) {
  switch (status) {
    case 'active': return 'Active';
    case 'thinking': return 'Thinking';
    case 'approval': return 'Waiting for Approval';
    case 'idle': return 'Idle';
    case 'ended': return 'Ended';
    default: return status;
  }
}

function statusClass(status) {
  return 'status-' + status;
}

function buildCardHTML(card) {
  const cardClasses = ['zd-card'];
  if (card.status === 'active') cardClasses.push('zd-card-active');
  if (card.hasPendingApproval) cardClasses.push('zd-card-approval');
  const durationStr = card.duration > 0 ? formatDuration(card.duration) : '';

  return `<div class="${cardClasses.join(' ')}" data-session-id="${escapeHTML(card.sessionId)}" style="--card-sh: ${card.color}">
    <div class="zd-card-header">
      <span class="zd-card-dot" style="background: rgb(${card.color})"></span>
      <span class="zd-card-name">${escapeHTML(card.name)}</span>
      <span class="zd-card-status ${statusClass(card.status)}">${escapeHTML(statusLabel(card.status))}</span>
    </div>
    ${card.lastAction ? `<span class="zd-card-last-action">Last: ${escapeHTML(card.lastAction.slice(0, 60))}</span>` : ''}
    <div class="zd-card-stats">
      <span>${card.toolCount} tool call${card.toolCount !== 1 ? 's' : ''}</span>
      ${durationStr ? `<span>${escapeHTML(durationStr)}</span>` : ''}
    </div>
    ${card.hasPendingApproval ? '<div class="zd-card-approval-badge">&#x26A0; Pending approval</div>' : ''}
  </div>`;
}

function buildOverviewHTML(cards) {
  if (cards.length === 0) return '<div class="zd-empty">No agent sessions found.</div>';

  const convoGroups = new Map();
  const ungrouped = [];

  for (const card of cards) {
    if (card.conversationPath) {
      if (!convoGroups.has(card.conversationPath)) {
        const parts = card.conversationPath.split('/');
        const projectDir = parts[parts.length - 2] || '';
        const projectParts = projectDir.split('-').filter(Boolean);
        const convoName = projectParts.length > 0 ? projectParts[projectParts.length - 1] : 'Project';
        const convoId = (parts[parts.length - 1] || '').replace('.jsonl', '').slice(0, 8);
        convoGroups.set(card.conversationPath, { name: convoName, convoId, cards: [], hasActive: false });
      }
      const group = convoGroups.get(card.conversationPath);
      group.cards.push(card);
      if (card.status !== 'ended') group.hasActive = true;
    } else {
      ungrouped.push(card);
    }
  }

  let html = '';
  const sortedGroups = [...convoGroups.entries()].sort((a, b) => {
    if (a[1].hasActive !== b[1].hasActive) return a[1].hasActive ? -1 : 1;
    const aMax = Math.max(0, ...a[1].cards.map(c => c.lastActivity || 0));
    const bMax = Math.max(0, ...b[1].cards.map(c => c.lastActivity || 0));
    return bMax - aMax;
  });

  for (const [convoPath, group] of sortedGroups) {
    html += '<div class="zd-convo-group">';
    html += '<div class="zd-convo-group-header">';
    html += '<span class="zd-convo-group-name">' + escapeHTML(group.name) + '</span>';
    html += '<span class="zd-convo-group-id">' + escapeHTML(group.convoId) + '</span>';
    html += '<span class="zd-convo-group-count">' + group.cards.length + ' session' + (group.cards.length !== 1 ? 's' : '') + '</span>';
    if (group.cards.length > 1) {
      html += '<div class="zd-convo-group-viewall" data-convo-path="' + escapeHTML(convoPath) + '">View All</div>';
    }
    html += '</div>';
    html += '<div class="zd-cards">';
    for (const card of group.cards) html += buildCardHTML(card);
    html += '</div></div>';
  }

  if (ungrouped.length > 0) {
    html += '<div class="zd-section-label">Unlinked Sessions</div>';
    html += '<div class="zd-cards">';
    for (const card of ungrouped) html += buildCardHTML(card);
    html += '</div>';
  }

  return html;
}

async function refreshOverview() {
  try {
    const cards = await bridgeCall('getSessionCards');
    const overviewEl = document.getElementById('zd-overview');
    if (!overviewEl) return;

    overviewEl.innerHTML = buildOverviewHTML(cards);

    // Card click → open session tab
    for (const cardEl of overviewEl.querySelectorAll('.zd-card')) {
      cardEl.addEventListener('click', () => {
        const sid = cardEl.dataset.sessionId;
        if (sid) bridgeCall('openSessionTab', { sessionId: sid });
      });
    }

    // "View All" click → open merged tab
    for (const btn of overviewEl.querySelectorAll('.zd-convo-group-viewall')) {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        bridgeCall('openMergedTab', { convoPath: btn.dataset.convoPath });
      });
    }

    // Update footer
    const footer = document.getElementById('zd-footer');
    if (footer) {
      const active = cards.filter(c => c.status !== 'ended').length;
      const pending = cards.filter(c => c.hasPendingApproval).length;
      footer.innerHTML = `<span class="zd-footer-item">${active} active session${active !== 1 ? 's' : ''}</span>` +
        (pending > 0 ? `<span class="zd-footer-item" style="color:var(--zd-status-approval)">${pending} approval${pending !== 1 ? 's' : ''} pending</span>` : '');
    }
  } catch (e) {
    console.error('[ZenRipple] Overview refresh failed:', e);
  }
}

// ── Session Detail Rendering ───────────────────────────────

async function initSessionDetail(sessionId, isMerged, mergedConvoPath) {
  const title = document.getElementById('zd-title');
  const badge = document.getElementById('zd-badge');
  const body = document.getElementById('zd-body');
  const back = document.getElementById('zd-back');

  if (back) {
    back.addEventListener('click', () => bridgeCall('openDashboardTab'));
  }

  if (!body) return;

  // Get session info
  let sessionInfo = null;
  try {
    if (isMerged) {
      sessionInfo = await bridgeCall('getMergedSessionInfo', { convoPath: mergedConvoPath });
    } else {
      sessionInfo = await bridgeCall('getSessionInfo', { sessionId });
    }
  } catch (_) {}

  if (title) title.textContent = sessionInfo?.name || sessionId?.slice(0, 12) || 'Session';
  if (badge) badge.textContent = sessionInfo?.status || '';

  body.innerHTML = `
    <div class="zd-detail">
      <div class="zd-replay-col" id="zd-replay-col">
        <div class="zd-replay-screenshot" id="zd-replay-ss">
          <span class="zd-no-screenshot">No screenshot</span>
        </div>
        <div class="zd-replay-transport" id="zd-transport">
          <div class="zd-transport-btn" id="zd-play-btn" title="Play (Space)">&#x25B6;</div>
          <div class="zd-transport-btn" id="zd-slower-btn" title="Slower ([)">&#x2BC7;</div>
          <span class="zd-transport-speed" id="zd-speed">1x</span>
          <div class="zd-transport-btn" id="zd-faster-btn" title="Faster (])">&#x2BC8;</div>
          <div class="zd-transport-progress" id="zd-progress">
            <div class="zd-transport-progress-fill" id="zd-progress-fill"></div>
          </div>
          <span class="zd-transport-count" id="zd-count">0</span>
        </div>
        <div class="zd-replay-list" id="zd-replay-entries"></div>
      </div>
      <div class="zd-conversation-col" id="zd-conversation-col">
        <div class="zd-conversation-scroll" id="zd-conversation">
          <div class="zd-empty">Loading conversation...</div>
        </div>
        <div class="zd-claude-input-wrapper">
          <input class="zd-claude-input" id="zd-claude-input" type="text" placeholder="Send to Claude Code...">
          <div class="zd-claude-send" id="zd-claude-send">&#x2192;</div>
          <div class="zd-claude-stop" id="zd-claude-stop" style="display:none" title="Stop">&#x25A0;</div>
        </div>
      </div>
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

  // Wire send buttons
  const claudeInput = document.getElementById('zd-claude-input');
  const claudeSend = document.getElementById('zd-claude-send');
  if (claudeInput && claudeSend) {
    claudeSend.addEventListener('click', () => _sendToClaudeCode(sessionId, claudeInput));
    claudeInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') _sendToClaudeCode(sessionId, claudeInput);
    });
  }

  const msgInput = document.getElementById('zd-msg-input');
  const msgSend = document.getElementById('zd-msg-send');
  if (msgInput && msgSend) {
    msgSend.addEventListener('click', () => _sendHumanMessage(sessionId, msgInput));
    msgInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') _sendHumanMessage(sessionId, msgInput);
    });
  }

  const stopBtn = document.getElementById('zd-claude-stop');
  if (stopBtn) {
    stopBtn.addEventListener('click', () => bridgeCall('stopClaude', { sessionId }));
  }

  // Load initial data
  await _loadSessionData(sessionId, isMerged, mergedConvoPath);
}

async function _sendToClaudeCode(sessionId, inputEl) {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  try {
    await bridgeCall('sendToClaudeCode', { sessionId, text });
  } catch (e) {
    console.error('[ZenRipple] Send to Claude failed:', e);
  }
}

async function _sendHumanMessage(sessionId, inputEl) {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  try {
    await bridgeCall('sendHumanMessage', { sessionId, text });
  } catch (e) {
    console.error('[ZenRipple] Send message failed:', e);
  }
}

async function _loadSessionData(sessionId, isMerged, mergedConvoPath) {
  try {
    const method = isMerged ? 'getMergedSessionData' : 'getSessionData';
    const params = isMerged ? { convoPath: mergedConvoPath } : { sessionId };
    const data = await bridgeCall(method, params);

    if (data.replayEntries) {
      _replayEntries = data.replayEntries;
      _renderReplayList();
    }
    if (data.conversationEntries) {
      _conversationEntries = data.conversationEntries;
      _renderConversation();
    }
    if (data.approvals) _renderApprovals(data.approvals, sessionId);
    if (data.messages) _renderMessages(data.messages);

    // Update footer
    const footer = document.getElementById('zd-footer');
    if (footer) {
      footer.innerHTML = `<span class="zd-footer-item">${_replayEntries.length} calls</span>` +
        `<span class="zd-footer-item">${data.status || 'Unknown'}</span>`;
    }
  } catch (e) {
    console.error('[ZenRipple] Load session data failed:', e);
  }
}

// ── Replay List ────────────────────────────────────────────

function _renderReplayList() {
  const listEl = document.getElementById('zd-replay-entries');
  if (!listEl) return;
  listEl.innerHTML = '';

  for (let i = _replayEntries.length - 1; i >= 0; i--) {
    const entry = _replayEntries[i];
    const el = document.createElement('div');
    el.className = 'zd-replay-entry';
    el.dataset.idx = String(i);

    if (entry._sourceColor) {
      const dot = document.createElement('span');
      dot.className = 'zd-replay-entry-session-dot';
      dot.style.background = 'rgb(' + entry._sourceColor + ')';
      dot.title = entry._sourceName || '';
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

  if (_replayEntries.length > 0) {
    _selectReplayEntry(_replayEntries.length - 1);
  }

  // Update count
  const countEl = document.getElementById('zd-count');
  if (countEl) countEl.textContent = _replayEntries.length + ' calls';
}

async function _selectReplayEntry(idx) {
  if (idx < 0 || idx >= _replayEntries.length) return;
  _selectedReplayIdx = idx;
  const entry = _replayEntries[idx];

  // Update selection
  for (const el of document.querySelectorAll('.zd-replay-entry')) {
    const isSelected = parseInt(el.dataset.idx, 10) === idx;
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
        const result = await bridgeCall('getScreenshot', {
          replayDir: entry._sourceReplayDir || entry._replayDir || '',
          filename: entry.screenshot,
        });
        url = result?.url || null;
        if (url) _screenshotCache.set(cacheKey, url);
      } catch (_) {}
    }
    if (url) {
      let img = ssContainer.querySelector('img');
      if (img) { img.src = url; }
      else {
        ssContainer.innerHTML = '';
        img = document.createElement('img');
        img.alt = 'Screenshot';
        img.src = url;
        ssContainer.appendChild(img);
      }
    } else {
      ssContainer.innerHTML = '<span class="zd-no-screenshot">Screenshot unavailable</span>';
    }
  } else if (ssContainer) {
    ssContainer.innerHTML = '<span class="zd-no-screenshot">No screenshot</span>';
  }

  // Update progress bar
  const fill = document.getElementById('zd-progress-fill');
  if (fill && _replayEntries.length > 0) {
    fill.style.width = ((idx + 1) / _replayEntries.length * 100) + '%';
  }
}

// ── Conversation Rendering ─────────────────────────────────

function _renderConversation() {
  const scrollEl = document.getElementById('zd-conversation');
  if (!scrollEl) return;

  if (_conversationEntries.length === 0) {
    scrollEl.innerHTML = '<div class="zd-empty">No conversation linked.</div>';
    return;
  }

  let html = '';
  for (const entry of _conversationEntries) {
    const msg = entry.message;
    if (!msg) continue;
    const role = msg.role || entry.type;
    const content = msg.content;

    if (role === 'user') {
      if (typeof content === 'string' && content.trim() &&
          !content.includes('<system-reminder>') && !content.includes('<local-command-caveat>')) {
        html += `<div class="zd-msg zd-msg-user">
          <div class="zd-msg-label user-label">You</div>
          <div class="zd-msg-content">${escapeHTML(content).slice(0, 2000)}</div>
        </div>`;
      }
    } else if (role === 'assistant') {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block.type === 'thinking') continue;
          if (block.type === 'text' && (block.text || '').trim()) {
            html += `<div class="zd-msg zd-msg-assistant">
              <div class="zd-msg-label agent-label">Agent</div>
              <div class="zd-msg-content">${escapeHTML(block.text).slice(0, 2000)}</div>
            </div>`;
          } else if (block.type === 'tool_use') {
            const name = block.name || '?';
            const isZR = name.startsWith('browser_') || name.includes('zenripple');
            const toolClass = isZR ? 'zd-zenripple-tool' : 'zd-other-tool';
            html += `<div class="zd-tool-block ${toolClass}">
              <div class="zd-tool-header">
                <span class="zd-tool-name">${escapeHTML(name)}</span>
              </div>
            </div>`;
          }
        }
      } else if (typeof content === 'string' && content.trim()) {
        html += `<div class="zd-msg zd-msg-assistant">
          <div class="zd-msg-label agent-label">Agent</div>
          <div class="zd-msg-content">${escapeHTML(content).slice(0, 2000)}</div>
        </div>`;
      }
    }
  }

  scrollEl.innerHTML = html;
  scrollEl.scrollTop = scrollEl.scrollHeight;
}

// ── Approvals & Messages ───────────────────────────────────

function _renderApprovals(entries, sessionId) {
  const el = document.getElementById('zd-approvals');
  if (!el) return;

  const approvals = new Map();
  for (const entry of entries) {
    if (entry.status === 'pending') {
      approvals.set(entry.id, { ...entry, resolved: false });
    } else if (entry.status === 'approved' || entry.status === 'denied') {
      const existing = approvals.get(entry.id);
      if (existing) {
        existing.resolved = true;
        existing.resolution = entry.status;
        existing.resolution_message = entry.message || '';
      }
    }
  }

  if (approvals.size === 0) {
    el.innerHTML = '<div class="zd-empty">No approvals</div>';
    return;
  }

  let html = '';
  for (const [id, approval] of approvals) {
    if (approval.resolved) {
      html += `<div class="zd-approval-card resolved">
        <div class="zd-approval-desc">${escapeHTML(approval.description || id)}</div>
        <div class="zd-approval-resolved">${escapeHTML(approval.resolution === 'approved' ? 'Approved' : 'Denied')}</div>
      </div>`;
    } else {
      html += `<div class="zd-approval-card pending">
        <div class="zd-approval-desc">${escapeHTML(approval.description || id)}</div>
        <div class="zd-approval-actions">
          <div class="zd-approval-btn approve" data-id="${escapeHTML(id)}">Approve</div>
          <div class="zd-approval-btn deny" data-id="${escapeHTML(id)}">Deny</div>
        </div>
      </div>`;
    }
  }
  el.innerHTML = html;

  // Wire buttons
  for (const btn of el.querySelectorAll('.zd-approval-btn')) {
    btn.addEventListener('click', () => {
      const action = btn.classList.contains('approve') ? 'approved' : 'denied';
      bridgeCall('resolveApproval', { sessionId, approvalId: btn.dataset.id, status: action, message: '' });
    });
  }
}

function _renderMessages(entries) {
  const el = document.getElementById('zd-messages');
  if (!el) return;

  const messages = entries.filter(e => e.direction && !e.delivered);
  if (messages.length === 0) {
    el.innerHTML = '<div class="zd-empty">No messages yet</div>';
    return;
  }

  let html = '';
  for (const msg of messages) {
    const isAgent = msg.direction === 'agent_to_human';
    html += `<div class="zd-chat-msg ${isAgent ? 'agent' : 'human'}">
      <div>${escapeHTML(msg.text || '')}</div>
      <div class="zd-chat-msg-time">${escapeHTML(extractTime(msg.timestamp))}</div>
    </div>`;
  }
  el.innerHTML = html;
  el.scrollTop = el.scrollHeight;
}

// ── Polling ────────────────────────────────────────────────

function startPolling() {
  stopPolling();
  _pollTimer = setInterval(async () => {
    if (document.hidden) return; // Don't poll hidden tabs
    if (_pageType === 'overview') {
      await refreshOverview();
    } else {
      await _loadSessionData(_sessionId, _pageType === 'merged', _mergedConvoPath);
    }
  }, _POLL_MS);
}

function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopPolling();
  else startPolling();
});

// ── Initialization ─────────────────────────────────────────

async function _init() {
  console.log('[ZenRipple] Dashboard page init, type=' + _pageType);

  if (_pageType === 'overview') {
    await refreshOverview();
  } else if (_pageType === 'session') {
    await initSessionDetail(_sessionId, false, '');
  } else if (_pageType === 'merged') {
    await initSessionDetail('', true, _mergedConvoPath);
  }

  startPolling();
}

// If bridge is already available (exported before page JS loaded), init immediately
// Otherwise wait for the 'zenripple-bridge-ready' CustomEvent
function _tryConnect() {
  if (_bridgeReady) return;
  if (typeof window.__zenrippleBridge === 'function') {
    _bridgeReady = true;
    console.log('[ZenRipple] Bridge found (poll)');
    _init();
    return;
  }
  console.log('[ZenRipple] Waiting for bridge...');
}
// Poll a few times in case the event was missed
setTimeout(_tryConnect, 300);
setTimeout(_tryConnect, 1000);
setTimeout(_tryConnect, 3000);
setTimeout(_tryConnect, 5000);
