// ==UserScript==
// @name           ZenRipple - Browser Automation for Claude Code
// @description    WebSocket server exposing browser control via MCP for AI agents
// @include        main
// @author         ZenRipple
// @version        1.0.0
// ==/UserScript==

(function() {
  'use strict';

  const VERSION = '1.0.0';
  const AGENT_PORT = 9876;
  const WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
  const AGENT_WORKSPACE_NAME = 'ZenRipple';
  const AGENT_WORKSPACE_ICON = '\u{1F916}'; // 🤖

  const logBuffer = [];
  const MAX_LOG_LINES = 200;
  const MAX_SESSION_TABS = 40;
  const MAX_INTERCEPT_RULES = 100;
  const MAX_CLIENT_FRAME_BUFFER = 20 * 1024 * 1024; // Incoming command payloads from MCP client
  const MAX_UPLOAD_SIZE = 8 * 1024 * 1024; // 8MB file input cap
  const MAX_UPLOAD_BASE64_LENGTH = 12 * 1024 * 1024; // ~1.5x file size with headroom

  function log(msg) {
    const line = new Date().toISOString() + ' ' + msg;
    console.log('[ZenRipple] ' + msg);
    logBuffer.push(line);
    if (logBuffer.length > MAX_LOG_LINES) logBuffer.shift();
  }

  // ============================================
  // SESSION MODEL
  // ============================================

  class Session {
    constructor(id) {
      this.id = id;
      this.connections = new Map();     // connId -> WebSocketConnection
      this.agentTabs = new Set();
      this.claimedTabs = new Set();     // Tabs claimed from other sessions/user — exempt from auto-cleanup
      this.tabEvents = [];              // max 200, per-session
      this.tabEventIndex = 0;           // monotonic
      this.dialogEvents = [];           // max 50, per-session — all dialog appearances
      this.dialogEventIndex = 0;        // monotonic
      this._pendingDialogs = [];        // consumable queue for handle_dialog (max 20)
      this.popupBlockedEvents = [];     // max 50, per-session
      this.popupBlockedEventIndex = 0;  // monotonic
      this.notifDialogCursor = 0;      // session-level cursor for notification piggybacking
      this.notifPopupCursor = 0;       // session-level cursor for notification piggybacking
      this.recordingActive = false;
      this.recordedActions = [];
      this.name = null;               // Human-readable session name (set via set_session_name)
      this.colorIndex = 0;            // Index into SESSION_COLOR_PALETTE (assigned by createSession)
      this.createdAt = Date.now();
      this.lastActivity = Date.now();
      this.staleTimer = null;
    }

    pushTabEvent(event) {
      event._index = this.tabEventIndex++;
      this.tabEvents.push(event);
      if (this.tabEvents.length > 200) this.tabEvents.shift();
    }

    pushDialogEvent(event) {
      event._index = this.dialogEventIndex++;
      this.dialogEvents.push(event);
      if (this.dialogEvents.length > 50) {
        const old = this.dialogEvents.shift();
        // Only clean up WeakRef if the dialog isn't still in _pendingDialogs
        // (it could still be awaiting handle_dialog)
        if (!this._pendingDialogs.includes(old)) {
          dialogWindowRefs.delete(old);
        }
      }
    }

    pushPopupBlockedEvent(event) {
      event._index = this.popupBlockedEventIndex++;
      this.popupBlockedEvents.push(event);
      if (this.popupBlockedEvents.length > 50) this.popupBlockedEvents.shift();
    }

    touch() {
      this.lastActivity = Date.now();
      if (this.staleTimer) {
        clearTimeout(this.staleTimer);
        this.staleTimer = null;
      }
    }
  }

  const sessions = new Map();            // sessionId -> Session
  const connectionToSession = new Map(); // connId -> sessionId

  let nextConnectionId = 1;

  // Drain pending notifications from a session, advancing its cursors.
  function collectSessionNotifications(session) {
    const notifications = [];
    for (const d of session.dialogEvents) {
      if (d._index >= session.notifDialogCursor) {
        notifications.push({ type: 'dialog_opened', dialog_type: d.type, message: d.message, tab_id: d.tab_id });
      }
    }
    if (session.dialogEvents.length > 0) {
      session.notifDialogCursor = session.dialogEventIndex;
    }
    for (const p of session.popupBlockedEvents) {
      if (p._index >= session.notifPopupCursor) {
        notifications.push({ type: 'popup_blocked', blocked_count: p.blocked_count, popup_urls: p.popup_urls, tab_id: p.tab_id });
      }
    }
    if (session.popupBlockedEvents.length > 0) {
      session.notifPopupCursor = session.popupBlockedEventIndex;
    }
    return notifications;
  }

  function createSession() {
    const id = crypto.randomUUID();
    const session = new Session(id);
    session.colorIndex = _nextColorIndex;
    _nextColorIndex = (_nextColorIndex + 1) % SESSION_COLOR_PALETTE.length;
    sessions.set(id, session);
    log('Session created: ' + id + ' [color:' + session.colorIndex + ']');
    return session;
  }

  function destroySession(sessionId) {
    const session = sessions.get(sessionId);
    if (!session) return;

    log('Destroying session: ' + sessionId);

    // Close all connections in this session
    for (const [connId, conn] of session.connections) {
      try { conn.close(); } catch (e) {}
      connectionToSession.delete(connId);
    }
    session.connections.clear();

    // Close created tabs, release claimed tabs back to unclaimed.
    // Copy to array first — removeTab triggers TabClose which modifies agentTabs during iteration.
    const tabsToProcess = [...session.agentTabs];
    session.agentTabs.clear();
    const claimedSet = new Set(session.claimedTabs);
    session.claimedTabs.clear();
    for (const tab of tabsToProcess) {
      if (claimedSet.has(tab)) {
        // Revert claimed tab to unclaimed — do NOT close it.
        // Keep data-agent-tab-id so the tab retains its stable identifier
        // and can be re-claimed by another session via list_workspace_tabs.
        try {
          tab.removeAttribute('data-agent-session-id');
          clearTabIndicators(tab);
          log('Released claimed tab back to unclaimed: ' +
            (tab.getAttribute('data-agent-tab-id') || tab.linkedPanel));
        } catch (e) {
          log('Error releasing claimed tab: ' + e);
        }
      } else {
        // Close tabs the session created
        try {
          if (tab.parentNode) gBrowser.removeTab(tab);
        } catch (e) {
          log('Error removing tab during session destroy: ' + e);
        }
      }
    }

    // Clean up intercept rules created by this session
    for (let i = interceptRules.length - 1; i >= 0; i--) {
      if (interceptRules[i].sessionId === sessionId) {
        interceptRules.splice(i, 1);
      }
    }

    // Clean up dialogWindowRefs for this session's dialogs
    for (const d of session.dialogEvents) dialogWindowRefs.delete(d);
    for (const d of session._pendingDialogs) dialogWindowRefs.delete(d);

    if (session.staleTimer) {
      clearTimeout(session.staleTimer);
      session.staleTimer = null;
    }

    sessions.delete(sessionId);
    log('Session destroyed: ' + sessionId);
  }

  // Grace timer: start counting down when last connection leaves
  const GRACE_PERIOD_MS = 5 * 60 * 1000; // 5 minutes

  function startGraceTimer(session) {
    if (session.staleTimer) return; // already running
    session.staleTimer = setTimeout(() => {
      if (session.connections.size === 0) {
        log('Grace period expired for session ' + session.id + ' — destroying');
        destroySession(session.id);
      }
    }, GRACE_PERIOD_MS);
    log('Grace timer started for session ' + session.id);
  }

  // Stale sweep: check for sessions inactive for > 30 minutes
  const STALE_THRESHOLD_MS = 30 * 60 * 1000;
  const STALE_SWEEP_MS = 10 * 60 * 1000;
  // 2 minutes: tabs from sessions with no connections and inactive this long can be claimed.
  // Intentionally shorter than GRACE_PERIOD_MS (5 min) so other agents can pick up
  // orphaned tabs promptly. If the original session reconnects, its agentTabs Set
  // will already have the tab removed (via tab_claimed_away event), and it can
  // see what happened via get_tab_events.
  const CLAIM_STALE_MS = 2 * 60 * 1000;

  // Tab indicator: threshold for active→claimed transition
  const TAB_ACTIVE_THRESHOLD_MS = 60 * 1000;   // 60 seconds
  const TAB_INDICATOR_SWEEP_MS = 5 * 1000;     // check every 5s
  const MAX_SESSION_NAME_LENGTH = 32;

  // Session accent color palette — visually distinct hues for dark backgrounds.
  // Each entry is an RGB triplet string for use with rgba(var(--sh), alpha).
  const SESSION_COLOR_PALETTE = [
    '34,211,238',   // cyan/teal
    '167,139,250',  // violet
    '251,113,133',  // rose/pink
    '163,230,53',   // lime
    '56,189,248',   // sky blue
    '232,121,249',  // fuchsia
    '251,146,60',   // orange
    '250,204,21',   // yellow
  ];
  let _nextColorIndex = 0;

  // CSS injected at runtime for agent tab visual indicators.
  // Uses CSS custom property --sh (session hue as R,G,B) set on each tab element.
  // data-agent-indicator="active"|"claimed" controls brightness (active = bright, claimed = dim).
  const AGENT_TAB_CSS = `
/* === ZenRipple Tab Indicators === */

@keyframes zenripple-wave-sweep {
  0% {
    background-position: 200% 0;
  }
  100% {
    background-position: -100% 0;
  }
}

@keyframes zenripple-dot-breathe {
  0%, 100% {
    box-shadow: 0 0 0 1.5px var(--zen-dialog-background, light-dark(#f0f0f0, #1a1b22)),
                0 0 4px rgba(var(--sh),0.4);
    opacity: 1;
  }
  50% {
    box-shadow: 0 0 0 1.5px var(--zen-dialog-background, light-dark(#f0f0f0, #1a1b22)),
                0 0 10px rgba(var(--sh),1);
    opacity: 0.7;
  }
}

/* --- ACTIVE: wave sweep + stripe --- */
.tabbrowser-tab[data-agent-indicator="active"] > .tab-stack > .tab-background {
  background-image: linear-gradient(
    90deg,
    rgba(var(--sh),0.06) 0%,
    rgba(var(--sh),0.22) 35%,
    rgba(var(--sh),0.06) 55%,
    transparent 80%
  ) !important;
  background-size: 300% 100% !important;
  box-shadow: inset 3px 0 0 rgb(var(--sh)) !important;
  animation: zenripple-wave-sweep 4s ease-in-out infinite !important;
}

.tabbrowser-tab[data-agent-indicator="active"] .tab-label {
  opacity: 1 !important;
}

.tabbrowser-tab[data-agent-indicator="active"] .tab-icon-image {
  opacity: 1 !important;
}

/* --- CLAIMED: dim session-colored wash + stripe --- */
.tabbrowser-tab[data-agent-indicator="claimed"] > .tab-stack > .tab-background {
  background-image: linear-gradient(90deg, rgba(var(--sh),0.04) 0%, transparent 100%) !important;
  box-shadow: inset 3px 0 0 rgba(var(--sh),0.25) !important;
}

.tabbrowser-tab[data-agent-indicator="claimed"] .tab-label {
  opacity: 0.7 !important;
}

/* --- Presence dot on favicon via .tab-icon-image::after --- */
/* NOT .tab-icon-stack::after which conflicts with Zen's sound-playing indicator */

.tabbrowser-tab[data-agent-indicator] .tab-icon-image {
  overflow: visible !important;
  position: relative;
}

.tabbrowser-tab[data-agent-indicator="active"] .tab-icon-image::after,
.tabbrowser-tab[data-agent-indicator="claimed"] .tab-icon-image::after {
  content: '';
  position: absolute;
  bottom: -2px;
  right: -2px;
  border-radius: 50%;
  pointer-events: none;
  z-index: 10;
}

.tabbrowser-tab[data-agent-indicator="active"] .tab-icon-image::after {
  width: 7px;
  height: 7px;
  background: rgb(var(--sh));
  box-shadow: 0 0 0 1.5px var(--zen-dialog-background, light-dark(#f0f0f0, #1a1b22)),
              0 0 5px rgba(var(--sh),0.5);
  animation: zenripple-dot-breathe 2.5s ease-in-out infinite;
}

.tabbrowser-tab[data-agent-indicator="claimed"] .tab-icon-image::after {
  width: 5px;
  height: 5px;
  background: rgba(var(--sh),0.5);
  box-shadow: 0 0 0 1.5px var(--zen-dialog-background, light-dark(#f0f0f0, #1a1b22));
}

/* --- Selected tab layering: Zen's selected bg shows through, our wash overlays --- */
.tabbrowser-tab[data-agent-indicator="active"][selected="true"] > .tab-stack > .tab-background {
  background-image: linear-gradient(
    90deg,
    rgba(var(--sh),0.08) 0%,
    rgba(var(--sh),0.24) 35%,
    rgba(var(--sh),0.08) 55%,
    transparent 80%
  ) !important;
  background-size: 300% 100% !important;
}

.tabbrowser-tab[data-agent-indicator="claimed"][selected="true"] > .tab-stack > .tab-background {
  background-image: linear-gradient(90deg, rgba(var(--sh),0.06) 0%, transparent 100%) !important;
}

/* --- Sublabel coloring (session name in session accent color) --- */
.tabbrowser-tab[data-agent-indicator="active"] .zen-tab-sublabel {
  color: rgb(var(--sh)) !important;
  opacity: 0.8 !important;
}

.tabbrowser-tab[data-agent-indicator="claimed"] .zen-tab-sublabel {
  color: rgb(var(--sh)) !important;
  opacity: 0.35 !important;
}
`;

  function staleSweep() {
    const now = Date.now();
    for (const [id, session] of sessions) {
      if (session.connections.size === 0 && now - session.lastActivity > STALE_THRESHOLD_MS) {
        log('Stale sweep: removing inactive session ' + id);
        destroySession(id);
      }
    }
  }

  let staleSweepInterval = null;
  let tabIndicatorInterval = null;

  // ============================================
  // WEBSOCKET SERVER (XPCOM nsIServerSocket)
  // ============================================

  // Use a browser-global to prevent multiple instances across windows.
  // fx-autoconfig loads .uc.js per-window; we only want one server.
  const GLOBAL_KEY = '__zenrippleAgentServer';

  let serverSocket = null;

  // --- Auth token: shared secret at ~/.zenripple/auth ---
  let authToken = null;

  async function loadOrCreateAuthToken() {
    const homeDir = Services.dirsvc.get('Home', Ci.nsIFile).path;
    const dir = PathUtils.join(homeDir, '.zenripple');
    const file = PathUtils.join(dir, 'auth');
    try {
      const text = await IOUtils.readUTF8(file);
      const token = text.trim();
      if (token.length >= 32) {
        authToken = token;
        log('Auth token loaded from ' + file);
        return;
      }
    } catch (e) { /* file doesn't exist yet — generate below */ }

    // Generate new token (73 chars: two UUIDs joined by hyphen)
    authToken = crypto.randomUUID() + '-' + crypto.randomUUID();
    try {
      await IOUtils.makeDirectory(dir, { ignoreExisting: true });
      await IOUtils.writeUTF8(file, authToken + '\n');
      await IOUtils.setPermissions(file, 0o600);
      log('Auth token generated and saved to ' + file);
    } catch (e) {
      log('WARNING: Could not write auth token to ' + file + ': ' + e);
      // Token is still in memory — connections will work for this session
    }
  }

  async function startServer() {
    // Check if another window already started the server
    if (Services.appinfo && globalThis[GLOBAL_KEY]) {
      log('Server already running in another window — skipping');
      return;
    }

    // Clean up any stale server from a previous load
    stopServer();

    // Load or generate auth token before opening socket
    await loadOrCreateAuthToken();

    try {
      serverSocket = Cc['@mozilla.org/network/server-socket;1']
        .createInstance(Ci.nsIServerSocket);
      serverSocket.init(AGENT_PORT, true, -1); // loopback only
      serverSocket.asyncListen({
        onSocketAccepted(server, transport) {
          log('New connection from ' + transport.host + ':' + transport.port);
          // Accept all connections — auth validated during WebSocket handshake
          new WebSocketConnection(transport);
        },
        onStopListening(server, status) {
          log('Server stopped: ' + status);
        }
      });
      globalThis[GLOBAL_KEY] = true;
      // Start stale sweep and tab indicator sweep
      staleSweepInterval = setInterval(staleSweep, STALE_SWEEP_MS);
      tabIndicatorInterval = setInterval(updateTabIndicators, TAB_INDICATOR_SWEEP_MS);
      log('WebSocket server listening on localhost:' + AGENT_PORT);
    } catch (e) {
      log('Failed to start server: ' + e);
      if (String(e).includes('NS_ERROR_SOCKET_ADDRESS_IN_USE')) {
        log('Port ' + AGENT_PORT + ' in use. Another instance may be running.');
      } else {
        log('Will retry in 5s...');
        setTimeout(startServer, 5000);
      }
    }
  }

  function stopServer() {
    // Close all sessions
    for (const [id] of sessions) {
      destroySession(id);
    }
    if (serverSocket) {
      try { serverSocket.close(); } catch (e) {}
      serverSocket = null;
    }
    if (staleSweepInterval) {
      clearInterval(staleSweepInterval);
      staleSweepInterval = null;
    }
    if (tabIndicatorInterval) {
      clearInterval(tabIndicatorInterval);
      tabIndicatorInterval = null;
    }
    removeAgentTabStyles();
    // Remove Services.obs observers (process-global — outlive window otherwise)
    if (networkObserverRegistered) {
      try { Services.obs.removeObserver(networkObserver, 'http-on-modify-request'); } catch (e) {}
      try { Services.obs.removeObserver(networkObserver, 'http-on-examine-response'); } catch (e) {}
      networkObserverRegistered = false;
    }
    try { Services.obs.removeObserver(dialogObserver, 'common-dialog-loaded'); } catch (e) {}
    // Remove progress listener
    try { gBrowser.removeTabsProgressListener(navProgressListener); } catch (e) {}
    // Remove tab event listeners
    if (_tabOpenListener) {
      try { gBrowser.tabContainer.removeEventListener('TabOpen', _tabOpenListener); } catch (e) {}
      _tabOpenListener = null;
    }
    if (_tabCloseListener) {
      try { gBrowser.tabContainer.removeEventListener('TabClose', _tabCloseListener); } catch (e) {}
      _tabCloseListener = null;
    }
    if (_popupBlockedListener) {
      try { gBrowser.removeEventListener('DOMUpdateBlockedPopups', _popupBlockedListener); } catch (e) {}
      _popupBlockedListener = null;
    }
    // Clear global state that outlives sessions
    interceptRules.length = 0;
    interceptNextId = 1;
    networkLog.length = 0;
    networkMonitorActive = false;
    unownedDialogs.length = 0;
    dialogWindowRefs.clear();
    globalThis[GLOBAL_KEY] = false;
  }

  // ============================================
  // WEBSOCKET CONNECTION
  // ============================================

  class WebSocketConnection {
    #transport;
    #inputStream;
    #outputStream;
    #bos; // BinaryOutputStream
    #handshakeComplete = false;
    #handshakeBuffer = '';
    #frameBuffer = new Uint8Array(0);
    #closed = false;
    #pump;

    // Per-connection state
    connectionId;
    sessionId = null;
    currentAgentTab = null;
    tabEventCursor = 0;           // index into session's tabEvents
    dialogEventCursor = 0;        // index into session's dialogEvents
    popupBlockedEventCursor = 0;  // index into session's popupBlockedEvents

    constructor(transport) {
      this.connectionId = 'conn-' + (nextConnectionId++);
      this.#transport = transport;
      this.#inputStream = transport.openInputStream(0, 0, 0);
      // OPEN_UNBUFFERED (2) prevents output buffering so writes go directly to socket
      this.#outputStream = transport.openOutputStream(2, 0, 0);
      this.#bos = Cc['@mozilla.org/binaryoutputstream;1']
        .createInstance(Ci.nsIBinaryOutputStream);
      this.#bos.setOutputStream(this.#outputStream);

      this.#pump = Cc['@mozilla.org/network/input-stream-pump;1']
        .createInstance(Ci.nsIInputStreamPump);
      this.#pump.init(this.#inputStream, 0, 0, false);
      this.#pump.asyncRead(this);
    }

    // --- nsIStreamListener ---

    onStartRequest(request) {}

    onStopRequest(request, status) {
      log('Connection ' + this.connectionId + ' closed (status: ' + status + ')');
      this.#closed = true;
      // Unregister from session — always clean connectionToSession even if session is gone
      if (this.sessionId) {
        connectionToSession.delete(this.connectionId);
        const session = sessions.get(this.sessionId);
        if (session) {
          session.connections.delete(this.connectionId);
          log('Connection ' + this.connectionId + ' removed from session ' + this.sessionId +
            ' (' + session.connections.size + ' remaining)');
          // Start grace timer if no connections left
          if (session.connections.size === 0) {
            startGraceTimer(session);
          }
        }
      }
      // Help GC by clearing references
      this.currentAgentTab = null;
      this.#frameBuffer = new Uint8Array(0);
    }

    onDataAvailable(request, stream, offset, count) {
      try {
        // IMPORTANT: Use nsIBinaryInputStream, NOT nsIScriptableInputStream.
        // nsIScriptableInputStream.read() truncates at 0x00 bytes, losing data.
        const bis = Cc['@mozilla.org/binaryinputstream;1']
          .createInstance(Ci.nsIBinaryInputStream);
        bis.setInputStream(stream);
        const byteArray = bis.readByteArray(count);
        log('onDataAvailable: ' + byteArray.length + ' bytes');

        if (!this.#handshakeComplete) {
          // Handshake is ASCII/UTF-8; decode without Function.apply stack pressure.
          const data = new TextDecoder().decode(new Uint8Array(byteArray));
          this.#handleHandshake(data);
        } else {
          this.#handleWebSocketData(new Uint8Array(byteArray));
        }
      } catch (e) {
        log('Error in onDataAvailable: ' + e + '\n' + e.stack);
      }
    }

    // --- WebSocket Handshake (RFC 6455) with URL routing ---

    #handleHandshake(data) {
      this.#handshakeBuffer += data;
      // Guard against unbounded buffer from clients that never complete handshake
      if (this.#handshakeBuffer.length > 65536) {
        log('Handshake buffer too large (' + this.#handshakeBuffer.length + ' bytes) — closing');
        this.close();
        return;
      }
      const endOfHeaders = this.#handshakeBuffer.indexOf('\r\n\r\n');
      if (endOfHeaders === -1) return; // incomplete headers

      const request = this.#handshakeBuffer.substring(0, endOfHeaders);
      const remaining = this.#handshakeBuffer.substring(endOfHeaders + 4);
      this.#handshakeBuffer = '';

      // Extract request path
      const pathMatch = request.match(/^GET\s+(\S+)/);
      const path = pathMatch ? pathMatch[1] : '/';

      // Extract Sec-WebSocket-Key
      const keyMatch = request.match(/Sec-WebSocket-Key:\s*(.+)/i);
      if (!keyMatch) {
        log('Invalid WebSocket handshake — no Sec-WebSocket-Key');
        this.close();
        return;
      }

      // Validate auth token
      if (authToken) {
        const authMatch = request.match(/Authorization:\s*Bearer\s+(\S+)/i);
        const clientToken = authMatch ? authMatch[1].trim() : null;
        if (!clientToken || clientToken !== authToken) {
          log('Auth failed from ' + this.connectionId + ' — invalid or missing token');
          const errResp =
            'HTTP/1.1 401 Unauthorized\r\n' +
            'Content-Length: 0\r\n' +
            'Connection: close\r\n\r\n';
          this.#writeRaw(errResp);
          this.close();
          return;
        }
      }

      // Route: determine session
      let session;
      const sessionMatch = path.match(/^\/session\/([a-f0-9-]+)/i);
      if (sessionMatch) {
        // Join existing session
        const existingId = sessionMatch[1];
        session = sessions.get(existingId);
        if (!session) {
          log('Session not found: ' + existingId + ' — returning 404');
          const errResp =
            'HTTP/1.1 404 Not Found\r\n' +
            'Content-Length: 0\r\n' +
            'Connection: close\r\n\r\n';
          this.#writeRaw(errResp);
          this.close();
          return;
        }
        log('Joining existing session: ' + existingId);
      } else {
        // /new or / — create new session
        session = createSession();
      }

      // Register connection with session
      this.sessionId = session.id;
      session.connections.set(this.connectionId, this);
      connectionToSession.set(this.connectionId, session.id);
      session.touch();

      const key = keyMatch[1].trim();
      const acceptKey = this.#computeAcceptKey(key + WS_MAGIC);

      const response =
        'HTTP/1.1 101 Switching Protocols\r\n' +
        'Upgrade: websocket\r\n' +
        'Connection: Upgrade\r\n' +
        'Sec-WebSocket-Accept: ' + acceptKey + '\r\n' +
        'X-ZenRipple-Session: ' + session.id + '\r\n' +
        'X-ZenRipple-Connection: ' + this.connectionId + '\r\n\r\n';

      this.#writeRaw(response);
      this.#handshakeComplete = true;
      log('WebSocket handshake complete (' + this.connectionId + ' -> session ' + session.id + ')');

      // Process any remaining data as WebSocket frames (convert to Uint8Array)
      if (remaining.length > 0) {
        const remainingBytes = new Uint8Array(remaining.length);
        for (let i = 0; i < remaining.length; i++) {
          remainingBytes[i] = remaining.charCodeAt(i);
        }
        this.#handleWebSocketData(remainingBytes);
      }
    }

    #computeAcceptKey(str) {
      const hash = Cc['@mozilla.org/security/hash;1']
        .createInstance(Ci.nsICryptoHash);
      hash.init(Ci.nsICryptoHash.SHA1);
      const data = Array.from(str, c => c.charCodeAt(0));
      hash.update(data, data.length);
      return hash.finish(true); // base64 encoded
    }

    // --- WebSocket Frame Parsing ---

    #handleWebSocketData(newBytes) {
      // newBytes is a Uint8Array (binary-safe from nsIBinaryInputStream)
      const combined = new Uint8Array(this.#frameBuffer.length + newBytes.length);
      combined.set(this.#frameBuffer);
      combined.set(newBytes, this.#frameBuffer.length);
      this.#frameBuffer = combined;

      // Guard against unbounded buffer growth (e.g., malformed frame claiming huge payload)
      if (this.#frameBuffer.length > MAX_CLIENT_FRAME_BUFFER) {
        log('Frame buffer exceeded limit (' + this.#frameBuffer.length + ' bytes) — closing connection');
        this.close();
        return;
      }

      // Parse all complete frames
      while (this.#frameBuffer.length >= 2) {
        const frame = this.#parseFrame(this.#frameBuffer);
        if (!frame) break; // incomplete

        this.#frameBuffer = this.#frameBuffer.slice(frame.totalLength);

        if (frame.opcode === 0x1) {
          // Text frame
          this.#onMessage(frame.payload);
        } else if (frame.opcode === 0x8) {
          // Close frame
          this.#sendCloseFrame();
          this.close();
          return;
        } else if (frame.opcode === 0x9) {
          // Ping — respond with pong
          this.#sendFrame(frame.payload, 0xA);
        }
        // Ignore pong (0xA) and other opcodes
      }
    }

    #parseFrame(buf) {
      if (buf.length < 2) return null;

      const byte0 = buf[0];
      const byte1 = buf[1];
      const opcode = byte0 & 0x0F;
      const masked = (byte1 & 0x80) !== 0;
      let payloadLength = byte1 & 0x7F;
      let offset = 2;

      if (payloadLength === 126) {
        if (buf.length < 4) return null;
        payloadLength = (buf[2] << 8) | buf[3];
        offset = 4;
      } else if (payloadLength === 127) {
        if (buf.length < 10) return null;
        payloadLength = 0;
        for (let i = 0; i < 8; i++) {
          payloadLength = payloadLength * 256 + buf[2 + i];
        }
        offset = 10;
      }

      let maskKey = null;
      if (masked) {
        if (buf.length < offset + 4) return null;
        maskKey = buf.slice(offset, offset + 4);
        offset += 4;
      }

      if (buf.length < offset + payloadLength) return null;

      let payload = buf.slice(offset, offset + payloadLength);
      if (masked && maskKey) {
        payload = new Uint8Array(payload);
        for (let i = 0; i < payload.length; i++) {
          payload[i] ^= maskKey[i % 4];
        }
      }

      const text = new TextDecoder().decode(payload);
      return { opcode, payload: text, totalLength: offset + payloadLength };
    }

    // --- WebSocket Frame Sending ---

    #sendFrame(data, opcode = 0x1) {
      if (this.#closed) return;
      try {
        const payload = new TextEncoder().encode(data);
        const header = [];

        // FIN + opcode
        header.push(0x80 | opcode);

        // Length (server-to-client is NOT masked)
        if (payload.length < 126) {
          header.push(payload.length);
        } else if (payload.length < 65536) {
          header.push(126, (payload.length >> 8) & 0xFF, payload.length & 0xFF);
        } else {
          header.push(127);
          // Upper 4 bytes always 0 (payloads < 4GB).
          // Cannot use >> for shifts >= 32; JS bitwise ops are 32-bit.
          header.push(0, 0, 0, 0);
          header.push(
            (payload.length >> 24) & 0xFF,
            (payload.length >> 16) & 0xFF,
            (payload.length >> 8) & 0xFF,
            payload.length & 0xFF
          );
        }

        const frame = new Uint8Array(header.length + payload.length);
        frame.set(new Uint8Array(header));
        frame.set(payload, header.length);

        this.#writeBinary(frame);
      } catch (e) {
        log('Error sending frame: ' + e);
      }
    }

    #sendCloseFrame() {
      this.#sendFrame('', 0x8);
    }

    send(text) {
      this.#sendFrame(text);
    }

    // --- Raw I/O ---

    #writeRaw(str) {
      if (this.#closed) return;
      try {
        this.#bos.writeBytes(str, str.length);
      } catch (e) {
        log('Error writing raw: ' + e);
        this.close();
      }
    }

    #writeBinary(uint8arr) {
      if (this.#closed) return;
      try {
        // Chunk to avoid stack overflow in String.fromCharCode.apply for large payloads (>64KB)
        const CHUNK = 8192;
        let written = 0;
        while (written < uint8arr.length) {
          const end = Math.min(written + CHUNK, uint8arr.length);
          const slice = uint8arr.subarray(written, end);
          const str = String.fromCharCode.apply(null, slice);
          this.#bos.writeBytes(str, str.length);
          written = end;
        }
        log('writeBinary: ' + uint8arr.length + ' bytes');
      } catch (e) {
        log('Error writing binary: ' + e + '\n' + e.stack);
        this.close();
      }
    }

    close() {
      if (this.#closed) return;
      this.#closed = true;
      try { this.#bos.close(); } catch (e) {}
      try { this.#inputStream.close(); } catch (e) {}
      try { this.#outputStream.close(); } catch (e) {}
      try { this.#transport.close(0); } catch (e) {}
      // Release memory immediately
      this.#frameBuffer = new Uint8Array(0);
      this.#handshakeBuffer = '';
      this.currentAgentTab = null;
    }

    // --- Message Handling ---

    #onMessage(text) {
      let msg;
      try {
        msg = JSON.parse(text);
      } catch (e) {
        log('Invalid JSON: ' + text.substring(0, 100));
        this.send(JSON.stringify({
          id: null,
          error: { code: -32700, message: 'Parse error' }
        }));
        return;
      }

      // Handle JSON-RPC
      this.#handleCommand(msg).then(response => {
        this.send(JSON.stringify(response));
      }).catch(e => {
        log('Unhandled error in command handler: ' + e);
        this.send(JSON.stringify({
          id: msg.id || null,
          error: { code: -1, message: 'Internal error: ' + e.message }
        }));
      });
    }

    async #handleCommand(msg) {
      const handler = commandHandlers[msg.method];
      if (!handler) {
        return {
          id: msg.id,
          error: { code: -32601, message: 'Unknown method: ' + msg.method }
        };
      }

      // Build session context
      const session = sessions.get(this.sessionId);
      if (!session) {
        return {
          id: msg.id,
          error: { code: -1, message: 'Session not found: ' + this.sessionId }
        };
      }
      session.touch();

      const ctx = {
        session,
        connection: this,
        resolveTab: (tabId) => resolveTabScoped(tabId, session, this),
      };

      try {
        log('Handling: ' + msg.method + ' [' + this.connectionId + ']');

        // Timeout protection — 120s to accommodate downloads and large file uploads
        // Clear the timer when handler completes to prevent accumulation
        let timeoutId;
        let dialogCheckInterval;
        const result = await Promise.race([
          handler(msg.params || {}, ctx).finally(() => {
            clearTimeout(timeoutId);
            clearInterval(dialogCheckInterval);
          }),
          new Promise((_, reject) => {
            timeoutId = setTimeout(() => reject(new Error('Command timed out after 120s')), 120000);
          }),
          // Dialog-aware early return: if a dialog appears while a command is running
          // (e.g. confirm() blocks the content process during a click), resolve early
          // instead of hanging until the 120s timeout.
          new Promise((resolve) => {
            const startDialogIdx = session.dialogEventIndex;
            dialogCheckInterval = setInterval(() => {
              if (session.dialogEventIndex > startDialogIdx) {
                resolve({
                  success: true,
                  note: 'A dialog appeared during this command — the command may not have completed. Handle the dialog first.',
                });
              }
            }, 250);
          }),
        ]);
        log('Completed: ' + msg.method);

        // Record action if recording is active (per-session)
        if (session.recordingActive && !RECORDING_EXCLUDE.has(msg.method)) {
          const MAX_RECORDED_ACTIONS = 5000;
          const params = { ...(msg.params || {}) };
          // Strip large binary data from recording to prevent memory bloat
          if (params.base64) params.base64 = '[base64 data omitted]';
          if (params.expression && params.expression.length > 1000) {
            params.expression = params.expression.substring(0, 1000) + '...[truncated]';
          }
          session.recordedActions.push({
            method: msg.method,
            params,
            timestamp: new Date().toISOString(),
          });
          if (session.recordedActions.length > MAX_RECORDED_ACTIONS) {
            session.recordedActions.shift();
          }
        }

        // Collect notifications: all events since the session's last notification cursor.
        // Uses session-level cursors (not per-connection) so notifications survive
        // MCPorter's stateless per-call connection model.
        const notifications = collectSessionNotifications(session);
        const response = { id: msg.id, result };
        if (notifications.length > 0) response._notifications = notifications;
        return response;
      } catch (e) {
        log('Error in ' + msg.method + ': ' + e);
        // Even on error, collect pending notifications so the agent knows about dialogs/popups
        const notifications = collectSessionNotifications(session);
        const errResponse = {
          id: msg.id,
          error: { code: -1, message: e.message }
        };
        if (notifications.length > 0) errResponse._notifications = notifications;
        return errResponse;
      }
    }
  }

  // ============================================
  // WORKSPACE MANAGEMENT
  // ============================================

  // Single shared workspace for ALL sessions — never created/destroyed per session
  let agentWorkspaceId = null;
  let _ensureWorkspacePromise = null;

  async function ensureAgentWorkspace() {
    // Prevent concurrent calls from creating duplicate workspaces
    if (_ensureWorkspacePromise) return _ensureWorkspacePromise;
    _ensureWorkspacePromise = _doEnsureAgentWorkspace();
    try {
      return await _ensureWorkspacePromise;
    } finally {
      _ensureWorkspacePromise = null;
    }
  }

  async function _doEnsureAgentWorkspace() {
    // Return cached ID if workspace still exists
    if (agentWorkspaceId) {
      const ws = gZenWorkspaces?.getWorkspaceFromId(agentWorkspaceId);
      if (ws) return agentWorkspaceId;
      agentWorkspaceId = null;
    }

    if (!gZenWorkspaces) {
      log('gZenWorkspaces not available — workspace scoping disabled');
      return null;
    }

    // Look for existing workspace by name
    const workspaces = gZenWorkspaces.getWorkspaces();
    if (workspaces) {
      const existing = workspaces.find(ws => ws.name === AGENT_WORKSPACE_NAME);
      if (existing) {
        agentWorkspaceId = existing.uuid;
        // Ensure icon is set on existing workspaces (backfill)
        if (existing.icon !== AGENT_WORKSPACE_ICON) {
          existing.icon = AGENT_WORKSPACE_ICON;
          gZenWorkspaces.saveWorkspace(existing);
        }
        log('Found workspace: ' + AGENT_WORKSPACE_NAME + ' (' + agentWorkspaceId + ')');
        return agentWorkspaceId;
      }
    }

    // Create new workspace (dontChange=true to avoid UI blocking)
    try {
      const created = await gZenWorkspaces.createAndSaveWorkspace(
        AGENT_WORKSPACE_NAME, AGENT_WORKSPACE_ICON, true
      );
      agentWorkspaceId = created.uuid;
      log('Created workspace: ' + AGENT_WORKSPACE_NAME + ' (' + agentWorkspaceId + ')');
      return agentWorkspaceId;
    } catch (e) {
      log('Failed to create workspace: ' + e);
      return null;
    }
  }

  // Return all tabs across ALL workspaces (not just the active one).
  // Uses gZenWorkspaces.allStoredTabs which traverses all workspace DOM
  // containers, falling back to gBrowser.tabs if unavailable.
  function getAllTabs() {
    if (window.gZenWorkspaces) {
      try {
        const all = gZenWorkspaces.allStoredTabs;
        if (all && all.length > 0) {
          return Array.from(all).filter(tab =>
            !tab.hasAttribute('zen-glance-tab')
            && !tab.hasAttribute('zen-essential')
            && !tab.hasAttribute('zen-empty-tab')
          );
        }
      } catch (e) {
        log('allStoredTabs failed, falling back: ' + e);
      }
    }
    return Array.from(gBrowser.tabs);
  }

  // ============================================
  // SESSION-SCOPED TAB RESOLUTION
  // ============================================

  function getSessionTabs(sessionId) {
    return getAllTabs().filter(tab =>
      tab.getAttribute('data-agent-session-id') === sessionId && tab.linkedBrowser
    );
  }

  function getSessionTabCount(sessionId) {
    return getSessionTabs(sessionId).length;
  }

  function ensureSessionCanOpenTabs(session, requested = 1) {
    const current = getSessionTabCount(session.id);
    const extra = Math.max(0, requested | 0);
    if (current + extra > MAX_SESSION_TABS) {
      throw new Error(
        'Session tab limit exceeded: ' + current + '/' + MAX_SESSION_TABS +
        ' open, requested ' + extra + ' more'
      );
    }
  }

  // Get ALL tabs in the agent workspace, regardless of session ownership.
  // Returns tabs that are in the agent workspace, including unclaimed user tabs.
  // Returns an empty array if the workspace has not been created yet — this
  // prevents accidentally exposing personal tabs from other workspaces.
  function getWorkspaceTabs() {
    if (!agentWorkspaceId || !gZenWorkspaces) return [];
    return getAllTabs().filter(tab => {
      try {
        // Check if tab belongs to the agent workspace
        const wsId = tab.getAttribute('zen-workspace-id');
        return wsId === agentWorkspaceId;
      } catch (e) {
        return false;
      }
    });
  }

  // Determine the ownership status of a tab in the workspace.
  // Returns: 'unclaimed' | 'owned' | 'stale'
  function getTabOwnership(tab) {
    const sessionId = tab.getAttribute('data-agent-session-id');
    if (!sessionId) return 'unclaimed';

    const ownerSession = sessions.get(sessionId);
    if (!ownerSession) return 'unclaimed'; // session was destroyed, tab is orphaned

    // Check if the owning session is stale (no connections + inactive for CLAIM_STALE_MS)
    if (ownerSession.connections.size === 0 &&
        Date.now() - ownerSession.lastActivity > CLAIM_STALE_MS) {
      return 'stale';
    }

    return 'owned';
  }

  // ============================================
  // TAB VISUAL INDICATORS
  // ============================================

  // Set the --sh (session hue) CSS custom property on a tab from its session's color.
  function stampSessionColor(tab, session) {
    try {
      const rgb = SESSION_COLOR_PALETTE[session.colorIndex] || SESSION_COLOR_PALETTE[0];
      tab.style.setProperty('--sh', rgb);
    } catch (e) {}
  }

  // Mark a tab as actively being used right now.
  function touchTabIndicator(tab) {
    try {
      tab.setAttribute('data-agent-tab-active-at', String(Date.now()));
      tab.setAttribute('data-agent-indicator', 'active');
    } catch (e) {
      // Tab may be detaching — ignore
    }
  }

  // Periodic sweep: demote active→claimed after TAB_ACTIVE_THRESHOLD_MS of inactivity.
  function updateTabIndicators() {
    const now = Date.now();
    for (const tab of getAllTabs()) {
      if (tab.getAttribute('data-agent-indicator') !== 'active') continue;
      const ts = parseInt(tab.getAttribute('data-agent-tab-active-at') || '0', 10);
      if (now - ts > TAB_ACTIVE_THRESHOLD_MS) {
        try { tab.setAttribute('data-agent-indicator', 'claimed'); } catch (e) {}
      }
    }
  }

  // Update the sublabel (session name) on a tab.
  function updateTabSublabel(tab, sessionName) {
    try {
      const sublabel = tab.querySelector('.zen-tab-sublabel');
      if (!sublabel) return; // Zen element not present — skip gracefully
      if (sessionName) {
        // Use Zen's l10n system — setting textContent directly gets overwritten
        // by the localization engine. The l10n template passes through any
        // tabSubtitle that isn't "zen-default-pinned".
        document.l10n.setArgs(sublabel, { tabSubtitle: sessionName });
        tab.setAttribute('zen-show-sublabel', sessionName);
      } else {
        // 'zen-default-pinned' is Zen's l10n sentinel that maps to the hidden/default
        // sublabel state in zen-vertical-tabs.ftl. Setting any other value shows it as-is.
        document.l10n.setArgs(sublabel, { tabSubtitle: 'zen-default-pinned' });
        tab.removeAttribute('zen-show-sublabel');
      }
    } catch (e) {
      // Tab may be detaching
    }
  }

  // Clear all agent indicator attributes, sublabel, and session color from a tab.
  function clearTabIndicators(tab) {
    try {
      tab.removeAttribute('data-agent-indicator');
      tab.removeAttribute('data-agent-tab-active-at');
      tab.style.removeProperty('--sh');
      updateTabSublabel(tab, null);
    } catch (e) {}
  }

  // Group all tabs belonging to a session so they are adjacent in the sidebar.
  // Tab visual order = DOM order in the workspace container.
  function groupSessionTabs(sessionId) {
    try {
      if (!agentWorkspaceId || !gZenWorkspaces) return;
      const wsElement = gZenWorkspaces.workspaceElement(agentWorkspaceId);
      if (!wsElement) return;
      const container = wsElement.tabsContainer;
      if (!container) return;
      const sessionTabs = [];
      for (const child of container.children) {
        if (child.getAttribute?.('data-agent-session-id') === sessionId) {
          sessionTabs.push(child);
        }
      }
      if (sessionTabs.length <= 1) return;
      // Move all session tabs to be adjacent after the first one
      let insertAfter = sessionTabs[0];
      for (let i = 1; i < sessionTabs.length; i++) {
        const tab = sessionTabs[i];
        const nextSibling = insertAfter.nextSibling;
        if (tab !== nextSibling) {
          container.insertBefore(tab, nextSibling);
        }
        insertAfter = tab;
      }
    } catch (e) {
      // Tab grouping is cosmetic — never block operations
    }
  }

  // Inject CSS styles for agent tab indicators.
  const STYLE_ID = 'zenripple-agent-tab-styles';
  function injectAgentTabStyles() {
    if (document.getElementById(STYLE_ID)) return; // already injected
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = AGENT_TAB_CSS;
    document.head.appendChild(style);
    log('Agent tab indicator styles injected');
  }

  function removeAgentTabStyles() {
    const el = document.getElementById(STYLE_ID);
    if (el) el.remove();
  }

  function resolveTabScoped(tabId, session, conn) {
    let resolved = null;

    if (!tabId) {
      // Prefer connection's tracked current tab
      if (conn.currentAgentTab && conn.currentAgentTab.linkedBrowser) {
        resolved = conn.currentAgentTab;
      } else {
        // Fall back to first session tab
        const sessionTabs = getSessionTabs(session.id);
        if (sessionTabs.length > 0) {
          conn.currentAgentTab = sessionTabs[0];
          resolved = sessionTabs[0];
        }
      }
    } else {
      // Search within session's tabs by data-agent-tab-id
      const sessionTabs = getSessionTabs(session.id);
      for (const tab of sessionTabs) {
        if (tab.getAttribute('data-agent-tab-id') === tabId) { resolved = tab; break; }
      }
      if (!resolved) {
        // Match by linkedPanel ID
        for (const tab of sessionTabs) {
          if (tab.linkedPanel === tabId) { resolved = tab; break; }
        }
      }
      if (!resolved) {
        // Match by URL within session
        for (const tab of sessionTabs) {
          if (tab.linkedBrowser?.currentURI?.spec === tabId) { resolved = tab; break; }
        }
      }
    }

    // Mark the resolved tab as actively being used
    if (resolved) touchTabIndicator(resolved);
    return resolved;
  }

  // ============================================
  // TAB EVENT TRACKING
  // ============================================

  // Store listeners so they can be removed in stopServer()
  let _tabOpenListener = null;
  let _tabCloseListener = null;

  function setupTabEventTracking() {
    try {
      _tabOpenListener = (event) => {
        const tab = event.target;
        // Check if opener is an agent tab — find its session
        const openerBC = tab.linkedBrowser?.browsingContext?.opener;
        const openerTab = openerBC ? gBrowser.getTabForBrowser(openerBC.top?.embedderElement) : null;
        const openerSessionId = openerTab ? openerTab.getAttribute('data-agent-session-id') : null;
        const ownerSession = openerSessionId ? sessions.get(openerSessionId) : null;

        if (ownerSession) {
          if (getSessionTabCount(ownerSession.id) >= MAX_SESSION_TABS) {
            ownerSession.pushTabEvent({
              type: 'tab_open_blocked',
              reason: 'session_tab_limit',
              limit: MAX_SESSION_TABS,
              timestamp: new Date().toISOString(),
            });
            log('Session ' + ownerSession.id + ' reached tab limit (' + MAX_SESSION_TABS + ') — closing popup');
            setTimeout(() => {
              try {
                if (tab.parentNode) gBrowser.removeTab(tab);
              } catch (e) {
                log('Failed to close over-limit popup tab: ' + e);
              }
            }, 0);
            return;
          }
          // Child tab inherits parent's session
          const popupId = tab.linkedPanel || ('agent-tab-' + Date.now());
          tab.setAttribute('data-agent-tab-id', popupId);
          tab.setAttribute('data-agent-session-id', ownerSession.id);
          tab.setAttribute('data-agent-indicator', 'claimed');
          stampSessionColor(tab, ownerSession);
          if (ownerSession.name) updateTabSublabel(tab, ownerSession.name);
          ownerSession.agentTabs.add(tab);
          // Move to shared agent workspace
          if (agentWorkspaceId && gZenWorkspaces) {
            gZenWorkspaces.moveTabToWorkspace(tab, agentWorkspaceId);
          }
          groupSessionTabs(ownerSession.id);
          log('Agent popup detected for session ' + ownerSession.id + ': ' + popupId);
        }

        // Push event to the owner session (or ignore if no session)
        const tabId = tab.getAttribute('data-agent-tab-id') || tab.linkedPanel;
        const openerTabId = openerTab ? (openerTab.getAttribute('data-agent-tab-id') || openerTab.linkedPanel) : null;
        const eventData = {
          type: 'tab_opened',
          tab_id: tabId,
          opener_tab_id: openerTabId,
          is_agent_tab: !!ownerSession,
          timestamp: new Date().toISOString(),
        };
        if (ownerSession) {
          ownerSession.pushTabEvent(eventData);
        }
      };

      _tabCloseListener = (event) => {
        const tab = event.target;
        const sessionId = tab.getAttribute('data-agent-session-id');
        const session = sessionId ? sessions.get(sessionId) : null;
        if (session) {
          session.agentTabs.delete(tab);
          session.claimedTabs.delete(tab);
          session.pushTabEvent({
            type: 'tab_closed',
            tab_id: tab.getAttribute('data-agent-tab-id') || tab.linkedPanel,
            timestamp: new Date().toISOString(),
          });
        }
      };

      gBrowser.tabContainer.addEventListener('TabOpen', _tabOpenListener);
      gBrowser.tabContainer.addEventListener('TabClose', _tabCloseListener);

      log('Tab event tracking active');
    } catch (e) {
      log('Failed to setup tab event tracking: ' + e);
    }
  }

  // ============================================
  // POPUP-BLOCKED TRACKING
  // ============================================
  // Firefox/Zen fires DOMUpdateBlockedPopups on gBrowser (not DOMPopupBlocked on window).
  // The browser element exposes browser.popupAndRedirectBlocker with:
  //   .getBlockedPopupCount()     → number of blocked popups
  //   .getBlockedPopups()         → async, returns [{browsingContext, innerWindowId, popupWindowURISpec}, ...]
  //   .unblockPopup(index)        → allow a specific blocked popup

  let _popupBlockedListener = null;

  function setupPopupBlockedTracking() {
    try {
      _popupBlockedListener = async (event) => {
        try {
          const browser = event.originalTarget;
          const tab = gBrowser.getTabForBrowser(browser);
          if (!tab) return;
          const sessionId = tab.getAttribute('data-agent-session-id');
          const session = sessionId ? sessions.get(sessionId) : null;
          if (!session) return;

          const blocker = browser.popupAndRedirectBlocker;
          if (!blocker) return;

          const count = blocker.getBlockedPopupCount();
          if (count === 0) return;

          // Get details of blocked popups
          let popupUrls = [];
          try {
            const popups = await blocker.getBlockedPopups();
            popupUrls = popups.map(p => p.popupWindowURISpec || '');
          } catch (e) {
            // getBlockedPopups() can fail if the browsing context is gone
          }

          session.pushPopupBlockedEvent({
            type: 'popup_blocked',
            tab_id: tab.getAttribute('data-agent-tab-id') || tab.linkedPanel,
            blocked_count: count,
            popup_urls: popupUrls,
            timestamp: new Date().toISOString(),
          });
          log('Popup blocked [session ' + sessionId.substring(0, 8) + ']: ' +
            count + ' popup(s) — ' + (popupUrls.join(', ') || 'unknown URLs'));
        } catch (e) {
          log('Popup-blocked handler error: ' + e);
        }
      };
      gBrowser.addEventListener('DOMUpdateBlockedPopups', _popupBlockedListener);
      log('Popup-blocked tracking active (DOMUpdateBlockedPopups on gBrowser)');
    } catch (e) {
      log('Failed to setup popup-blocked tracking: ' + e);
    }
  }

  // ============================================
  // DIALOG HANDLING (per-session with global fallback)
  // ============================================

  // Global fallback for dialogs that can't be attributed to a session
  const unownedDialogs = [];
  // Global WeakRef map for handle_dialog — keyed by dialogInfo object
  const dialogWindowRefs = new Map();

  // Helper to safely read from nsIWritablePropertyBag2 or plain object
  function bagGet(bag, key, fallback) {
    // Try property bag .get() first (nsIPropertyBag2 in Firefox/Zen)
    if (typeof bag.get === 'function') {
      try { return bag.get(key) ?? fallback; } catch (e) { return fallback; }
    }
    // Fall back to direct property access (plain objects in tests)
    return bag[key] ?? fallback;
  }

  const dialogObserver = {
    observe(subject, topic, data) {
      if (topic !== 'common-dialog-loaded') return;
      try {
        const dialogWin = subject;
        const args = dialogWin.arguments?.[0];
        if (!args) return;
        const dialogInfo = {
          type: bagGet(args, 'promptType', 'unknown'), // alertCheck, confirmCheck, prompt
          message: bagGet(args, 'text', ''),
          default_value: bagGet(args, 'value', ''),
          timestamp: new Date().toISOString(),
        };

        // Attribute dialog to a session via owningBrowsingContext → tab → session
        let ownerSession = null;
        const bc = bagGet(args, 'owningBrowsingContext', null)
          || bagGet(args, 'browsingContext', null);
        if (bc) {
          const browser = bc.top?.embedderElement;
          const tab = browser ? gBrowser.getTabForBrowser(browser) : null;
          const sessionId = tab?.getAttribute('data-agent-session-id');
          ownerSession = sessionId ? sessions.get(sessionId) : null;
          if (tab) {
            dialogInfo.tab_id = tab.getAttribute('data-agent-tab-id') || tab.linkedPanel;
          }
        }

        // Store WeakRef for later accept/dismiss
        dialogWindowRefs.set(dialogInfo, new WeakRef(dialogWin));

        if (ownerSession) {
          // Per-session storage
          ownerSession.pushDialogEvent(dialogInfo);
          ownerSession._pendingDialogs.push(dialogInfo);
          if (ownerSession._pendingDialogs.length > 20) {
            const old = ownerSession._pendingDialogs.shift();
            dialogWindowRefs.delete(old);
          }
        } else {
          // Global fallback for unattributed dialogs
          unownedDialogs.push(dialogInfo);
          if (unownedDialogs.length > 20) {
            const old = unownedDialogs.shift();
            dialogWindowRefs.delete(old);
          }
        }

        log('Dialog captured: ' + dialogInfo.type +
          (ownerSession ? ' [session ' + ownerSession.id.substring(0, 8) + ']' : ' [unowned]') +
          ' — ' + dialogInfo.message.substring(0, 80));
      } catch (e) {
        log('Dialog observer error: ' + e);
      }
    }
  };

  function setupDialogObserver() {
    try {
      Services.obs.addObserver(dialogObserver, 'common-dialog-loaded');
      log('Dialog observer active');
    } catch (e) {
      log('Failed to setup dialog observer: ' + e);
    }
  }

  // ============================================
  // NETWORK MONITORING
  // ============================================

  let networkMonitorActive = false;
  const networkLog = [];           // Circular buffer of network entries
  const MAX_NETWORK_LOG = 500;
  const interceptRules = [];       // {id, sessionId, pattern: RegExp, action: 'block'|'modify_headers', headers: {}}
  let interceptNextId = 1;

  const networkObserver = {
    observe(subject, topic, data) {
      try {
        const channel = subject.QueryInterface(Ci.nsIHttpChannel);
        const url = channel.URI?.spec || '';

        // Apply intercept rules
        for (const rule of interceptRules) {
          if (rule.pattern.test(url)) {
            if (rule.action === 'block') {
              channel.cancel(Cr.NS_ERROR_ABORT);
              log('Intercepted (blocked): ' + url.substring(0, 80));
              return;
            }
            if (rule.action === 'modify_headers' && rule.headers) {
              for (const [name, value] of Object.entries(rule.headers)) {
                channel.setRequestHeader(name, value, false);
              }
            }
          }
        }

        if (!networkMonitorActive) return;

        if (topic === 'http-on-modify-request') {
          networkLog.push({
            url,
            method: channel.requestMethod,
            type: 'request',
            timestamp: new Date().toISOString(),
          });
          if (networkLog.length > MAX_NETWORK_LOG) networkLog.shift();
        } else if (topic === 'http-on-examine-response') {
          // Find matching request and update, or add new entry
          let status = 0;
          let contentType = '';
          try { status = channel.responseStatus; } catch (e) {}
          try { contentType = channel.getResponseHeader('Content-Type'); } catch (e) {}
          networkLog.push({
            url,
            method: channel.requestMethod,
            type: 'response',
            status,
            content_type: contentType,
            timestamp: new Date().toISOString(),
          });
          if (networkLog.length > MAX_NETWORK_LOG) networkLog.shift();
        }
      } catch (e) {
        // Non-HTTP channel or other error — ignore
      }
    }
  };

  let networkObserverRegistered = false;

  function ensureNetworkObserver() {
    if (networkObserverRegistered) return;
    Services.obs.addObserver(networkObserver, 'http-on-modify-request');
    Services.obs.addObserver(networkObserver, 'http-on-examine-response');
    networkObserverRegistered = true;
    log('Network observer registered');
  }

  // ============================================
  // ACTION RECORDING (Phase 9) — per-session state in Session class
  // ============================================

  // Commands to exclude from recording (meta/debug commands)
  const RECORDING_EXCLUDE = new Set([
    'ping', 'get_agent_logs', 'record_start', 'record_stop',
    'record_save', 'record_replay', 'get_tab_events', 'get_dialogs', 'get_popup_blocked_events',
    'list_tabs', 'list_workspace_tabs', 'claim_tab', 'get_page_info', 'get_navigation_status',
    'network_get_log', 'intercept_list_rules', 'eval_chrome',
    'get_config', 'set_config',
    'session_info', 'session_close', 'list_sessions', 'set_session_name',
  ]);

  // ============================================
  // NAVIGATION STATUS TRACKING
  // ============================================

  // WeakMap: browser → {url, httpStatus, errorCode, loading}
  const navStatusMap = new WeakMap();

  const navProgressListener = {
    QueryInterface: ChromeUtils.generateQI([
      'nsIWebProgressListener',
      'nsISupportsWeakReference',
    ]),

    onStateChange(webProgress, request, stateFlags, status) {
      if (!(stateFlags & Ci.nsIWebProgressListener.STATE_IS_DOCUMENT)) return;
      const browser = webProgress?.browsingContext?.top?.embedderElement;
      if (!browser) return;

      const entry = navStatusMap.get(browser) || {};

      if (stateFlags & Ci.nsIWebProgressListener.STATE_START) {
        entry.loading = true;
        entry.httpStatus = 0;
        entry.errorCode = 0;
        entry.url = request?.name || '';
      }
      if (stateFlags & Ci.nsIWebProgressListener.STATE_STOP) {
        entry.loading = false;
        if (request instanceof Ci.nsIHttpChannel) {
          try {
            entry.httpStatus = request.responseStatus;
          } catch (e) {
            // Channel may be invalid
          }
        }
        if (status !== 0) {
          entry.errorCode = status;
        }
      }
      navStatusMap.set(browser, entry);
    },

    onLocationChange() {},
    onProgressChange() {},
    onSecurityChange() {},
    onStatusChange() {},
    onContentBlockingEvent() {},
  };

  function setupNavTracking() {
    try {
      gBrowser.addTabsProgressListener(navProgressListener);
      log('Navigation status tracking active');
    } catch (e) {
      log('Failed to setup nav tracking: ' + e);
    }
  }

  // ============================================
  // SCREENSHOT
  // ============================================

  const MAX_SCREENSHOT_WIDTH = 1568; // Claude's recommended max image width

  async function screenshotTab(tab) {
    const browser = tab.linkedBrowser;
    const browsingContext = browser.browsingContext;
    const wg = browsingContext?.currentWindowGlobal;

    if (wg) {
      try {
        // drawSnapshot(rect, scale, bgColor) — null rect = full viewport
        const bitmap = await wg.drawSnapshot(null, 1, 'white');
        try {
          const canvas = document.createElement('canvas');
          // Resize to max width while maintaining aspect ratio
          if (bitmap.width > MAX_SCREENSHOT_WIDTH) {
            canvas.width = MAX_SCREENSHOT_WIDTH;
            canvas.height = Math.round(bitmap.height * (MAX_SCREENSHOT_WIDTH / bitmap.width));
          } else {
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
          }
          const ctx = canvas.getContext('2d');
          ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
          // JPEG is 5-10x smaller than PNG for web page screenshots
          const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
          return { image: dataUrl, width: canvas.width, height: canvas.height, viewport_width: bitmap.width, viewport_height: bitmap.height };
        } finally {
          bitmap.close(); // Prevent memory leak
        }
      } catch (e) {
        log('drawSnapshot failed, trying PageThumbs fallback: ' + e);
      }
    }

    // Fallback: PageThumbs
    try {
      const { PageThumbs } = ChromeUtils.importESModule(
        'resource://gre/modules/PageThumbs.sys.mjs'
      );
      const blob = await PageThumbs.captureToBlob(browser, {
        fullScale: true,
        fullViewport: true,
      });
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.readAsDataURL(blob);
        reader.onloadend = () => resolve(reader.result);
        reader.onerror = reject;
      });
      return { image: dataUrl, width: null, height: null };
    } catch (e2) {
      throw new Error('Screenshot failed: drawSnapshot: ' + e2 + '; PageThumbs unavailable');
    }
  }

  // ============================================
  // ACTOR HELPERS
  // ============================================

  function getActorForTab(tab, frameId) {
    // tab is already resolved by the caller via ctx.resolveTab()
    if (!tab) throw new Error('Tab not found');
    const browser = tab.linkedBrowser;
    if (!browser) throw new Error('Tab has no linked browser');
    const wg = frameId
      ? getWindowGlobalForFrame(browser, frameId)
      : browser.browsingContext?.currentWindowGlobal;
    if (!wg) throw new Error(frameId ? 'Frame not found: ' + frameId : 'Page not loaded (no currentWindowGlobal)');
    try {
      return wg.getActor('ZenRippleAgent');
    } catch (e) {
      const url = browser.currentURI?.spec || '?';
      log('getActor failed: ' + e + ' (url: ' + url + ')');
      // Give a helpful error for non-HTTP pages where the actor won't load
      if (url === 'about:blank') {
        throw new Error(
          'Cannot access page content — the tab is on about:blank. ' +
          'Navigate to a URL first, then use wait_for_load before interacting.'
        );
      }
      if (url.startsWith('about:') || url.startsWith('data:')) {
        throw new Error(
          'Cannot access page content — the tab is on ' + url +
          ' which is not an HTTP/HTTPS page. Navigate to an HTTP/HTTPS URL first.'
        );
      }
      throw new Error('Cannot access page content: ' + e.message);
    }
  }

  function getWindowGlobalForFrame(browser, frameId) {
    const contexts = browser.browsingContext?.getAllBrowsingContextsInSubtree() || [];
    for (const ctx of contexts) {
      if (ctx.id == frameId) {  // Allow type coercion (int vs string)
        return ctx.currentWindowGlobal;
      }
    }
    return null;
  }

  function listFramesForTab(tab) {
    if (!tab) throw new Error('Tab not found');
    const browser = tab.linkedBrowser;
    const topCtx = browser.browsingContext;
    if (!topCtx) throw new Error('Page not loaded');
    const contexts = topCtx.getAllBrowsingContextsInSubtree() || [];
    return contexts.map(ctx => ({
      frame_id: ctx.id,
      url: ctx.currentWindowGlobal?.documentURI?.spec || '',
      is_top: ctx === topCtx,
    }));
  }

  // Interaction commands (click, key press, etc.) can trigger focus loss,
  // navigation, or browsing-context changes that destroy the actor before
  // the sendQuery response arrives. The action WAS dispatched — wrap with
  // a fallback so the caller gets a success result.
  async function actorInteraction(tab, messageName, data, fallbackResult, frameId) {
    const actor = getActorForTab(tab, frameId);
    try {
      return await actor.sendQuery(messageName, data);
    } catch (e) {
      if (String(e).includes('destroyed') || String(e).includes('AbortError')) {
        log(messageName + ': actor destroyed (action was dispatched)');
        return fallbackResult || { success: true, note: 'Action dispatched (actor destroyed before confirmation)' };
      }
      throw e;
    }
  }

  // ============================================
  // DOWNLOADS HELPER
  // ============================================

  let DownloadsModule = null;
  async function getDownloads() {
    if (!DownloadsModule) {
      const mod = ChromeUtils.importESModule('resource://gre/modules/Downloads.sys.mjs');
      DownloadsModule = mod.Downloads;
    }
    return DownloadsModule;
  }

  // ============================================
  // CHROME EVAL HELPER
  // ============================================

  function formatChromeResult(value, depth = 0) {
    if (depth > 3) return '[max depth]';
    if (value === null) return null;
    if (value === undefined) return undefined;
    if (typeof value === 'string') {
      return value.length > 10000 ? value.substring(0, 10000) + '...[truncated]' : value;
    }
    if (typeof value === 'number' || typeof value === 'boolean') return value;
    if (Array.isArray(value)) {
      return value.slice(0, 100).map(v => formatChromeResult(v, depth + 1));
    }
    if (typeof value === 'object') {
      // XPCOM objects may throw on property access
      const result = {};
      try {
        const keys = Object.keys(value).slice(0, 50);
        for (const key of keys) {
          try {
            result[key] = formatChromeResult(value[key], depth + 1);
          } catch (e) {
            result[key] = '[error: ' + e.message + ']';
          }
        }
      } catch (e) {
        return String(value);
      }
      return result;
    }
    return String(value);
  }

  // ============================================
  // COMMAND HANDLERS
  // ============================================

  const commandHandlers = {
    // --- Ping / Debug ---
    ping: async (params, ctx) => {
      return { pong: true, version: VERSION, session_id: ctx.session.id };
    },

    get_agent_logs: async () => {
      return { logs: logBuffer.slice(-50) };
    },

    // --- Tab Management ---
    create_tab: async ({ url, persist }, ctx) => {
      ensureSessionCanOpenTabs(ctx.session, 1);
      const wsId = await ensureAgentWorkspace();
      const tab = gBrowser.addTab(url || 'about:blank', {
        triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()
      });
      // Stamp stable ID and session ID before workspace move
      const stableId = tab.linkedPanel || ('agent-tab-' + Date.now());
      tab.setAttribute('data-agent-tab-id', stableId);
      tab.setAttribute('data-agent-session-id', ctx.session.id);
      tab.setAttribute('data-agent-indicator', 'claimed');
      stampSessionColor(tab, ctx.session);
      if (ctx.session.name) updateTabSublabel(tab, ctx.session.name);
      ctx.session.agentTabs.add(tab);
      // When persist is true, mark as claimed so the tab survives session destruction
      if (persist) ctx.session.claimedTabs.add(tab);

      // Move tab to shared agent workspace
      if (wsId && gZenWorkspaces) {
        gZenWorkspaces.moveTabToWorkspace(tab, wsId);
      }
      groupSessionTabs(ctx.session.id);
      ctx.connection.currentAgentTab = tab;
      // Only set selectedTab if agent workspace is active
      try {
        if (gZenWorkspaces && gZenWorkspaces.activeWorkspace === wsId) {
          gBrowser.selectedTab = tab;
        }
      } catch (e) { /* ignore — workspace may not be active */ }
      log('Created tab: ' + stableId + ' -> ' + (url || 'about:blank') +
        (persist ? ' [persist]' : '') + ' [session:' + ctx.session.id.substring(0, 8) + ']');

      return {
        tab_id: stableId,
        url: url || 'about:blank',
        persist: !!persist,
      };
    },

    close_tab: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      if (ctx.connection.currentAgentTab === tab) ctx.connection.currentAgentTab = null;
      ctx.session.agentTabs.delete(tab);
      ctx.session.claimedTabs.delete(tab);
      gBrowser.removeTab(tab);
      return { success: true };
    },

    switch_tab: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      ctx.connection.currentAgentTab = tab;
      gBrowser.selectedTab = tab;
      return { success: true };
    },

    list_tabs: async (params, ctx) => {
      const tabs = getSessionTabs(ctx.session.id);
      return tabs.map(t => ({
        tab_id: t.getAttribute('data-agent-tab-id') || t.linkedPanel || '',
        title: t.label || '',
        url: t.linkedBrowser?.currentURI?.spec || '',
        active: t === ctx.connection.currentAgentTab
      })).filter(t => t.tab_id);
    },

    // --- Navigation ---
    navigate: async ({ url, tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      // Defer navigation so response is sent before any process swap
      setTimeout(() => {
        try {
          const browser = tab.linkedBrowser;
          const loadOpts = {
            triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()
          };
          if (typeof browser.fixupAndLoadURIString === 'function') {
            browser.fixupAndLoadURIString(url, loadOpts);
          } else {
            browser.loadURI(Services.io.newURI(url), loadOpts);
          }
        } catch (e) {
          log('Navigate error (deferred): ' + e);
        }
      }, 0);
      return { success: true };
    },

    go_back: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      tab.linkedBrowser.goBack();
      return { success: true };
    },

    go_forward: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      tab.linkedBrowser.goForward();
      return { success: true };
    },

    reload: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      tab.linkedBrowser.reload();
      return { success: true };
    },

    // --- Tab Events (per-connection cursor into session log) ---
    get_tab_events: async (params, ctx) => {
      const events = ctx.session.tabEvents.filter(e => e._index >= ctx.connection.tabEventCursor);
      if (events.length > 0) {
        ctx.connection.tabEventCursor = events[events.length - 1]._index + 1;
      }
      // Strip internal _index from returned events
      return events.map(({ _index, ...rest }) => rest);
    },

    // --- Dialogs (per-session with unowned fallback) ---
    get_dialogs: async (params, ctx) => {
      // Merge session-owned + unowned dialogs for this session's view
      const sessionDialogs = ctx.session._pendingDialogs;
      const all = [...sessionDialogs, ...unownedDialogs];
      return all.map(d => ({
        type: d.type,
        message: d.message,
        default_value: d.default_value,
        timestamp: d.timestamp,
        tab_id: d.tab_id,
      }));
    },

    handle_dialog: async ({ action, text }, ctx) => {
      if (!action) throw new Error('action is required (accept or dismiss)');
      // Try session-owned dialogs first, then unowned
      let dialog;
      let source;
      if (ctx.session._pendingDialogs.length > 0) {
        dialog = ctx.session._pendingDialogs.shift();
        source = 'session';
      } else if (unownedDialogs.length > 0) {
        dialog = unownedDialogs.shift();
        source = 'unowned';
      } else {
        throw new Error('No pending dialogs');
      }
      const dialogWin = dialogWindowRefs.get(dialog)?.deref();
      dialogWindowRefs.delete(dialog);
      if (!dialogWin || dialogWin.closed) {
        return { success: false, note: 'Dialog already closed' };
      }
      try {
        const ui = dialogWin.document?.getElementById('commonDialog');
        if (!ui) throw new Error('Dialog UI not found');
        if (text !== undefined && dialog.type === 'prompt') {
          const input = dialogWin.document.getElementById('loginTextbox');
          if (input) input.value = text;
        }
        if (action === 'accept') {
          ui.acceptDialog();
        } else {
          ui.cancelDialog();
        }
        return { success: true, action, type: dialog.type, source };
      } catch (e) {
        return { success: false, error: e.message };
      }
    },

    // --- Popup Blocked Events (drain-on-read, per-connection cursor) ---
    get_popup_blocked_events: async (params, ctx) => {
      const events = ctx.session.popupBlockedEvents.filter(
        e => e._index >= ctx.connection.popupBlockedEventCursor
      );
      if (events.length > 0) {
        ctx.connection.popupBlockedEventCursor = events[events.length - 1]._index + 1;
      }
      return events.map(({ _index, ...rest }) => rest);
    },

    allow_blocked_popup: async ({ tab_id, index }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      const browser = tab.linkedBrowser;
      const blocker = browser.popupAndRedirectBlocker;
      if (!blocker) throw new Error('Popup blocker API not available');
      const count = blocker.getBlockedPopupCount();
      if (count === 0) throw new Error('No blocked popups for this tab');
      if (index !== undefined && index !== null && (index < 0 || index >= count)) {
        throw new Error('Popup index out of range: ' + index + ' (have ' + count + ' blocked popups)');
      }
      // Get blocked popups list for detail
      let popups = [];
      try { popups = await blocker.getBlockedPopups(); } catch (e) {}

      // Tab creation from unblockPopup/unblockAllPopups is async —
      // the _tabOpenListener handles workspace moves and session stamping.
      // We listen for TabOpen to collect opened tab IDs for the response.
      const expectedCount = (index !== undefined && index !== null) ? 1 : count;
      const newTabs = [];
      const tabsReady = new Promise((resolve) => {
        const onTabOpen = (event) => {
          newTabs.push(event.target);
          if (newTabs.length >= expectedCount) {
            gBrowser.tabContainer.removeEventListener('TabOpen', onTabOpen);
            resolve();
          }
        };
        gBrowser.tabContainer.addEventListener('TabOpen', onTabOpen);
        // Timeout: don't wait forever if some popups don't create tabs
        setTimeout(() => {
          gBrowser.tabContainer.removeEventListener('TabOpen', onTabOpen);
          resolve();
        }, 3000);
      });

      // Unblock specific popup by index, or all
      if (index !== undefined && index !== null) {
        blocker.unblockPopup(index);
      } else {
        blocker.unblockAllPopups();
      }

      // Wait for tab(s) to appear asynchronously
      await tabsReady;

      // Collect opened tab IDs — the _tabOpenListener already handled
      // workspace moves and session stamping via browsingContext.opener.
      // As a safety net, stamp any unstamped tabs ourselves.
      const wsId = agentWorkspaceId || await ensureAgentWorkspace();
      const openedTabIds = [];
      for (const newTab of newTabs) {
        if (newTab.getAttribute('data-agent-session-id')) {
          openedTabIds.push(newTab.getAttribute('data-agent-tab-id') || newTab.linkedPanel);
          continue;
        }
        // Verify this tab was opened by our tab (avoid capturing unrelated tabs)
        const openerBC = newTab.linkedBrowser?.browsingContext?.opener;
        const openerBrowser = openerBC?.top?.embedderElement;
        if (!openerBrowser || openerBrowser !== tab.linkedBrowser) continue;
        // Safety net: stamp and move if _tabOpenListener didn't catch it
        const stableId = newTab.linkedPanel || ('agent-tab-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6));
        newTab.setAttribute('data-agent-tab-id', stableId);
        newTab.setAttribute('data-agent-session-id', ctx.session.id);
        ctx.session.agentTabs.add(newTab);
        newTab.setAttribute('data-agent-indicator', 'claimed');
        stampSessionColor(newTab, ctx.session);
        if (ctx.session.name) updateTabSublabel(newTab, ctx.session.name);
        if (wsId && gZenWorkspaces) {
          gZenWorkspaces.moveTabToWorkspace(newTab, wsId);
        }
        groupSessionTabs(ctx.session.id);
        openedTabIds.push(stableId);
        ctx.session.pushTabEvent({
          type: 'tab_opened',
          tab_id: stableId,
          opener_tab_id: tab.getAttribute('data-agent-tab-id') || tab.linkedPanel,
          is_agent_tab: true,
          source: 'unblocked_popup',
          timestamp: new Date().toISOString(),
        });
        log('Unblocked popup moved to agent workspace (safety net): ' + stableId);
      }

      const result = {
        success: true,
        unblocked: (index !== undefined && index !== null) ? 1 : count,
        opened_tab_ids: openedTabIds,
      };
      if (index !== undefined && index !== null) {
        result.popup_url = popups[index]?.popupWindowURISpec || '';
      } else {
        result.popup_urls = popups.map(p => p.popupWindowURISpec || '');
      }
      return result;
    },

    // --- Navigation Status ---
    get_navigation_status: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      const browser = tab.linkedBrowser;
      const entry = navStatusMap.get(browser) || {};
      return {
        url: browser.currentURI?.spec || '',
        http_status: entry.httpStatus || 0,
        error_code: entry.errorCode || 0,
        loading: browser.webProgress?.isLoadingDocument || false,
      };
    },

    // --- Frames ---
    list_frames: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      return listFramesForTab(tab);
    },

    // Click at content viewport coordinates with auto-routing through iframes.
    // Shows a cyan crosshair, then delegates to click_coordinates which
    // automatically routes into iframes when detected.
    click_native: async ({ tab_id, x, y, color }, ctx) => {
      if (x === undefined || y === undefined) throw new Error('x and y are required');
      // Delegate to click_coordinates with the cyan color for grounded clicks
      return await commandHandlers.click_coordinates(
        { tab_id, x, y, color: color || 'cyan' }, ctx
      );
    },

    // --- Observation ---
    get_viewport_dimensions: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:GetViewportDimensions', {});
    },

    get_page_info: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      const browser = tab.linkedBrowser;
      return {
        url: browser.currentURI?.spec || '',
        title: tab.label || '',
        loading: browser.webProgress?.isLoadingDocument || false,
        can_go_back: browser.canGoBack,
        can_go_forward: browser.canGoForward
      };
    },

    screenshot: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      return await screenshotTab(tab);
    },

    get_dom: async ({ tab_id, frame_id, viewport_only, max_elements, incremental }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:ExtractDOM', {
        viewport_only: !!viewport_only,
        max_elements: max_elements || 0,
        incremental: !!incremental,
      });
    },

    get_page_text: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:GetPageText');
    },

    get_page_html: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:GetPageHTML');
    },

    get_accessibility_tree: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:GetAccessibilityTree');
    },

    // --- Interaction ---
    click_element: async ({ tab_id, frame_id, index }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:ClickElement', { index }, null, frame_id);
    },

    click_coordinates: async ({ tab_id, frame_id, x, y, color }, ctx) => {
      if (x === undefined || y === undefined) throw new Error('x and y are required');
      const tab = ctx.resolveTab(tab_id);
      const data = { x, y };
      if (color) data.color = color;
      const result = await actorInteraction(tab, 'ZenRippleAgent:ClickCoordinates', data, null, frame_id);
      // Auto-route: if the click hit an iframe, forward into the iframe's
      // content process with adjusted coordinates.
      if (result && (result.tag === 'iframe' || result.tag === 'frame') && result.iframe_bc_id && result.iframe_rect) {
        const iframeX = x - result.iframe_rect.x;
        const iframeY = y - result.iframe_rect.y;
        // Only route if coordinates are within the iframe bounds
        if (iframeX >= 0 && iframeY >= 0 &&
            iframeX < result.iframe_rect.width && iframeY < result.iframe_rect.height) {
          const iframeResult = await actorInteraction(
            tab, 'ZenRippleAgent:ClickCoordinates',
            { x: iframeX, y: iframeY, color },
            null, result.iframe_bc_id
          );
          if (iframeResult) {
            iframeResult.routed_through_iframe = true;
            iframeResult.iframe_frame_id = result.iframe_bc_id;
            return iframeResult;
          }
        }
      }
      return result;
    },

    fill_field: async ({ tab_id, frame_id, index, value }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      if (value === undefined) throw new Error('value is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:FillField', { index, value: String(value) }, null, frame_id);
    },

    select_option: async ({ tab_id, frame_id, index, value }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      if (value === undefined) throw new Error('value is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:SelectOption', { index, value: String(value) }, null, frame_id);
    },

    type_text: async ({ tab_id, frame_id, text }, ctx) => {
      if (!text) throw new Error('text is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:TypeText', { text }, null, frame_id);
    },

    press_key: async ({ tab_id, frame_id, key, modifiers }, ctx) => {
      if (!key) throw new Error('key is required');
      const mods = modifiers || {};
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:PressKey', { key, modifiers: mods }, { success: true, key }, frame_id);
    },

    scroll: async ({ tab_id, frame_id, direction, amount }, ctx) => {
      if (!direction) throw new Error('direction is required (up/down/left/right)');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:Scroll', { direction, amount: amount || 500 }, null, frame_id);
    },

    hover: async ({ tab_id, frame_id, index }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:Hover', { index }, null, frame_id);
    },

    hover_coordinates: async ({ tab_id, frame_id, x, y, color }, ctx) => {
      if (x === undefined || y === undefined) throw new Error('x and y are required');
      const tab = ctx.resolveTab(tab_id);
      const data = { x, y };
      if (color) data.color = color;
      const result = await actorInteraction(tab, 'ZenRippleAgent:HoverCoordinates', data, null, frame_id);
      // Auto-route: if the hover hit an iframe, forward into the iframe's
      // content process with adjusted coordinates.
      if (result && (result.tag === 'iframe' || result.tag === 'frame') && result.iframe_bc_id && result.iframe_rect) {
        const iframeX = x - result.iframe_rect.x;
        const iframeY = y - result.iframe_rect.y;
        if (iframeX >= 0 && iframeY >= 0 &&
            iframeX < result.iframe_rect.width && iframeY < result.iframe_rect.height) {
          const iframeResult = await actorInteraction(
            tab, 'ZenRippleAgent:HoverCoordinates',
            { x: iframeX, y: iframeY, color },
            null, result.iframe_bc_id
          );
          if (iframeResult) {
            iframeResult.routed_through_iframe = true;
            iframeResult.iframe_frame_id = result.iframe_bc_id;
            return iframeResult;
          }
        }
      }
      return result;
    },

    scroll_at_point: async ({ tab_id, frame_id, x, y, direction, amount }, ctx) => {
      if (x === undefined || y === undefined) throw new Error('x and y are required');
      if (!direction) throw new Error('direction is required (up/down/left/right)');
      const tab = ctx.resolveTab(tab_id);
      const result = await actorInteraction(
        tab, 'ZenRippleAgent:ScrollAtPoint',
        { x, y, direction, amount: amount || 500 },
        null, frame_id
      );
      // Auto-route: if the scroll hit an iframe, forward into the iframe's
      // content process with adjusted coordinates.
      if (result && (result.tag === 'iframe' || result.tag === 'frame') && result.iframe_bc_id && result.iframe_rect) {
        const iframeX = x - result.iframe_rect.x;
        const iframeY = y - result.iframe_rect.y;
        if (iframeX >= 0 && iframeY >= 0 &&
            iframeX < result.iframe_rect.width && iframeY < result.iframe_rect.height) {
          const iframeResult = await actorInteraction(
            tab, 'ZenRippleAgent:ScrollAtPoint',
            { x: iframeX, y: iframeY, direction, amount: amount || 500 },
            null, result.iframe_bc_id
          );
          if (iframeResult) {
            iframeResult.routed_through_iframe = true;
            iframeResult.iframe_frame_id = result.iframe_bc_id;
            return iframeResult;
          }
        }
      }
      return result;
    },

    // --- Console / Eval ---
    console_setup: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:SetupConsoleCapture', {}, null, frame_id);
    },

    console_teardown: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenRippleAgent:TeardownConsoleCapture', {}, null, frame_id);
    },

    console_get_logs: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:GetConsoleLogs');
    },

    console_get_errors: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:GetConsoleErrors');
    },

    console_evaluate: async ({ tab_id, frame_id, expression }, ctx) => {
      if (!expression) throw new Error('expression is required');
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:EvalJS', { expression });
    },

    // --- Clipboard (global) ---
    clipboard_read: async () => {
      try {
        const trans = Cc['@mozilla.org/widget/transferable;1'].createInstance(Ci.nsITransferable);
        trans.init(null);
        trans.addDataFlavor('text/plain');
        Services.clipboard.getData(trans, Ci.nsIClipboard.kGlobalClipboard);
        const data = {};
        const dataLen = {};
        trans.getTransferData('text/plain', data);
        const str = data.value?.QueryInterface(Ci.nsISupportsString);
        return { text: str ? str.data : '' };
      } catch (e) {
        return { text: '', error: e.message };
      }
    },

    clipboard_write: async ({ text }) => {
      if (text === undefined) throw new Error('text is required');
      try {
        const trans = Cc['@mozilla.org/widget/transferable;1'].createInstance(Ci.nsITransferable);
        trans.init(null);
        trans.addDataFlavor('text/plain');
        const str = Cc['@mozilla.org/supports-string;1'].createInstance(Ci.nsISupportsString);
        str.data = text;
        trans.setTransferData('text/plain', str);
        Services.clipboard.setData(trans, null, Ci.nsIClipboard.kGlobalClipboard);
        return { success: true, length: text.length };
      } catch (e) {
        throw new Error('Clipboard write failed: ' + e.message);
      }
    },

    // --- Control ---
    wait: async ({ seconds = 2 }) => {
      await new Promise(r => setTimeout(r, seconds * 1000));
      return { success: true };
    },

    wait_for_element: async ({ tab_id, frame_id, selector, timeout = 10 }, ctx) => {
      if (!selector) throw new Error('selector is required');
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      const deadline = Date.now() + timeout * 1000;
      while (Date.now() < deadline) {
        try {
          const actor = getActorForTab(tab, frame_id);
          const result = await actor.sendQuery('ZenRippleAgent:QuerySelector', { selector });
          if (result.found) return result;
        } catch (e) {
          // Actor might not be available yet during navigation
        }
        await new Promise(r => setTimeout(r, 250));
      }
      return { found: false, timeout: true };
    },

    wait_for_text: async ({ tab_id, frame_id, text, timeout = 10 }, ctx) => {
      if (!text) throw new Error('text is required');
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      const deadline = Date.now() + timeout * 1000;
      while (Date.now() < deadline) {
        try {
          const actor = getActorForTab(tab, frame_id);
          const result = await actor.sendQuery('ZenRippleAgent:SearchText', { text });
          if (result.found) return result;
        } catch (e) {
          // Actor might not be available yet during navigation
        }
        await new Promise(r => setTimeout(r, 250));
      }
      return { found: false, timeout: true };
    },

    wait_for_load: async ({ tab_id, timeout = 15 }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      const browser = tab.linkedBrowser;
      const deadline = Date.now() + timeout * 1000;
      while (browser.webProgress?.isLoadingDocument && Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 200));
      }
      const navEntry = navStatusMap.get(browser) || {};
      return {
        success: true,
        url: browser.currentURI?.spec || '',
        title: tab.label || '',
        loading: browser.webProgress?.isLoadingDocument || false,
        http_status: navEntry.httpStatus || 0,
      };
    },

    // --- Cookies (Phase 7) ---
    get_cookies: async ({ tab_id, url, name }, ctx) => {
      let host;
      let originAttrs = {};
      if (tab_id || !url) {
        const tab = ctx.resolveTab(tab_id);
        if (tab) {
          try {
            host = tab.linkedBrowser.currentURI?.host;
            originAttrs = tab.linkedBrowser.contentPrincipal?.originAttributes || {};
          } catch (e) {}
        }
      }
      if (!host && url) {
        try { host = Services.io.newURI(url).host; } catch (e) { throw new Error('Invalid URL: ' + url); }
      }
      if (!host) throw new Error('No host found — provide url or ensure a tab is active');
      const result = [];
      const cookies = Services.cookies.getCookiesFromHost(host, originAttrs);
      if (cookies) {
        for (const cookie of cookies) {
          if (name && cookie.name !== name) continue;
          let expires = 'session';
          try {
            if (cookie.expiry && cookie.expiry > 0) {
              expires = new Date(cookie.expiry * 1000).toISOString();
            }
          } catch (e) {}
          result.push({
            name: cookie.name,
            value: cookie.value,
            domain: cookie.host,
            path: cookie.path,
            secure: cookie.isSecure,
            httpOnly: cookie.isHttpOnly,
            sameSite: ['none', 'lax', 'strict'][cookie.sameSite] || 'none',
            expires,
          });
        }
      }
      return result;
    },

    set_cookie: async ({ tab_id, frame_id, url, name, value, path, secure, httpOnly, sameSite, expires }, ctx) => {
      if (!name) throw new Error('name is required');
      let cookieStr = encodeURIComponent(name) + '=' + encodeURIComponent(value || '');
      if (path) cookieStr += '; path=' + path;
      if (secure) cookieStr += '; Secure';
      if (httpOnly) cookieStr += '; HttpOnly';
      if (sameSite) cookieStr += '; SameSite=' + sameSite;
      if (expires) {
        const d = typeof expires === 'number'
          ? new Date(expires * 1000)
          : new Date(expires);
        cookieStr += '; expires=' + d.toUTCString();
      }
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:SetCookie', { cookie: cookieStr });
    },

    delete_cookies: async ({ tab_id, url, name }, ctx) => {
      let host;
      let originAttrs = {};
      if (tab_id || !url) {
        const tab = ctx.resolveTab(tab_id);
        if (tab) {
          try {
            host = tab.linkedBrowser.currentURI?.host;
            originAttrs = tab.linkedBrowser.contentPrincipal?.originAttributes || {};
          } catch (e) {}
        }
      }
      if (!host && url) {
        try { host = Services.io.newURI(url).host; } catch (e) { throw new Error('Invalid URL: ' + url); }
      }
      if (!host) throw new Error('No host found — provide url or ensure a tab is active');
      let removed = 0;
      const cookies = Services.cookies.getCookiesFromHost(host, originAttrs);
      const toProcess = cookies ? [...cookies] : [];
      for (const cookie of toProcess) {
        if (name && cookie.name !== name) continue;
        Services.cookies.remove(cookie.host, cookie.name, cookie.path, originAttrs);
        removed++;
      }
      return { success: true, removed };
    },

    // --- Storage (Phase 7) ---
    get_storage: async ({ tab_id, frame_id, storage_type, key }, ctx) => {
      if (!storage_type) throw new Error('storage_type is required (localStorage or sessionStorage)');
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:GetStorage', { storage_type, key });
    },

    set_storage: async ({ tab_id, frame_id, storage_type, key, value }, ctx) => {
      if (!storage_type || !key) throw new Error('storage_type and key are required');
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:SetStorage', { storage_type, key, value: String(value) });
    },

    delete_storage: async ({ tab_id, frame_id, storage_type, key }, ctx) => {
      if (!storage_type) throw new Error('storage_type is required');
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenRippleAgent:DeleteStorage', { storage_type, key });
    },

    // --- Network Monitoring (Phase 7, global) ---
    network_monitor_start: async () => {
      ensureNetworkObserver();
      networkMonitorActive = true;
      return { success: true, note: 'Network monitoring started' };
    },

    network_monitor_stop: async () => {
      networkMonitorActive = false;
      return { success: true, note: 'Network monitoring stopped' };
    },

    network_get_log: async ({ url_filter, method_filter, status_filter, limit }) => {
      let entries = [...networkLog];
      if (url_filter) {
        if (url_filter.length > 1000) throw new Error('url_filter too long: max 1000 characters');
        let re;
        try { re = new RegExp(url_filter, 'i'); } catch (e) {
          throw new Error('Invalid regex in url_filter: ' + e.message);
        }
        entries = entries.filter(e => re.test(e.url));
      }
      if (method_filter) {
        const m = method_filter.toUpperCase();
        entries = entries.filter(e => e.method === m);
      }
      if (status_filter !== undefined && status_filter !== null) {
        entries = entries.filter(e => e.status === status_filter);
      }
      if (limit) entries = entries.slice(-limit);
      return entries;
    },

    // --- Request Interception (Phase 7, global) ---
    intercept_add_rule: async ({ pattern, action, headers }, ctx) => {
      if (!pattern || !action) throw new Error('pattern and action are required');
      if (!['block', 'modify_headers'].includes(action)) {
        throw new Error('action must be "block" or "modify_headers"');
      }
      // Validate regex pattern to prevent ReDoS — reject overly long patterns
      if (pattern.length > 1000) {
        throw new Error('Pattern too long: max 1000 characters');
      }
      ensureNetworkObserver();
      let compiled;
      try {
        compiled = new RegExp(pattern, 'i');
      } catch (e) {
        throw new Error('Invalid regex pattern: ' + e.message);
      }
      const normalizedHeaders = headers || {};
      // Duplicate detection is session-scoped — only dedupe within this session
      const existing = interceptRules.find(r =>
        r.sessionId === ctx.session.id &&
        r.pattern.source === compiled.source &&
        r.action === action &&
        JSON.stringify(r.headers || {}) === JSON.stringify(normalizedHeaders)
      );
      if (existing) {
        return { success: true, rule_id: existing.id, duplicate: true };
      }
      if (interceptRules.length >= MAX_INTERCEPT_RULES) {
        throw new Error('Too many interception rules: max ' + MAX_INTERCEPT_RULES);
      }
      const id = interceptNextId++;
      interceptRules.push({
        id,
        sessionId: ctx.session.id,
        pattern: compiled,
        action,
        headers: normalizedHeaders,
      });
      return { success: true, rule_id: id };
    },

    intercept_remove_rule: async ({ rule_id }, ctx) => {
      if (!rule_id) throw new Error('rule_id is required');
      const idx = interceptRules.findIndex(r => r.id === rule_id);
      if (idx === -1) throw new Error('Rule not found: ' + rule_id);
      const rule = interceptRules[idx];
      if (rule.sessionId && rule.sessionId !== ctx.session.id) {
        throw new Error('Cannot remove rule ' + rule_id + ': owned by another session');
      }
      interceptRules.splice(idx, 1);
      return { success: true };
    },

    intercept_list_rules: async (params, ctx) => {
      return interceptRules.map(r => ({
        id: r.id,
        pattern: r.pattern.source,
        action: r.action,
        headers: r.headers,
        own: r.sessionId === ctx.session.id,
      }));
    },

    // --- Session Persistence (Phase 7) — scoped to session ---
    // Note: session_save/session_restore does not preserve claimed-vs-created
    // distinction. Restored tabs are always treated as newly created tabs and
    // will be closed on session destroy (not released back to unclaimed).
    session_save: async ({ file_path }, ctx) => {
      if (!file_path) throw new Error('file_path is required');
      const tabs = getSessionTabs(ctx.session.id);
      const tabData = tabs.map(t => ({
        url: t.linkedBrowser?.currentURI?.spec || 'about:blank',
        title: t.label || '',
      }));
      // Collect cookies for all tab domains
      const domains = new Set();
      for (const td of tabData) {
        try { domains.add(Services.io.newURI(td.url).host); } catch (e) {}
      }
      const cookieData = [];
      const tabOriginAttrs = new Map();
      for (const t of tabs) {
        try {
          const h = t.linkedBrowser.currentURI?.host;
          if (h) tabOriginAttrs.set(h, t.linkedBrowser.contentPrincipal?.originAttributes || {});
        } catch (e) {}
      }
      for (const host of domains) {
        const attrs = tabOriginAttrs.get(host) || {};
        const hostCookies = Services.cookies.getCookiesFromHost(host, attrs);
        const cookieList = hostCookies ? [...hostCookies] : [];
        for (const cookie of cookieList) {
          cookieData.push({
            host: cookie.host,
            name: cookie.name,
            value: cookie.value,
            path: cookie.path,
            secure: cookie.isSecure,
            httpOnly: cookie.isHttpOnly,
            sameSite: cookie.sameSite,
            expiry: cookie.expiry,
          });
        }
      }
      const sessionData = { tabs: tabData, cookies: cookieData, saved_at: new Date().toISOString() };
      const json = JSON.stringify(sessionData, null, 2);
      const encoder = new TextEncoder();
      await IOUtils.write(file_path, encoder.encode(json));
      return { success: true, tabs: tabData.length, cookies: cookieData.length, file: file_path };
    },

    session_restore: async ({ file_path }, ctx) => {
      if (!file_path) throw new Error('file_path is required');
      const bytes = await IOUtils.read(file_path);
      const json = new TextDecoder().decode(bytes);
      const sessionData = JSON.parse(json);
      // Restore cookies
      let cookiesRestored = 0;
      if (sessionData.cookies) {
        for (const c of sessionData.cookies) {
          try {
            const schemeType = Ci.nsICookie?.SCHEME_UNSET ?? 0;
            Services.cookies.add(
              c.host, c.path, c.name, c.value,
              c.secure, c.httpOnly, !c.expiry, c.expiry || 0, {},
              c.sameSite || 0, schemeType
            );
            cookiesRestored++;
          } catch (e) {
            log('Cookie restore failed: ' + c.name + ' — ' + e);
          }
        }
      }
      // Restore tabs into current session
      const wsId = await ensureAgentWorkspace();
      let tabsRestored = 0;
      let tabsSkipped = 0;
      const existingTabs = getSessionTabCount(ctx.session.id);
      const remainingCapacity = Math.max(0, MAX_SESSION_TABS - existingTabs);
      if (sessionData.tabs) {
        for (const td of sessionData.tabs) {
          if (!td.url || td.url === 'about:blank') continue;
          if (tabsRestored >= remainingCapacity) {
            tabsSkipped++;
            continue;
          }
          const tab = gBrowser.addTab(td.url, {
            triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()
          });
          const stableId = tab.linkedPanel || ('agent-tab-' + Date.now() + '-' + tabsRestored);
          tab.setAttribute('data-agent-tab-id', stableId);
          tab.setAttribute('data-agent-session-id', ctx.session.id);
          tab.setAttribute('data-agent-indicator', 'claimed');
          stampSessionColor(tab, ctx.session);
          if (ctx.session.name) updateTabSublabel(tab, ctx.session.name);
          ctx.session.agentTabs.add(tab);
          if (wsId && gZenWorkspaces) {
            gZenWorkspaces.moveTabToWorkspace(tab, wsId);
          }
          tabsRestored++;
        }
      }
      groupSessionTabs(ctx.session.id);
      return {
        success: true,
        tabs_restored: tabsRestored,
        tabs_skipped: tabsSkipped,
        tab_limit: MAX_SESSION_TABS,
        cookies_restored: cookiesRestored
      };
    },

    // --- Multi-Tab Coordination (Phase 9) ---
    compare_tabs: async ({ tab_ids }, ctx) => {
      if (!tab_ids || !Array.isArray(tab_ids) || tab_ids.length < 2) {
        throw new Error('tab_ids must be an array of at least 2 tab IDs');
      }
      const results = [];
      for (const tid of tab_ids) {
        const tab = ctx.resolveTab(tid);
        if (!tab) {
          results.push({ tab_id: tid, error: 'Tab not found' });
          continue;
        }
        const url = tab.linkedBrowser?.currentURI?.spec || '';
        const title = tab.label || '';
        let textPreview = '';
        try {
          const actor = getActorForTab(tab);
          const page = await actor.sendQuery('ZenRippleAgent:GetPageText');
          textPreview = (page.text || '').substring(0, 500);
        } catch (e) {
          textPreview = '(unable to get text: ' + e.message + ')';
        }
        results.push({ tab_id: tid, url, title, text_preview: textPreview });
      }
      return results;
    },

    batch_navigate: async ({ urls, persist }, ctx) => {
      if (!urls || !Array.isArray(urls) || urls.length === 0) {
        throw new Error('urls must be a non-empty array');
      }
      ensureSessionCanOpenTabs(ctx.session, urls.length);
      const wsId = await ensureAgentWorkspace();
      const opened = [];
      for (const url of urls) {
        const tab = gBrowser.addTab(url, {
          triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()
        });
        const stableId = tab.linkedPanel || ('agent-tab-' + Date.now() + '-' + opened.length);
        tab.setAttribute('data-agent-tab-id', stableId);
        tab.setAttribute('data-agent-session-id', ctx.session.id);
        tab.setAttribute('data-agent-indicator', 'claimed');
        stampSessionColor(tab, ctx.session);
        if (ctx.session.name) updateTabSublabel(tab, ctx.session.name);
        ctx.session.agentTabs.add(tab);
        if (persist) ctx.session.claimedTabs.add(tab);
        if (wsId && gZenWorkspaces) {
          gZenWorkspaces.moveTabToWorkspace(tab, wsId);
        }
        opened.push({ tab_id: stableId, url });
      }
      groupSessionTabs(ctx.session.id);
      return { success: true, tabs: opened, persist: !!persist };
    },

    // --- Action Recording (Phase 9, per-session) ---
    record_start: async (params, ctx) => {
      ctx.session.recordingActive = true;
      ctx.session.recordedActions.length = 0;
      return { success: true, note: 'Recording started' };
    },

    record_stop: async (params, ctx) => {
      ctx.session.recordingActive = false;
      return { success: true, actions: ctx.session.recordedActions.length };
    },

    record_save: async ({ file_path }, ctx) => {
      if (!file_path) throw new Error('file_path is required');
      const data = {
        actions: ctx.session.recordedActions,
        recorded_at: new Date().toISOString(),
        count: ctx.session.recordedActions.length,
      };
      const json = JSON.stringify(data, null, 2);
      const encoder = new TextEncoder();
      await IOUtils.write(file_path, encoder.encode(json));
      return { success: true, file: file_path, actions: ctx.session.recordedActions.length };
    },

    record_replay: async ({ file_path, delay }, ctx) => {
      if (!file_path) throw new Error('file_path is required');
      const bytes = await IOUtils.read(file_path);
      const json = new TextDecoder().decode(bytes);
      const data = JSON.parse(json);
      const actions = data.actions || [];
      if (actions.length === 0) {
        return { success: true, replayed: 0, note: 'No actions to replay' };
      }
      const delayMs = (delay || 0.5) * 1000;
      let replayed = 0;
      let errors = [];
      for (const action of actions) {
        try {
          const handler = commandHandlers[action.method];
          if (!handler) throw new Error('Unknown method: ' + action.method);
          await handler(action.params || {}, ctx);
          replayed++;
        } catch (e) {
          errors.push({ method: action.method, error: e.message });
        }
        if (delayMs > 0) {
          await new Promise(r => setTimeout(r, delayMs));
        }
      }
      return { success: true, replayed, total: actions.length, errors: errors.length > 0 ? errors : undefined };
    },

    // --- Config (Firefox prefs under zenripple.*) ---
    get_config: async ({ key }) => {
      if (!key) throw new Error('key is required');
      const prefKey = 'zenripple.' + key.replace(/[^a-zA-Z0-9_.\-]/g, '');
      try {
        return { key, value: Services.prefs.getStringPref(prefKey, '') };
      } catch (e) {
        return { key, value: '' };
      }
    },

    set_config: async ({ key, value }) => {
      if (!key) throw new Error('key is required');
      const prefKey = 'zenripple.' + key.replace(/[^a-zA-Z0-9_.\-]/g, '');
      Services.prefs.setStringPref(prefKey, String(value || ''));
      return { success: true, key };
    },

    // --- Chrome-Context Eval (Phase 10) ---
    eval_chrome: async ({ expression }) => {
      if (!expression) throw new Error('expression is required');
      const sandbox = Cu.Sandbox(Services.scriptSecurityManager.getSystemPrincipal(), {
        wantComponents: true,
        sandboxPrototype: window,
      });
      sandbox.Services = Services;
      sandbox.gBrowser = gBrowser;
      sandbox.Cc = Cc;
      sandbox.Ci = Ci;
      sandbox.Cu = Cu;
      sandbox.IOUtils = IOUtils;
      try {
        const result = Cu.evalInSandbox(expression, sandbox);
        return { result: formatChromeResult(result) };
      } catch (e) {
        return { error: e.message, stack: e.stack || '' };
      } finally {
        // Immediately destroy sandbox compartment to prevent memory accumulation
        try { Cu.nukeSandbox(sandbox); } catch (_) {}
      }
    },

    // --- Drag-and-Drop (Phase 10) ---
    drag_element: async ({ tab_id, frame_id, sourceIndex, targetIndex, steps }, ctx) => {
      if (sourceIndex === undefined || sourceIndex === null) throw new Error('sourceIndex is required');
      if (targetIndex === undefined || targetIndex === null) throw new Error('targetIndex is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(
        tab, 'ZenRippleAgent:DragElement',
        { sourceIndex, targetIndex, steps: steps || 10 },
        null, frame_id
      );
    },

    drag_coordinates: async ({ tab_id, frame_id, startX, startY, endX, endY, steps }, ctx) => {
      if (startX === undefined || startY === undefined) throw new Error('startX and startY are required');
      if (endX === undefined || endY === undefined) throw new Error('endX and endY are required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(
        tab, 'ZenRippleAgent:DragCoordinates',
        { startX, startY, endX, endY, steps: steps || 10 },
        null, frame_id
      );
    },

    // --- File Upload (Phase 11) ---
    file_upload: async ({ tab_id, frame_id, index, file_path }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      if (!file_path) throw new Error('file_path is required');
      const exists = await IOUtils.exists(file_path);
      if (!exists) throw new Error('File not found: ' + file_path);

      // Guard against OOM from huge files (base64 + JSON transport overhead is substantial)
      const stat = await IOUtils.stat(file_path);
      if (stat.size > MAX_UPLOAD_SIZE) {
        throw new Error('File too large: ' + stat.size + ' bytes (max ' + MAX_UPLOAD_SIZE + ')');
      }

      let bytes = await IOUtils.read(file_path);
      const CHUNK = 8192;
      const chunks = [];
      for (let i = 0; i < bytes.length; i += CHUNK) {
        chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK)));
      }
      let binaryStr = chunks.join('');
      chunks.length = 0;
      let base64 = btoa(binaryStr);
      binaryStr = '';
      if (base64.length > MAX_UPLOAD_BASE64_LENGTH) {
        throw new Error('Encoded file payload too large: ' + base64.length + ' bytes');
      }

      const filename = PathUtils.filename(file_path);
      const ext = filename.split('.').pop().toLowerCase();
      const mimeMap = {
        jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', gif: 'image/gif',
        webp: 'image/webp', svg: 'image/svg+xml', bmp: 'image/bmp',
        pdf: 'application/pdf', txt: 'text/plain', csv: 'text/csv',
        json: 'application/json', xml: 'application/xml',
        zip: 'application/zip', gz: 'application/gzip',
        doc: 'application/msword', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        xls: 'application/vnd.ms-excel', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      };
      const mimeType = mimeMap[ext] || 'application/octet-stream';

      const tab = ctx.resolveTab(tab_id);
      try {
        return await actorInteraction(
          tab, 'ZenRippleAgent:FileUpload',
          { index, base64, filename, mimeType }, null, frame_id
        );
      } finally {
        // Drop large temporary allocations as soon as possible.
        base64 = '';
        bytes = null;
      }
    },

    // --- Wait for Download (Phase 11, global) ---
    wait_for_download: async ({ timeout = 60, save_to }) => {
      const dl = await getDownloads();
      const list = await dl.getList(dl.ALL);

      return new Promise((resolve) => {
        let resolved = false;
        let timeoutId;

        const view = {
          onDownloadChanged(download) {
            if (resolved) return;

            if (download.succeeded) {
              resolved = true;
              clearTimeout(timeoutId);
              list.removeView(view);

              (async () => {
                let finalPath = download.target.path;
                if (save_to) {
                  try {
                    await IOUtils.copy(download.target.path, save_to);
                    finalPath = save_to;
                  } catch (e) {
                    resolve({
                      success: true, file_path: download.target.path,
                      save_to_error: e.message,
                      file_name: PathUtils.filename(download.target.path),
                      file_size: download.totalBytes || 0,
                      content_type: download.contentType || '',
                    });
                    return;
                  }
                }
                resolve({
                  success: true, file_path: finalPath,
                  file_name: PathUtils.filename(finalPath),
                  file_size: download.totalBytes || 0,
                  content_type: download.contentType || '',
                });
              })();
            } else if (download.error) {
              resolved = true;
              clearTimeout(timeoutId);
              list.removeView(view);
              resolve({
                success: false,
                error: download.error.message || 'Download failed',
                file_path: download.target?.path || '',
              });
            }
          },
        };

        list.addView(view);

        timeoutId = setTimeout(() => {
          if (resolved) return;
          resolved = true;
          list.removeView(view);
          resolve({
            success: false,
            error: 'Timeout: no download completed within ' + timeout + 's',
            timeout: true,
          });
        }, timeout * 1000);
      });
    },

    // --- Session Management (Phase 12) ---
    session_info: async (params, ctx) => {
      return {
        session_id: ctx.session.id,
        name: ctx.session.name,
        color_index: ctx.session.colorIndex,
        workspace_name: AGENT_WORKSPACE_NAME,
        workspace_id: agentWorkspaceId,
        connection_id: ctx.connection.connectionId,
        connection_count: ctx.session.connections.size,
        tab_count: getSessionTabs(ctx.session.id).length,
        claimed_tab_count: ctx.session.claimedTabs.size,
        created_at: ctx.session.createdAt,
      };
    },

    session_close: async (params, ctx) => {
      const sessionId = ctx.session.id;
      const tabCount = getSessionTabs(sessionId).length;
      const claimedCount = ctx.session.claimedTabs.size;
      // Defer destruction so this response is sent first
      setTimeout(() => destroySession(sessionId), 50);
      return {
        success: true,
        session_id: sessionId,
        tabs_closed: Math.max(0, tabCount - claimedCount),
        tabs_released: claimedCount,
      };
    },

    list_sessions: async () => {
      const result = [];
      for (const [id, session] of sessions) {
        result.push({
          session_id: id,
          name: session.name,
          color_index: session.colorIndex,
          workspace_name: AGENT_WORKSPACE_NAME,
          connection_count: session.connections.size,
          tab_count: getSessionTabs(id).length,
          created_at: session.createdAt,
        });
      }
      return result;
    },

    set_session_name: async ({ name }, ctx) => {
      if (typeof name !== 'string') {
        throw new Error('name must be a string');
      }
      const trimmed = name.trim();
      // Empty string clears the session name
      if (trimmed.length === 0) {
        ctx.session.name = null;
        for (const tab of getSessionTabs(ctx.session.id)) {
          updateTabSublabel(tab, null);
        }
        log('Session ' + ctx.session.id + ' name cleared');
        const otherNames = [];
        for (const [id, session] of sessions) {
          if (id !== ctx.session.id && session.name) otherNames.push(session.name);
        }
        return { name: null, other_session_names: otherNames };
      }
      if (trimmed.length > MAX_SESSION_NAME_LENGTH) {
        throw new Error('name must be at most ' + MAX_SESSION_NAME_LENGTH + ' characters');
      }
      // Strip control characters (keep printable Unicode + spaces)
      const sanitized = trimmed.replace(/[\x00-\x1f\x7f-\x9f]/g, '');
      if (sanitized.length === 0) {
        throw new Error('name must contain printable characters');
      }
      ctx.session.name = sanitized;
      // Update sublabel on all existing session tabs
      for (const tab of getSessionTabs(ctx.session.id)) {
        updateTabSublabel(tab, sanitized);
      }
      // Return other session names so the caller can pick a unique name
      const otherNames = [];
      for (const [id, session] of sessions) {
        if (id !== ctx.session.id && session.name) {
          otherNames.push(session.name);
        }
      }
      log('Session ' + ctx.session.id + ' named: ' + sanitized);
      return {
        name: sanitized,
        other_session_names: otherNames,
      };
    },

    // --- Tab Claiming (workspace-wide visibility) ---
    list_workspace_tabs: async (params, ctx) => {
      await ensureAgentWorkspace();
      const wsTabs = getWorkspaceTabs();
      return wsTabs.map(tab => {
        const sessionId = tab.getAttribute('data-agent-session-id') || null;
        const tabId = tab.getAttribute('data-agent-tab-id') || tab.linkedPanel || '';
        const ownership = getTabOwnership(tab);
        const isMine = sessionId === ctx.session.id;

        const entry = {
          tab_id: tabId,
          title: tab.label || '',
          url: tab.linkedBrowser?.currentURI?.spec || '',
          ownership,         // 'unclaimed', 'owned', or 'stale'
          is_mine: isMine,   // true if owned by the calling session
        };

        // Include owner session ID for owned/stale tabs (for transparency)
        if (sessionId && !isMine) {
          entry.owner_session_id = sessionId;
        }

        // Surface claimed status for calling session's tabs
        if (isMine) {
          entry.claimed = ctx.session.claimedTabs.has(tab);
        }

        return entry;
      }).filter(t => t.tab_id || t.url);
    },

    claim_tab: async ({ tab_id }, ctx) => {
      if (!tab_id) throw new Error('tab_id is required');

      // Ensure workspace exists before searching (prevents fallback to all tabs)
      await ensureAgentWorkspace();
      // Search ALL workspace tabs (not just session-scoped)
      const wsTabs = getWorkspaceTabs();
      let targetTab = null;
      for (const tab of wsTabs) {
        const id = tab.getAttribute('data-agent-tab-id') || tab.linkedPanel || '';
        if (id === tab_id) {
          targetTab = tab;
          break;
        }
      }
      // Fallback: match by URL
      if (!targetTab) {
        for (const tab of wsTabs) {
          if (tab.linkedBrowser?.currentURI?.spec === tab_id) {
            targetTab = tab;
            break;
          }
        }
      }
      if (!targetTab) throw new Error('Tab not found in workspace: ' + tab_id);
      if (!targetTab.linkedBrowser) throw new Error('Tab has no linked browser');

      const ownership = getTabOwnership(targetTab);
      const existingSessionId = targetTab.getAttribute('data-agent-session-id') || null;

      // Already owned by this session
      if (existingSessionId === ctx.session.id) {
        return {
          success: true,
          tab_id: targetTab.getAttribute('data-agent-tab-id') || targetTab.linkedPanel,
          url: targetTab.linkedBrowser?.currentURI?.spec || '',
          title: targetTab.label || '',
          already_owned: true,
        };
      }

      // Reject claiming actively-owned tabs from other sessions
      if (ownership === 'owned') {
        throw new Error(
          'Tab is actively owned by session ' + existingSessionId +
          '. Cannot claim tabs from active sessions.'
        );
      }

      // ownership is 'unclaimed' or 'stale' — proceed with claiming
      ensureSessionCanOpenTabs(ctx.session, 1);

      // Remove from previous session's tracking if it was stale
      if (existingSessionId) {
        const prevSession = sessions.get(existingSessionId);
        if (prevSession) {
          prevSession.agentTabs.delete(targetTab);
          prevSession.claimedTabs.delete(targetTab);
          prevSession.pushTabEvent({
            type: 'tab_claimed_away',
            tab_id: targetTab.getAttribute('data-agent-tab-id') || targetTab.linkedPanel,
            claimed_by_session: ctx.session.id,
            timestamp: new Date().toISOString(),
          });
        }
      }

      // Stamp with new session ownership
      const stableId = targetTab.getAttribute('data-agent-tab-id') || targetTab.linkedPanel || ('agent-tab-' + Date.now());
      targetTab.setAttribute('data-agent-tab-id', stableId);
      targetTab.setAttribute('data-agent-session-id', ctx.session.id);
      targetTab.setAttribute('data-agent-indicator', 'claimed');
      stampSessionColor(targetTab, ctx.session);
      if (ctx.session.name) updateTabSublabel(targetTab, ctx.session.name);
      ctx.session.agentTabs.add(targetTab);
      ctx.session.claimedTabs.add(targetTab);

      // Ensure tab is in the agent workspace (agentWorkspaceId already set by
      // the ensureAgentWorkspace() call at the top of this handler)
      if (agentWorkspaceId && gZenWorkspaces) {
        gZenWorkspaces.moveTabToWorkspace(targetTab, agentWorkspaceId);
      }

      groupSessionTabs(ctx.session.id);
      ctx.connection.currentAgentTab = targetTab;

      ctx.session.pushTabEvent({
        type: 'tab_claimed',
        tab_id: stableId,
        previous_owner: existingSessionId,
        was_stale: ownership === 'stale',
        timestamp: new Date().toISOString(),
      });

      log('Tab claimed: ' + stableId + ' [' + ownership + '] -> session:' + ctx.session.id.substring(0, 8));

      return {
        success: true,
        tab_id: stableId,
        url: targetTab.linkedBrowser?.currentURI?.spec || '',
        title: targetTab.label || '',
        persist: true,  // Claimed tabs always survive session destruction
        previous_owner: existingSessionId,
        was_stale: ownership === 'stale',
      };
    },
  };

  // ============================================
  // ACTOR REGISTRATION
  // ============================================

  const ACTOR_GLOBAL_KEY = '__zenrippleActorsRegistered';

  function registerActors() {
    // Actors are browser-global — only register once across all windows
    if (globalThis[ACTOR_GLOBAL_KEY]) {
      log('Actors already registered');
      return;
    }

    try {
      // file:// is NOT a trusted scheme for actor modules.
      // Register a resource:// substitution so Firefox trusts the URIs.
      const actorsDir = Services.dirsvc.get('UChrm', Ci.nsIFile);
      actorsDir.append('JS');
      actorsDir.append('actors');

      const resProto = Services.io
        .getProtocolHandler('resource')
        .QueryInterface(Ci.nsIResProtocolHandler);
      resProto.setSubstitution('zenripple-agent', Services.io.newFileURI(actorsDir));
      log('Registered resource://zenripple-agent/ -> ' + actorsDir.path);

      const parentURI = 'resource://zenripple-agent/ZenRippleAgentParent.sys.mjs';
      const childURI = 'resource://zenripple-agent/ZenRippleAgentChild.sys.mjs';

      ChromeUtils.registerWindowActor('ZenRippleAgent', {
        parent: { esModuleURI: parentURI },
        child: { esModuleURI: childURI },
        allFrames: true,
        matches: ['*://*/*'],
      });

      globalThis[ACTOR_GLOBAL_KEY] = true;
      log('JSWindowActor ZenRippleAgent registered');
    } catch (e) {
      if (String(e).includes('NotSupportedError') || String(e).includes('already been registered')) {
        // Already registered by another window — expected under fx-autoconfig
        globalThis[ACTOR_GLOBAL_KEY] = true;
        log('Actors already registered (caught re-registration)');
      } else {
        log('Actor registration failed: ' + e);
      }
    }
  }

  // ============================================
  // SESSION REPLAY VIEWER (Ctrl+Shift+E)
  // ============================================

  const REPLAY_MODAL_ID = 'zenripple-replay-modal';
  const REPLAY_STYLE_ID = 'zenripple-replay-styles';

  const REPLAY_VIEWER_CSS = `
/* === ZenRipple Session Replay Viewer === */

@keyframes zenripple-modal-enter {
  from { opacity: 0; transform: scale(0.96) translateY(-8px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

@keyframes zr-play-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

@keyframes zr-progress-glow {
  0% { box-shadow: 0 0 4px var(--zr-accent); }
  100% { box-shadow: 0 0 8px var(--zr-accent), 0 0 2px var(--zr-accent); }
}

#zenripple-replay-modal {
  --zr-bg-surface: var(--zl-bg-surface, #1e1e2e);
  --zr-bg-raised: var(--zl-bg-raised, #2a2a3e);
  --zr-bg-elevated: var(--zl-bg-elevated, #363650);
  --zr-bg-hover: var(--zl-bg-hover, rgba(255,255,255,0.04));
  --zr-text-primary: var(--zl-text-primary, #e0e0e6);
  --zr-text-secondary: var(--zl-text-secondary, #a0a0b0);
  --zr-text-muted: var(--zl-text-muted, #6b6b80);
  --zr-accent: var(--zl-accent, #7aa2f7);
  --zr-accent-dim: var(--zl-accent-dim, rgba(122,162,247,0.1));
  --zr-accent-20: var(--zl-accent-20, rgba(122,162,247,0.2));
  --zr-border-subtle: var(--zl-border-subtle, rgba(255,255,255,0.08));
  --zr-border-default: var(--zl-border-default, rgba(255,255,255,0.12));
  --zr-border-strong: var(--zl-border-strong, rgba(255,255,255,0.18));
  --zr-r-xl: var(--zl-r-xl, 20px);
  --zr-r-md: var(--zl-r-md, 10px);
  --zr-r-sm: var(--zl-r-sm, 6px);
  --zr-font-mono: var(--zl-font-mono, 'SF Mono', 'Fira Code', 'Cascadia Code', monospace);
  --zr-font-ui: var(--zl-font-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif);
  --zr-shadow-modal: var(--zl-shadow-modal, 0 24px 80px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.06));
  --zr-success: var(--zl-success, #a6e3a1);
  --zr-error: var(--zl-error, #f38ba8);

  position: fixed;
  inset: 0;
  z-index: 100005;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  font-family: var(--zr-font-ui);
  color: var(--zr-text-primary);
}

#zenripple-replay-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,0.55);
  backdrop-filter: blur(14px);
}

#zenripple-replay-container {
  position: relative;
  width: min(96%, 1920px);
  height: 88vh;
  max-height: 980px;
  background: var(--zr-bg-surface);
  border-radius: var(--zr-r-xl);
  box-shadow: var(--zr-shadow-modal);
  border: 1px solid var(--zr-border-subtle);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: zenripple-modal-enter 0.28s cubic-bezier(0.16, 1, 0.3, 1) both;
}

/* ── Header ── */
.zenripple-replay-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--zr-border-subtle);
  flex-shrink: 0;
}

.zenripple-replay-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--zr-text-primary);
  letter-spacing: 0.02em;
}

.zenripple-replay-session-badge {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  font-weight: 600;
  color: var(--zr-accent);
  background: var(--zr-accent-dim);
  padding: 2px 8px;
  border-radius: var(--zr-r-sm);
  letter-spacing: 0.02em;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.zenripple-replay-close {
  margin-left: auto;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--zr-r-sm);
  color: var(--zr-text-muted);
  cursor: pointer;
  transition: all 0.12s;
  font-size: 16px;
  line-height: 1;
  border: none;
  background: none;
  -moz-appearance: none;
}

.zenripple-replay-close:hover {
  background: var(--zr-bg-hover);
  color: var(--zr-text-primary);
}

/* ── Main Content (3-panel) ── */
.zenripple-replay-body {
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* Main wrapper: screenshot + details.  flex-direction flips in narrow mode. */
.zenripple-replay-main {
  flex: 1;
  display: flex;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

/* ── Splitters ── */
.zenripple-replay-splitter {
  flex-shrink: 0;
  background: var(--zr-border-subtle);
  position: relative;
  z-index: 2;
  transition: background 0.12s;
}
.zenripple-replay-splitter-v {
  width: 1px;
  cursor: col-resize;
}
.zenripple-replay-splitter-v::after {
  content: '';
  position: absolute;
  inset: 0 -3px;
}
.zenripple-replay-splitter-h {
  height: 1px;
  cursor: row-resize;
}
.zenripple-replay-splitter-h::after {
  content: '';
  position: absolute;
  inset: -3px 0;
}
.zenripple-replay-splitter:hover,
.zenripple-replay-splitter.dragging {
  background: var(--zr-accent);
}

/* ── Narrow mode ── */
.zenripple-narrow .zenripple-replay-main {
  flex-direction: column;
}
/* Inner splitter flips to horizontal in narrow mode */
.zenripple-narrow .zenripple-replay-splitter-inner {
  width: auto;
  height: 1px;
  cursor: row-resize;
}
.zenripple-narrow .zenripple-replay-splitter-inner::after {
  inset: -3px 0;
}

/* Left: Screenshot viewer — absolute positioning ensures the image
   never overflows even when the natural image is much taller than the
   container.  The 16px inset gives a visible margin + room for the
   border-radius to show. */
.zenripple-replay-screenshot {
  flex: 2;
  min-width: 0;
  min-height: 0;
  position: relative;
  background: var(--zr-bg-raised);
  overflow: hidden;
}

.zenripple-replay-screenshot img {
  position: absolute;
  inset: 16px;
  width: calc(100% - 32px);
  height: calc(100% - 32px);
  object-fit: contain;
  border-radius: var(--zr-r-md);
  border: 1px solid var(--zr-border-default);
}

/* Center placeholder text when no screenshot is available */
.zenripple-replay-screenshot .zenripple-replay-no-screenshot {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.zenripple-replay-no-screenshot {
  color: var(--zr-text-muted);
  font-size: 12px;
  font-style: italic;
}

/* Center: Tool call details */
.zenripple-replay-details {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.zenripple-replay-detail-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 14px;
}

.zenripple-replay-tool-name {
  font-family: var(--zr-font-mono);
  font-size: 14px;
  font-weight: 700;
  color: var(--zr-accent);
  margin-bottom: 10px;
  word-break: break-all;
}

.zenripple-replay-meta {
  display: flex;
  gap: 6px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.zenripple-replay-meta-item {
  font-family: var(--zr-font-mono);
  font-size: 11px;
  font-weight: 600;
  color: var(--zr-text-secondary);
  background: var(--zr-bg-elevated);
  padding: 2px 7px;
  border-radius: var(--zr-r-sm);
  letter-spacing: 0.02em;
}

.zenripple-replay-meta-item.error {
  color: var(--zr-error);
  background: rgba(243,139,168,0.1);
}

.zenripple-replay-section-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--zr-text-muted);
  margin: 12px 0 6px 0;
}

.zenripple-replay-json {
  font-family: var(--zr-font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--zr-text-secondary);
  background: var(--zr-bg-raised);
  border: 1px solid var(--zr-border-subtle);
  border-radius: var(--zr-r-sm);
  padding: 10px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 240px;
  overflow-y: auto;
}

.zenripple-replay-json.zr-result-json {
  max-height: 400px;
}

.zenripple-replay-json .zr-key { color: #89b4fa; }
.zenripple-replay-json .zr-str { color: #a6e3a1; }
.zenripple-replay-json .zr-num { color: #fab387; }
.zenripple-replay-json .zr-bool { color: #cba6f7; }
.zenripple-replay-json .zr-null { color: #6b6b80; }

/* Right: Tool call list */
.zenripple-replay-list {
  flex: 0 0 280px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.zenripple-replay-list-header {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--zr-text-muted);
  padding: 12px 14px 8px;
  border-bottom: 1px solid var(--zr-border-subtle);
  flex-shrink: 0;
}

.zenripple-replay-list-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 4px 6px;
}

.zenripple-replay-entry {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px;
  border-radius: var(--zr-r-sm);
  cursor: pointer;
  transition: background 0.1s;
  min-height: 32px;
}

.zenripple-replay-entry:hover {
  background: var(--zr-bg-hover);
}

.zenripple-replay-entry.selected {
  background: var(--zr-accent-dim);
}

.zenripple-replay-entry.selected .zenripple-replay-entry-name {
  color: var(--zr-accent);
}

.zenripple-replay-entry-seq {
  font-family: var(--zr-font-mono);
  font-size: 9px;
  font-weight: 600;
  color: var(--zr-text-muted);
  min-width: 20px;
  text-align: right;
  flex-shrink: 0;
}

.zenripple-replay-entry-col {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  gap: 1px;
}

.zenripple-replay-entry-name {
  font-family: var(--zr-font-mono);
  font-size: 12px;
  font-weight: 500;
  color: var(--zr-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.zenripple-replay-entry-subtitle {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  color: var(--zr-text-secondary);
  opacity: 0.65;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.3;
}

.zenripple-replay-entry.selected .zenripple-replay-entry-subtitle {
  opacity: 0.9;
}

.zenripple-replay-entry-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--zr-success);
  flex-shrink: 0;
}

.zenripple-replay-entry-dot.error {
  background: var(--zr-error);
}

.zenripple-replay-entry-time {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  color: var(--zr-text-muted);
  flex-shrink: 0;
}

/* ── Footer / Transport Bar ── */
.zenripple-replay-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 20px;
  border-top: 1px solid var(--zr-border-subtle);
  flex-shrink: 0;
}

.zenripple-replay-transport {
  display: flex;
  align-items: center;
  gap: 6px;
}

.zenripple-replay-transport-btn {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--zr-r-sm);
  color: var(--zr-text-muted);
  cursor: pointer;
  border: none;
  background: none;
  -moz-appearance: none;
  transition: all 0.12s;
  font-size: 12px;
  padding: 0;
}

.zenripple-replay-transport-btn:hover {
  background: var(--zr-bg-hover);
  color: var(--zr-text-primary);
}

.zenripple-replay-transport-btn.active {
  color: var(--zr-accent);
}

.zenripple-replay-transport-btn.active:hover {
  background: var(--zr-accent-dim);
}

.zenripple-replay-speed {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  font-weight: 600;
  color: var(--zr-text-muted);
  min-width: 28px;
  text-align: center;
  transition: color 0.15s;
}

.zenripple-replay-speed.highlight {
  color: var(--zr-accent);
}

/* Progress bar */
.zenripple-replay-progress {
  flex: 1;
  height: 3px;
  background: var(--zr-bg-elevated);
  border-radius: 2px;
  overflow: hidden;
  cursor: pointer;
  margin: 0 4px;
}

.zenripple-replay-progress-fill {
  height: 100%;
  background: var(--zr-accent);
  border-radius: 2px;
  transition: width 0.15s ease-out;
  min-width: 0;
}

.zenripple-replay-progress:hover .zenripple-replay-progress-fill {
  height: 5px;
  margin-top: -1px;
}

.zenripple-replay-hint {
  font-size: 10px;
  color: var(--zr-text-muted);
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.zenripple-replay-kbd {
  font-family: var(--zr-font-mono);
  font-size: 9px;
  font-weight: 600;
  background: var(--zr-bg-elevated);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--zr-text-secondary);
  box-shadow: 0 1px 2px rgba(0,0,0,0.3);
}

.zenripple-replay-count {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  color: var(--zr-text-muted);
  flex-shrink: 0;
}

/* ── Empty state ── */
.zenripple-replay-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--zr-text-muted);
  font-size: 13px;
  font-style: italic;
}

/* ── Scrollbar styling (Firefox/Gecko) ── */
#zenripple-replay-modal * {
  scrollbar-width: thin;
  scrollbar-color: var(--zr-border-strong) transparent;
}

/* ── Back button ── */
.zenripple-replay-back {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--zr-r-sm);
  color: var(--zr-text-muted);
  cursor: pointer;
  transition: all 0.12s;
  font-size: 16px;
  line-height: 1;
  border: none;
  background: none;
  -moz-appearance: none;
  flex-shrink: 0;
}
.zenripple-replay-back:hover {
  background: var(--zr-bg-hover);
  color: var(--zr-text-primary);
}

/* ── Session browser ── */
#zenripple-replay-container.zenripple-session-browser-mode {
  width: min(96%, 560px);
}
.zenripple-session-browser {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
}
.zenripple-session-browser-scroll {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.zenripple-session-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 14px;
  border-radius: var(--zr-r-md);
  cursor: pointer;
  transition: background 0.1s;
}
.zenripple-session-row:hover {
  background: var(--zr-bg-hover);
}
.zenripple-session-row.selected {
  background: var(--zr-accent-dim);
}
.zenripple-session-row-main {
  display: flex;
  align-items: center;
  gap: 10px;
}
.zenripple-session-row-name {
  font-family: var(--zr-font-mono);
  font-size: 13px;
  font-weight: 600;
  color: var(--zr-text-primary);
}
.zenripple-session-row.selected .zenripple-session-row-name {
  color: var(--zr-accent);
}
.zenripple-session-row-count {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  font-weight: 600;
  color: var(--zr-text-secondary);
  background: var(--zr-bg-elevated);
  padding: 1px 6px;
  border-radius: var(--zr-r-sm);
}
.zenripple-session-row-id {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  color: var(--zr-text-secondary);
  opacity: 0.5;
}
.zenripple-session-row-meta {
  display: flex;
  gap: 10px;
  align-items: center;
}
.zenripple-session-row-date {
  font-size: 11px;
  color: var(--zr-text-secondary);
}
.zenripple-session-row-urls {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}
.zenripple-session-row-url {
  font-family: var(--zr-font-mono);
  font-size: 10px;
  color: var(--zr-text-secondary);
  opacity: 0.6;
  background: var(--zr-bg-elevated);
  padding: 1px 6px;
  border-radius: var(--zr-r-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 250px;
}
`;

  // ── Replay viewer state ──
  let _replayModal = null;
  let _replayEntries = [];
  let _replaySelectedIdx = -1;
  let _replaySessionId = null;
  let _replayReplayDir = null;
  let _selectGeneration = 0;
  let _currentScreenshotBlobURL = null;
  let _replayLoading = false;

  // Persistent splitter positions (survive modal close/reopen).
  // Separate state for wide and narrow modes.  Values are the "main"
  // panel percentage (outer) and the screenshot percentage (inner).
  const _splitterState = {
    wide:   { outer: null, inner: null },
    narrow: { outer: null, inner: null },
  };

  // Screenshot prefetch cache: Map<filename, blobURL>
  const _screenshotCache = new Map();
  const _CACHE_MAX = 5;

  function _cacheScreenshot(filename, blobURL) {
    if (!filename || !blobURL) return;
    _screenshotCache.set(filename, blobURL);
    // Evict oldest entries beyond max
    while (_screenshotCache.size > _CACHE_MAX) {
      const oldest = _screenshotCache.keys().next().value;
      const oldURL = _screenshotCache.get(oldest);
      _screenshotCache.delete(oldest);
      // Don't revoke if it's currently displayed
      if (oldURL !== _currentScreenshotBlobURL) {
        try { URL.revokeObjectURL(oldURL); } catch (_) {}
      }
    }
  }

  function _clearScreenshotCache() {
    for (const [, url] of _screenshotCache) {
      if (url !== _currentScreenshotBlobURL) {
        try { URL.revokeObjectURL(url); } catch (_) {}
      }
    }
    _screenshotCache.clear();
  }

  async function _prefetchScreenshot(replayDir, filename) {
    if (!filename || _screenshotCache.has(filename)) return;
    const url = await loadScreenshot(replayDir, filename);
    if (url) _cacheScreenshot(filename, url);
  }

  // Playback state
  let _playbackTimer = null;
  let _playbackPlaying = false;
  let _playbackSpeedIdx = 1;  // index into PLAYBACK_SPEEDS
  const PLAYBACK_SPEEDS = [0.5, 1, 2, 4, 8, 16, 32];
  const PLAYBACK_BASE_MS = 2000;  // interval at 1x

  // Live update polling
  let _liveUpdateTimer = null;
  const _LIVE_POLL_MS = 2000;

  function injectReplayStyles() {
    if (document.getElementById(REPLAY_STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = REPLAY_STYLE_ID;
    style.textContent = REPLAY_VIEWER_CSS;
    document.head.appendChild(style);
  }

  function syntaxHighlightJSON(jsonStr) {
    if (typeof jsonStr !== 'string') return '';
    // Single-pass tokenizer to avoid nested-span issues from chained regexes
    const escaped = jsonStr
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    // Match JSON tokens: strings, numbers, booleans, null, and key-value separators
    return escaped.replace(
      /("(?:\\.|[^"\\])*")(\s*:)?|(-?\b\d+\.?\d*(?:[eE][+-]?\d+)?\b)|\b(true|false)\b|\b(null)\b/g,
      function(match, str, colon, num, bool_, null_) {
        if (str) {
          return colon
            ? '<span class="zr-key">' + str + '</span>' + colon
            : '<span class="zr-str">' + str + '</span>';
        }
        if (num) return '<span class="zr-num">' + num + '</span>';
        if (bool_) return '<span class="zr-bool">' + bool_ + '</span>';
        if (null_) return '<span class="zr-null">' + null_ + '</span>';
        return match;
      }
    );
  }

  function formatJSON(value) {
    if (typeof value === 'string') {
      try {
        const parsed = JSON.parse(value);
        return JSON.stringify(parsed, null, 2);
      } catch (_) {
        return value;
      }
    }
    if (typeof value === 'object' && value !== null) {
      return JSON.stringify(value, null, 2);
    }
    return String(value ?? '');
  }

  function extractTime(timestamp) {
    if (!timestamp) return '';
    try {
      const d = new Date(timestamp);
      if (isNaN(d.getTime())) return '';
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (_) { return ''; }
  }

  function escapeHTML(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function stripToolPrefix(name) {
    return (name || '').replace(/^browser_/, '');
  }

  function toolCallSubtitle(tool, args) {
    if (!args || typeof args !== 'object') return '';
    const a = args;
    const stripProto = (u) => String(u || '').replace(/^https?:\/\//, '');
    const trunc = (s, n) => { s = String(s || ''); return s.length > n ? s.slice(0, n) + '\u2026' : s; };
    const selectorIdx = () => {
      let s = a.selector || '';
      if (a.index != null) s += ' [' + a.index + ']';
      return trunc(s, 50);
    };
    const bare = (tool || '').replace(/^browser_/, '');
    switch (bare) {
      case 'navigate':
      case 'create_tab':
        return trunc(stripProto(a.url), 50);
      case 'batch_navigate':
        return (a.urls || []).map(u => stripProto(u)).join(', ').slice(0, 50);
      case 'click':
      case 'hover':
      case 'select_option':
      case 'wait_for_element':
        return selectorIdx();
      case 'fill':
        return trunc((a.selector || '') + ' \u2190 ' + (a.value || ''), 50);
      case 'grounded_click':
      case 'find_element_by_description':
        return trunc(a.description, 50);
      case 'type':
        return trunc(a.text, 50);
      case 'press_key': {
        let parts = [];
        if (a.ctrl || a.control) parts.push('Ctrl');
        if (a.alt) parts.push('Alt');
        if (a.shift) parts.push('Shift');
        if (a.meta) parts.push('Cmd');
        parts.push(a.key || '');
        return parts.join('+');
      }
      case 'scroll':
        return a.direction || '';
      case 'set_session_name':
        return trunc(a.name, 50);
      case 'get_dom':
      case 'get_elements_compact':
      case 'get_accessibility_tree':
        return trunc(a.selector || '*', 50);
      case 'eval_chrome':
      case 'console_eval':
        return trunc(a.expression, 45);
      case 'wait_for_text':
        return trunc(a.text, 50);
      case 'wait':
        return a.ms != null ? a.ms + 'ms' : '';
      case 'wait_for_load':
        return a.state || '';
      case 'set_cookie':
        return trunc((a.name || '') + '=' + (a.value || ''), 50);
      case 'intercept_add_rule':
        return trunc(a.url_pattern, 50);
      case 'file_upload':
        return trunc((a.path || '').split('/').pop(), 50);
      case 'save_screenshot':
        return trunc((a.path || '').split('/').pop(), 50);
      case 'claim_tab':
        return trunc(a.tab_id, 50);
      case 'close_tab':
      case 'switch_tab':
        return trunc(a.tab_id, 50);
      case 'set_storage':
      case 'get_storage':
      case 'delete_storage':
        return trunc(a.key, 50);
      case 'clipboard_write':
        return trunc(a.text, 50);
      case 'handle_dialog':
        return a.action || '';
      case 'compare_tabs':
        return trunc((a.tab_id_1 || '') + ' vs ' + (a.tab_id_2 || ''), 50);
      case 'drag':
        return trunc((a.source_selector || '') + ' \u2192 ' + (a.target_selector || ''), 50);
      case 'drag_coordinates':
        return (a.start_x || 0) + ',' + (a.start_y || 0) + ' \u2192 ' + (a.end_x || 0) + ',' + (a.end_y || 0);
      case 'click_coordinates':
        return (a.x || 0) + ',' + (a.y || 0);
      case 'reflect':
        return trunc(a.query, 50);
      case 'record_replay':
        return trunc(a.name, 50);
      default: {
        // Fallback: find first short string value in args
        for (const v of Object.values(a)) {
          if (typeof v === 'string' && v.length > 0 && v.length <= 80) {
            return trunc(v, 50);
          }
        }
        return '';
      }
    }
  }

  async function loadReplayData(sessionId) {
    const tmpDir = PathUtils.tempDir;
    const replayDir = PathUtils.join(tmpDir, 'zenripple_replay_' + sessionId);
    const logPath = PathUtils.join(replayDir, 'tool_log.jsonl');

    let entries = [];
    try {
      const content = await IOUtils.readUTF8(logPath);
      const lines = content.trim().split('\n').filter(Boolean);
      for (const line of lines) {
        try { entries.push(JSON.parse(line)); } catch (_) {}
      }
      entries.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
    } catch (e) {
      log('Replay viewer: no tool_log.jsonl found for session ' + sessionId);
    }
    return { entries, replayDir };
  }

  // ── Session discovery: scan all replay dirs on disk ──

  async function discoverAllSessions() {
    const tmpDir = PathUtils.tempDir;
    const prefix = 'zenripple_replay_';
    const results = [];
    try {
      const children = await IOUtils.getChildren(tmpDir);
      for (const childPath of children) {
        const dirName = PathUtils.filename(childPath);
        if (!dirName.startsWith(prefix)) continue;
        const sessionId = dirName.slice(prefix.length);
        if (!sessionId) continue;
        const manifestPath = PathUtils.join(childPath, 'manifest.json');
        let manifest = null;
        try {
          manifest = JSON.parse(await IOUtils.readUTF8(manifestPath));
        } catch (_) { continue; }
        // Parse tool log: only JSON-parse lines that are relevant (navigate,
        // create_tab, set_session_name) to avoid parsing thousands of lines.
        let toolCount = 0;
        let urls = [];
        let sessionName = null;
        try {
          const logContent = await IOUtils.readUTF8(PathUtils.join(childPath, 'tool_log.jsonl'));
          const lines = logContent.trim().split('\n').filter(Boolean);
          toolCount = lines.length;
          for (const line of lines) {
            // Fast string check before expensive JSON.parse
            if (line.includes('browser_navigate') || line.includes('browser_create_tab') || line.includes('browser_set_session_name')) {
              try {
                const entry = JSON.parse(line);
                if (entry.tool === 'browser_navigate' && entry.args?.url) {
                  urls.push(entry.args.url);
                } else if (entry.tool === 'browser_create_tab' && entry.args?.url) {
                  urls.push(entry.args.url);
                } else if (entry.tool === 'browser_set_session_name' && entry.args?.name) {
                  sessionName = entry.args.name;
                }
              } catch (_) {}
            }
          }
        } catch (_) {}
        results.push({
          sessionId,
          name: sessionName,
          startedAt: manifest.started_at || '',
          toolCount,
          urls,
          dir: childPath,
        });
      }
    } catch (e) {
      log('discoverAllSessions error: ' + e);
    }
    // Sort newest first
    results.sort((a, b) => (b.startedAt || '').localeCompare(a.startedAt || ''));
    return results;
  }

  // ── Smart session matching for non-agent tabs ──

  async function guessSessionForTab(tab) {
    if (!tab?.linkedBrowser) return null;
    const currentUrl = tab.linkedBrowser.currentURI?.spec || '';
    // Collect tab history URLs
    const historyUrls = [];
    try {
      const hist = tab.linkedBrowser.sessionHistory;
      if (hist) {
        for (let i = 0; i < hist.count; i++) {
          const entry = hist.getEntryAtIndex(i);
          if (entry?.URI?.spec) historyUrls.push(entry.URI.spec);
        }
      }
    } catch (_) {}
    if (!currentUrl && historyUrls.length === 0) return null;

    const allUrls = [currentUrl, ...historyUrls].filter(Boolean);
    const allSessions = await discoverAllSessions();
    if (allSessions.length === 0) return null;

    // Score each session by URL overlap
    let bestSession = null;
    let bestScore = 0;
    for (const s of allSessions) {
      if (s.urls.length === 0) continue;
      let score = 0;
      for (const tabUrl of allUrls) {
        for (const sessionUrl of s.urls) {
          if (tabUrl === sessionUrl) score += 10;
          else {
            try {
              const tHost = new URL(tabUrl).hostname;
              const sHost = new URL(sessionUrl).hostname;
              if (tHost === sHost) score += 2;
            } catch (_) {}
          }
        }
      }
      // On ties, prefer newer sessions (allSessions is sorted newest-first)
      if (score > bestScore) {
        bestScore = score;
        bestSession = s;
      }
    }
    return bestSession;
  }

  // ── Workspace check ──

  function isTabInAgentWorkspace(tab) {
    if (!tab || !agentWorkspaceId) return false;
    return tab.getAttribute('zen-workspace-id') === agentWorkspaceId;
  }

  function isCurrentWorkspaceAgent() {
    if (!gZenWorkspaces) return false;
    // Lazily resolve agentWorkspaceId if no session has connected yet
    if (!agentWorkspaceId) {
      const workspaces = gZenWorkspaces._workspaceCache;
      if (workspaces) {
        const ws = workspaces.find(w => w.name === AGENT_WORKSPACE_NAME);
        if (ws) agentWorkspaceId = ws.uuid;
      }
      if (!agentWorkspaceId) return false;
    }
    try {
      const active = gZenWorkspaces.getActiveWorkspace?.();
      if (active) return active.uuid === agentWorkspaceId;
      // Fallback: check selected tab
      const tab = gBrowser.selectedTab;
      return tab && tab.getAttribute('zen-workspace-id') === agentWorkspaceId;
    } catch (_) { return false; }
  }

  // ── Session browser view ──

  let _sessionBrowserMode = false; // true when showing session list

  function buildSessionBrowserHTML(sessionList) {
    const rows = sessionList.map((s, i) => {
      const name = escapeHTML(s.name || s.sessionId.slice(0, 12));
      const date = s.startedAt ? new Date(s.startedAt) : null;
      const dateStr = date && !isNaN(date.getTime())
        ? date.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
          date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : '';
      const urlPreview = s.urls.length > 0
        ? escapeHTML(s.urls[0].replace(/^https?:\/\//, '').slice(0, 40))
        : '';
      return `<div class="zenripple-session-row" data-idx="${i}" tabindex="0">
        <div class="zenripple-session-row-main">
          <span class="zenripple-session-row-name">${name}</span>
          <span class="zenripple-session-row-count">${s.toolCount} call${s.toolCount !== 1 ? 's' : ''}</span>
        </div>
        <div class="zenripple-session-row-meta">
          <span class="zenripple-session-row-date">${escapeHTML(dateStr)}</span>
          ${urlPreview ? `<span class="zenripple-session-row-url">${urlPreview}</span>` : ''}
        </div>
      </div>`;
    }).join('');
    return `<div class="zenripple-session-browser-scroll">${rows || '<div class="zenripple-replay-empty">No saved session replays found.</div>'}</div>`;
  }

  async function showSessionBrowser() {
    _sessionBrowserMode = true;
    _stopPlayback();
    _stopLiveUpdates();

    const allSessions = await discoverAllSessions();

    // Update modal content to show session list
    const container = _replayModal.querySelector('#zenripple-replay-container');
    if (!container) return;

    // Save reference for navigation
    _sessionBrowserList = allSessions;
    _sessionBrowserIdx = 0;

    // Narrow the container for browser mode
    container.classList.add('zenripple-session-browser-mode');

    // Hide body + footer, show browser
    const body = container.querySelector('.zenripple-replay-body');
    const emptyMsg = container.querySelector('.zenripple-replay-empty');
    const footer = container.querySelector('.zenripple-replay-footer');
    if (body) body.style.display = 'none';
    if (emptyMsg) emptyMsg.style.display = 'none';
    if (footer) footer.style.display = 'none';

    // Remove previous browser if any
    const oldBrowser = container.querySelector('.zenripple-session-browser');
    if (oldBrowser) oldBrowser.remove();

    // Update header
    const title = container.querySelector('.zenripple-replay-title');
    if (title) title.textContent = 'All Sessions';
    const badge = container.querySelector('.zenripple-replay-session-badge');
    if (badge) badge.textContent = allSessions.length + ' saved';
    const backBtn = container.querySelector('.zenripple-replay-back');
    if (backBtn) backBtn.style.display = 'none';

    // Build and insert browser
    const browser = document.createElement('div');
    browser.className = 'zenripple-session-browser';
    browser.innerHTML = buildSessionBrowserHTML(allSessions);

    // Insert after header
    const header = container.querySelector('.zenripple-replay-header');
    if (header && header.nextSibling) {
      container.insertBefore(browser, header.nextSibling);
    } else {
      container.appendChild(browser);
    }

    // Click handlers
    const rows = browser.querySelectorAll('.zenripple-session-row');
    for (const row of rows) {
      row.addEventListener('click', () => {
        const idx = parseInt(row.dataset.idx, 10);
        _openSessionFromBrowser(idx);
      });
    }

    // Highlight first
    if (rows.length > 0) {
      rows[0].classList.add('selected');
    }
  }

  let _sessionBrowserList = [];
  let _sessionBrowserIdx = 0;

  function _navigateSessionBrowser(delta) {
    const browser = _replayModal?.querySelector('.zenripple-session-browser');
    if (!browser || _sessionBrowserList.length === 0) return;
    const rows = browser.querySelectorAll('.zenripple-session-row');
    _sessionBrowserIdx = Math.max(0, Math.min(rows.length - 1, _sessionBrowserIdx + delta));
    for (const r of rows) r.classList.remove('selected');
    if (rows[_sessionBrowserIdx]) {
      rows[_sessionBrowserIdx].classList.add('selected');
      rows[_sessionBrowserIdx].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  async function _openSessionFromBrowser(idx) {
    const s = _sessionBrowserList[idx];
    if (!s) return;
    _sessionBrowserMode = false;
    // Look up live session data if available
    const session = sessions.get(s.sessionId) || { name: s.name };
    closeReplayModal();
    await openReplayModal(s.sessionId, session);
  }

  function _exitSessionBrowser() {
    if (!_sessionBrowserMode || !_replayModal) return;
    _sessionBrowserMode = false;

    const container = _replayModal.querySelector('#zenripple-replay-container');
    if (!container) return;

    // Restore container width
    container.classList.remove('zenripple-session-browser-mode');

    // Remove browser panel
    const browser = container.querySelector('.zenripple-session-browser');
    if (browser) browser.remove();

    // Restore body + footer + empty message
    const body = container.querySelector('.zenripple-replay-body');
    const emptyMsg = container.querySelector('.zenripple-replay-empty');
    const footer = container.querySelector('.zenripple-replay-footer');
    if (body) body.style.display = '';
    if (emptyMsg) emptyMsg.style.display = '';
    if (footer) footer.style.display = '';

    // Restore header
    const title = container.querySelector('.zenripple-replay-title');
    if (title) title.textContent = 'Session Replay';
    const badge = container.querySelector('.zenripple-replay-session-badge');
    if (badge) badge.textContent = escapeHTML(
      (_replaySessionId ? (sessions.get(_replaySessionId)?.name || _replaySessionId.slice(0, 12)) : '')
    );
    const backBtn = container.querySelector('.zenripple-replay-back');
    if (backBtn) backBtn.style.display = '';

    // Restart live updates
    _startLiveUpdates();
  }

  function _startLiveUpdates() {
    _stopLiveUpdates();
    _liveUpdateTimer = setInterval(() => _pollForNewEntries(), _LIVE_POLL_MS);
  }

  function _stopLiveUpdates() {
    if (_liveUpdateTimer) {
      clearInterval(_liveUpdateTimer);
      _liveUpdateTimer = null;
    }
  }

  async function _pollForNewEntries() {
    if (!_replayModal || !_replayReplayDir) return;
    const logPath = PathUtils.join(_replayReplayDir, 'tool_log.jsonl');
    let allEntries = [];
    try {
      const content = await IOUtils.readUTF8(logPath);
      const lines = content.trim().split('\n').filter(Boolean);
      for (const line of lines) {
        try { allEntries.push(JSON.parse(line)); } catch (_) {}
      }
      allEntries.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
    } catch (_) {
      return;
    }

    if (allEntries.length <= _replayEntries.length) return;

    // New entries found — append them
    const newEntries = allEntries.slice(_replayEntries.length);
    _replayEntries = allEntries;

    // Add new entries to the list DOM (inserted at top since list is reversed)
    const listContainer = _replayModal.querySelector('#zenripple-replay-entries');
    if (listContainer) {
      for (const entry of newEntries) {
        const i = allEntries.indexOf(entry);
        const el = document.createElement('div');
        el.className = 'zenripple-replay-entry';
        el.dataset.idx = String(i);
        const subtitle = toolCallSubtitle(entry.tool, entry.args);
        el.innerHTML = `
          <span class="zenripple-replay-entry-seq">${escapeHTML(entry.seq ?? i)}</span>
          <span class="zenripple-replay-entry-dot${entry.error ? ' error' : ''}"></span>
          <span class="zenripple-replay-entry-col">
            <span class="zenripple-replay-entry-name">${escapeHTML(stripToolPrefix(entry.tool || ''))}</span>
            ${subtitle ? '<span class="zenripple-replay-entry-subtitle">' + escapeHTML(subtitle) + '</span>' : ''}
          </span>
          <span class="zenripple-replay-entry-time">${escapeHTML(extractTime(entry.timestamp))}</span>
        `;
        el.addEventListener('click', () => {
          _stopPlayback();
          selectReplayEntry(i, _replayReplayDir);
        });
        // Insert at top (most recent first)
        listContainer.insertBefore(el, listContainer.firstChild);
      }
    }

    // Update header count
    const listHeader = _replayModal.querySelector('.zenripple-replay-list-header');
    if (listHeader) listHeader.textContent = 'Tool Calls (' + allEntries.length + ')';
    const countEl = _replayModal.querySelector('#zenripple-replay-count');
    if (countEl) countEl.textContent = allEntries.length + ' call' + (allEntries.length !== 1 ? 's' : '');

    // Update progress bar since total changed
    _updateProgressBar();
  }

  async function loadScreenshot(replayDir, filename) {
    if (!filename) return null;
    if (filename.includes('/') || filename.includes('\\') || filename.includes('..')) return null;
    const filepath = PathUtils.join(replayDir, filename);
    try {
      const bytes = await IOUtils.read(filepath);
      const blob = new Blob([bytes], { type: 'image/jpeg' });
      return URL.createObjectURL(blob);
    } catch (e) {
      return null;
    }
  }

  // ── Playback engine ──

  function _playbackInterval() {
    return PLAYBACK_BASE_MS / PLAYBACK_SPEEDS[_playbackSpeedIdx];
  }

  function _stopPlayback() {
    if (_playbackTimer) {
      clearInterval(_playbackTimer);
      _playbackTimer = null;
    }
    _playbackPlaying = false;
    _updateTransportUI();
  }

  function _startPlayback() {
    _stopPlayback();
    _playbackPlaying = true;
    _updateTransportUI();
    _playbackTimer = setInterval(() => {
      if (!_replayModal || _replayEntries.length === 0) {
        _stopPlayback();
        return;
      }
      // Advance with wraparound
      let next = _replaySelectedIdx + 1;
      if (next >= _replayEntries.length) next = 0;
      selectReplayEntry(next, _replayReplayDir);
    }, _playbackInterval());
  }

  function _togglePlayback() {
    if (_playbackPlaying) {
      _stopPlayback();
    } else {
      _startPlayback();
    }
  }

  function _setPlaybackSpeed(idx) {
    _playbackSpeedIdx = Math.max(0, Math.min(PLAYBACK_SPEEDS.length - 1, idx));
    _updateTransportUI();
    if (_playbackPlaying) {
      // Restart timer with new speed
      _startPlayback();
    }
  }

  function _updateTransportUI() {
    if (!_replayModal) return;

    // Play/pause button
    const playBtn = _replayModal.querySelector('#zr-play-btn');
    if (playBtn) {
      playBtn.innerHTML = _playbackPlaying ? '&#x23F8;' : '&#x25B6;';
      playBtn.classList.toggle('active', _playbackPlaying);
      playBtn.title = _playbackPlaying ? 'Pause (Space)' : 'Play (Space)';
    }

    // Speed display
    const speedEl = _replayModal.querySelector('#zr-speed');
    if (speedEl) {
      const s = PLAYBACK_SPEEDS[_playbackSpeedIdx];
      speedEl.textContent = s + 'x';
      speedEl.classList.toggle('highlight', s !== 1);
    }

    // Progress bar
    _updateProgressBar();
  }

  function _updateProgressBar() {
    if (!_replayModal) return;
    const fill = _replayModal.querySelector('#zr-progress-fill');
    if (fill && _replayEntries.length > 0) {
      const pct = ((_replaySelectedIdx + 1) / _replayEntries.length) * 100;
      fill.style.width = pct + '%';
    } else if (fill) {
      fill.style.width = '0%';
    }
  }

  // ── Modal builder ──

  function buildReplayModal(sessionId, session, entries, replayDir) {
    const modal = document.createElement('div');
    modal.id = REPLAY_MODAL_ID;
    modal.dataset.sessionId = sessionId;

    const sessionLabel = escapeHTML((session && session.name) || sessionId.slice(0, 12));
    const hasEntries = entries.length > 0;

    modal.innerHTML = `
      <div id="zenripple-replay-backdrop"></div>
      <div id="zenripple-replay-container">
        <div class="zenripple-replay-header">
          <div class="zenripple-replay-back" title="All Sessions (Backspace)">&#x2190;</div>
          <span class="zenripple-replay-title">Session Replay</span>
          <span class="zenripple-replay-session-badge">${sessionLabel}</span>
          <div class="zenripple-replay-close" title="Close (Esc)">&#x2715;</div>
        </div>
        ${hasEntries ? `
        <div class="zenripple-replay-body">
          <div class="zenripple-replay-main">
            <div class="zenripple-replay-screenshot" id="zenripple-replay-ss">
              <span class="zenripple-replay-no-screenshot">Select a tool call</span>
            </div>
            <div class="zenripple-replay-splitter zenripple-replay-splitter-v zenripple-replay-splitter-inner" data-splitter="inner"></div>
            <div class="zenripple-replay-details">
              <div class="zenripple-replay-detail-scroll" id="zenripple-replay-detail">
                <div class="zenripple-replay-tool-name" id="zenripple-replay-tname">\u2014</div>
                <div class="zenripple-replay-meta" id="zenripple-replay-meta"></div>
                <div class="zenripple-replay-section-label">Arguments</div>
                <div class="zenripple-replay-json" id="zenripple-replay-args">\u2014</div>
                <div class="zenripple-replay-section-label">Result</div>
                <div class="zenripple-replay-json zr-result-json" id="zenripple-replay-result">\u2014</div>
              </div>
            </div>
          </div>
          <div class="zenripple-replay-splitter zenripple-replay-splitter-v zenripple-replay-splitter-outer" data-splitter="outer"></div>
          <div class="zenripple-replay-list">
            <div class="zenripple-replay-list-header">Tool Calls (${entries.length})</div>
            <div class="zenripple-replay-list-scroll" id="zenripple-replay-entries"></div>
          </div>
        </div>
        ` : `
        <div class="zenripple-replay-empty">No tool calls recorded for this session yet.</div>
        `}
        <div class="zenripple-replay-footer">
          <div class="zenripple-replay-transport">
            <div class="zenripple-replay-transport-btn" id="zr-play-btn" title="Play (Space)">&#x25B6;</div>
            <div class="zenripple-replay-transport-btn" id="zr-slower-btn" title="Slower ([)">&#x2BC7;</div>
            <span class="zenripple-replay-speed" id="zr-speed">1x</span>
            <div class="zenripple-replay-transport-btn" id="zr-faster-btn" title="Faster (])">&#x2BC8;</div>
          </div>
          <div class="zenripple-replay-progress" id="zr-progress">
            <div class="zenripple-replay-progress-fill" id="zr-progress-fill"></div>
          </div>
          <span class="zenripple-replay-hint">
            <span class="zenripple-replay-kbd">j</span><span class="zenripple-replay-kbd">k</span>
            nav
          </span>
          <span class="zenripple-replay-hint">
            <span class="zenripple-replay-kbd">[</span><span class="zenripple-replay-kbd">]</span>
            speed
          </span>
          <span class="zenripple-replay-hint">
            <span class="zenripple-replay-kbd">Esc</span>
            close
          </span>
          <span class="zenripple-replay-count" id="zenripple-replay-count">
            ${entries.length} call${entries.length !== 1 ? 's' : ''}
          </span>
        </div>
      </div>
    `;

    // Build the entry list — reversed (most recent at top)
    if (hasEntries) {
      const listContainer = modal.querySelector('#zenripple-replay-entries');
      for (let i = entries.length - 1; i >= 0; i--) {
        const entry = entries[i];
        const el = document.createElement('div');
        el.className = 'zenripple-replay-entry';
        el.dataset.idx = String(i);
        const subtitle = toolCallSubtitle(entry.tool, entry.args);
        el.innerHTML = `
          <span class="zenripple-replay-entry-seq">${escapeHTML(entry.seq ?? i)}</span>
          <span class="zenripple-replay-entry-dot${entry.error ? ' error' : ''}"></span>
          <span class="zenripple-replay-entry-col">
            <span class="zenripple-replay-entry-name">${escapeHTML(stripToolPrefix(entry.tool || ''))}</span>
            ${subtitle ? '<span class="zenripple-replay-entry-subtitle">' + escapeHTML(subtitle) + '</span>' : ''}
          </span>
          <span class="zenripple-replay-entry-time">${escapeHTML(extractTime(entry.timestamp))}</span>
        `;
        el.addEventListener('click', () => {
          _stopPlayback();
          selectReplayEntry(i, replayDir);
        });
        listContainer.appendChild(el);
      }
    }

    // Close and back handlers
    modal.querySelector('#zenripple-replay-backdrop').addEventListener('click', closeReplayModal);
    modal.querySelector('.zenripple-replay-close').addEventListener('click', closeReplayModal);
    const backBtn = modal.querySelector('.zenripple-replay-back');
    if (backBtn) backBtn.addEventListener('click', () => showSessionBrowser());

    // Transport button handlers
    const playBtn = modal.querySelector('#zr-play-btn');
    if (playBtn) playBtn.addEventListener('click', _togglePlayback);
    const slowerBtn = modal.querySelector('#zr-slower-btn');
    if (slowerBtn) slowerBtn.addEventListener('click', () => _setPlaybackSpeed(_playbackSpeedIdx - 1));
    const fasterBtn = modal.querySelector('#zr-faster-btn');
    if (fasterBtn) fasterBtn.addEventListener('click', () => _setPlaybackSpeed(_playbackSpeedIdx + 1));

    // Progress bar click-to-seek
    const progressBar = modal.querySelector('#zr-progress');
    if (progressBar && hasEntries) {
      progressBar.addEventListener('click', (ev) => {
        const rect = progressBar.getBoundingClientRect();
        const pct = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
        const idx = Math.round(pct * (_replayEntries.length - 1));
        _stopPlayback();
        selectReplayEntry(idx, replayDir);
      });
    }

    // ── Splitter drag handling ──
    const _body = modal.querySelector('.zenripple-replay-body');
    const _main = modal.querySelector('.zenripple-replay-main');
    const _innerSplit = modal.querySelector('.zenripple-replay-splitter-inner');
    const _outerSplit = modal.querySelector('.zenripple-replay-splitter-outer');
    const _ssPanel = modal.querySelector('.zenripple-replay-screenshot');
    const _detPanel = modal.querySelector('.zenripple-replay-details');
    const _listPanel = modal.querySelector('.zenripple-replay-list');

    if (_body && _main && _innerSplit && _outerSplit) {
      let _dragTarget = null; // 'inner' | 'outer'
      let _wasNarrow = false; // track mode for save/restore on switch

      function _isNarrow() { return _body.classList.contains('zenripple-narrow'); }

      // Apply saved splitter positions for the given mode
      function _restoreLayout(mode) {
        const s = _splitterState[mode];
        if (s.outer != null) {
          _main.style.flex = `0 0 ${s.outer}%`;
          _listPanel.style.flex = `0 0 ${100 - s.outer}%`;
        }
        if (s.inner != null) {
          _ssPanel.style.flex = `0 0 ${s.inner}%`;
          _detPanel.style.flex = `0 0 ${100 - s.inner}%`;
        }
      }

      // Save current splitter positions for the given mode
      function _saveLayout(mode) {
        const bodyRect = _body.getBoundingClientRect();
        const mainRect = _main.getBoundingClientRect();
        if (bodyRect.width > 0) {
          _splitterState[mode].outer = (mainRect.width / bodyRect.width) * 100;
        }
        if (mode === 'narrow' && mainRect.height > 0) {
          const ssRect = _ssPanel.getBoundingClientRect();
          _splitterState[mode].inner = (ssRect.height / mainRect.height) * 100;
        } else if (mode === 'wide' && mainRect.width > 0) {
          const ssRect = _ssPanel.getBoundingClientRect();
          _splitterState[mode].inner = (ssRect.width / mainRect.width) * 100;
        }
      }

      function _onSplitDown(which, ev) {
        ev.preventDefault();
        _dragTarget = which;
        const el = which === 'inner' ? _innerSplit : _outerSplit;
        el.classList.add('dragging');
        const cursor = (_isNarrow() && which === 'inner') ? 'row-resize' : 'col-resize';
        document.documentElement.style.cursor = cursor;
        document.documentElement.style.userSelect = 'none';
        _ssPanel.style.pointerEvents = 'none';
      }

      function _onSplitMove(ev) {
        if (!_dragTarget) return;
        if (_dragTarget === 'outer') {
          const rect = _body.getBoundingClientRect();
          const pct = Math.max(30, Math.min(85, ((ev.clientX - rect.left) / rect.width) * 100));
          _main.style.flex = `0 0 ${pct}%`;
          _listPanel.style.flex = `0 0 ${100 - pct}%`;
        } else {
          if (_isNarrow()) {
            const rect = _main.getBoundingClientRect();
            const pct = Math.max(20, Math.min(80, ((ev.clientY - rect.top) / rect.height) * 100));
            _ssPanel.style.flex = `0 0 ${pct}%`;
            _detPanel.style.flex = `0 0 ${100 - pct}%`;
          } else {
            const rect = _main.getBoundingClientRect();
            const pct = Math.max(20, Math.min(80, ((ev.clientX - rect.left) / rect.width) * 100));
            _ssPanel.style.flex = `0 0 ${pct}%`;
            _detPanel.style.flex = `0 0 ${100 - pct}%`;
          }
        }
      }

      function _onSplitUp() {
        if (!_dragTarget) return;
        _innerSplit.classList.remove('dragging');
        _outerSplit.classList.remove('dragging');
        // Save positions for the current mode
        const mode = _isNarrow() ? 'narrow' : 'wide';
        _splitterState[mode].outer = null;
        _splitterState[mode].inner = null;
        _saveLayout(mode);
        _dragTarget = null;
        document.documentElement.style.cursor = '';
        document.documentElement.style.userSelect = '';
        _ssPanel.style.pointerEvents = '';
      }

      _innerSplit.addEventListener('mousedown', (ev) => _onSplitDown('inner', ev));
      _outerSplit.addEventListener('mousedown', (ev) => _onSplitDown('outer', ev));
      window.addEventListener('mousemove', _onSplitMove);
      window.addEventListener('mouseup', _onSplitUp);

      modal._splitterCleanup = () => {
        window.removeEventListener('mousemove', _onSplitMove);
        window.removeEventListener('mouseup', _onSplitUp);
      };

      // Observe container size: toggle narrow class and swap saved layouts
      const container = modal.querySelector('#zenripple-replay-container');
      if (container) {
        const ro = new ResizeObserver((entries) => {
          for (const entry of entries) {
            const { width, height } = entry.contentRect;
            const narrow = width < height * 1.1;
            _body.classList.toggle('zenripple-narrow', narrow);

            // On mode switch, save old layout and restore new mode's layout
            if (narrow !== _wasNarrow) {
              // Clear inline flex so CSS defaults apply before restoring
              _main.style.flex = '';
              _listPanel.style.flex = '';
              _ssPanel.style.flex = '';
              _detPanel.style.flex = '';
              const newMode = narrow ? 'narrow' : 'wide';
              _restoreLayout(newMode);
              _wasNarrow = narrow;
            }
          }
        });
        ro.observe(container);
        modal._resizeObserverCleanup = () => { ro.disconnect(); };

        // Initial mode detection + restore
        const rect = container.getBoundingClientRect();
        _wasNarrow = rect.width < rect.height * 1.1;
        _body.classList.toggle('zenripple-narrow', _wasNarrow);
        _restoreLayout(_wasNarrow ? 'narrow' : 'wide');
      }
    }

    return modal;
  }

  async function selectReplayEntry(idx, replayDir) {
    if (idx < 0 || idx >= _replayEntries.length) return;
    _replaySelectedIdx = idx;
    const generation = ++_selectGeneration;
    const entry = _replayEntries[idx];

    // Update list selection — entries are rendered reversed in DOM
    const entryEls = _replayModal.querySelectorAll('.zenripple-replay-entry');
    for (const el of entryEls) {
      el.classList.toggle('selected', parseInt(el.dataset.idx, 10) === idx);
    }

    // Scroll selected entry into view
    for (const el of entryEls) {
      if (parseInt(el.dataset.idx, 10) === idx) {
        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        break;
      }
    }

    // Update tool name
    const tname = _replayModal.querySelector('#zenripple-replay-tname');
    if (tname) tname.textContent = entry.tool || '\u2014';

    // Update meta badges — show timestamp, duration, and seq
    const meta = _replayModal.querySelector('#zenripple-replay-meta');
    if (meta) {
      const durationStr = entry.duration_ms != null ? escapeHTML(Math.round(entry.duration_ms) + 'ms') : '\u2014';
      const timeStr = escapeHTML(extractTime(entry.timestamp)) || '\u2014';
      const seqStr = escapeHTML('#' + (entry.seq ?? idx));
      meta.innerHTML = `
        <span class="zenripple-replay-meta-item">${seqStr}</span>
        <span class="zenripple-replay-meta-item">${timeStr}</span>
        <span class="zenripple-replay-meta-item">${durationStr}</span>
        ${entry.error ? '<span class="zenripple-replay-meta-item error">ERROR</span>' : ''}
      `;
    }

    // Update args
    const argsEl = _replayModal.querySelector('#zenripple-replay-args');
    if (argsEl) {
      const formatted = formatJSON(entry.args);
      argsEl.innerHTML = syntaxHighlightJSON(formatted);
    }

    // Update result
    const resultEl = _replayModal.querySelector('#zenripple-replay-result');
    if (resultEl) {
      const formatted = formatJSON(entry.result);
      resultEl.innerHTML = syntaxHighlightJSON(formatted);
    }

    // Update progress bar
    _updateProgressBar();

    // Revoke previous screenshot blob URL before loading new one (unless cached)
    if (_currentScreenshotBlobURL) {
      let inCache = false;
      for (const url of _screenshotCache.values()) {
        if (url === _currentScreenshotBlobURL) { inCache = true; break; }
      }
      if (!inCache) try { URL.revokeObjectURL(_currentScreenshotBlobURL); } catch (_) {}
    }
    _currentScreenshotBlobURL = null;

    // Load screenshot (check cache first, reuse <img> to avoid flash)
    const ssContainer = _replayModal.querySelector('#zenripple-replay-ss');
    if (ssContainer) {
      if (entry.screenshot) {
        let url = _screenshotCache.get(entry.screenshot) || null;
        if (!url) {
          url = await loadScreenshot(replayDir, entry.screenshot);
          if (generation !== _selectGeneration) {
            if (url) try { URL.revokeObjectURL(url); } catch (_) {}
            return;
          }
          if (url) _cacheScreenshot(entry.screenshot, url);
        }
        if (url) {
          _currentScreenshotBlobURL = url;
          // Reuse existing <img> element to prevent flash
          let img = ssContainer.querySelector('img');
          if (img) {
            img.src = url;
          } else {
            ssContainer.innerHTML = '';
            img = document.createElement('img');
            img.alt = 'Screenshot';
            img.src = url;
            ssContainer.appendChild(img);
          }
        } else {
          ssContainer.innerHTML = '<span class="zenripple-replay-no-screenshot">Screenshot unavailable</span>';
        }
      } else {
        ssContainer.innerHTML = '<span class="zenripple-replay-no-screenshot">No screenshot for this call</span>';
      }
    }

    // Prefetch next screenshot for smoother playback
    const nextIdx = idx + 1 < _replayEntries.length ? idx + 1 : 0;
    const nextEntry = _replayEntries[nextIdx];
    if (nextEntry && nextEntry.screenshot) {
      _prefetchScreenshot(replayDir, nextEntry.screenshot);
    }
  }

  function _navigateReplay(direction) {
    // direction: 1 = next (newer), -1 = previous (older)
    if (_replayEntries.length === 0) return;
    let newIdx = _replaySelectedIdx + direction;
    if (newIdx < 0) newIdx = 0;
    if (newIdx >= _replayEntries.length) newIdx = _replayEntries.length - 1;
    if (newIdx !== _replaySelectedIdx) {
      selectReplayEntry(newIdx, _replayReplayDir);
    }
  }

  function handleReplayKeydown(e) {
    if (!_replayModal) return;

    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      if (_sessionBrowserMode) {
        _exitSessionBrowser();
      } else {
        closeReplayModal();
      }
      return;
    }

    // Backspace: go to session browser (or back from it)
    if (e.key === 'Backspace') {
      e.preventDefault();
      e.stopPropagation();
      if (_sessionBrowserMode) {
        _exitSessionBrowser();
      } else {
        showSessionBrowser();
      }
      return;
    }

    // Session browser mode: navigate list
    if (_sessionBrowserMode) {
      if (e.key === 'ArrowDown' || e.key === 'j') {
        e.preventDefault(); e.stopPropagation();
        _navigateSessionBrowser(1);
        return;
      }
      if (e.key === 'ArrowUp' || e.key === 'k') {
        e.preventDefault(); e.stopPropagation();
        _navigateSessionBrowser(-1);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault(); e.stopPropagation();
        _openSessionFromBrowser(_sessionBrowserIdx);
        return;
      }
      return; // Swallow other keys in browser mode
    }

    // Navigation: ArrowDown/j = older (lower seq), ArrowUp/k = newer (higher seq)
    if (e.key === 'ArrowDown' || e.key === 'j') {
      e.preventDefault();
      e.stopPropagation();
      _stopPlayback();
      _navigateReplay(-1);
      return;
    }
    if (e.key === 'ArrowUp' || e.key === 'k') {
      e.preventDefault();
      e.stopPropagation();
      _stopPlayback();
      _navigateReplay(1);
      return;
    }

    // Playback: Space = play/pause
    if (e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
      _togglePlayback();
      return;
    }

    // Speed: [ = slower, ] = faster
    if (e.key === '[') {
      e.preventDefault();
      e.stopPropagation();
      _setPlaybackSpeed(_playbackSpeedIdx - 1);
      return;
    }
    if (e.key === ']') {
      e.preventDefault();
      e.stopPropagation();
      _setPlaybackSpeed(_playbackSpeedIdx + 1);
      return;
    }

    // Home/End: jump to first/last
    if (e.key === 'Home' || e.key === 'g') {
      e.preventDefault();
      e.stopPropagation();
      _stopPlayback();
      if (_replayEntries.length > 0) selectReplayEntry(0, _replayReplayDir);
      return;
    }
    if (e.key === 'End' || e.key === 'G') {
      e.preventDefault();
      e.stopPropagation();
      _stopPlayback();
      if (_replayEntries.length > 0) selectReplayEntry(_replayEntries.length - 1, _replayReplayDir);
      return;
    }
  }

  async function openReplayModal(sessionId, session) {
    if (_replayLoading) return;
    _replayLoading = true;

    const openBrowser = sessionId === '_browser_';
    const effectiveId = openBrowser ? '' : sessionId;

    try {
      closeReplayModal();

      _replaySessionId = effectiveId;
      _sessionBrowserMode = false;
      injectReplayStyles();

      let entries = [];
      let replayDir = '';
      if (effectiveId) {
        const data = await loadReplayData(effectiveId);
        entries = data.entries;
        replayDir = data.replayDir;
      }
      _replayEntries = entries;
      _replaySelectedIdx = -1;
      _replayReplayDir = replayDir;

      // Reset playback state
      _playbackPlaying = false;
      _playbackSpeedIdx = 1;
      _playbackTimer = null;

      _replayModal = buildReplayModal(effectiveId || 'browser', session, entries, replayDir);
      document.documentElement.appendChild(_replayModal);

      window.addEventListener('keydown', handleReplayKeydown, true);

      if (openBrowser) {
        // Go straight to session browser
        await showSessionBrowser();
      } else {
        // Auto-select the most recent entry
        if (entries.length > 0) {
          selectReplayEntry(entries.length - 1, replayDir);
        }
        // Start polling for new entries while modal is open
        _startLiveUpdates();
      }

      log('Replay viewer opened for session ' + (effectiveId || 'browser') + ' (' + entries.length + ' entries)');
    } catch (err) {
      log('Error opening replay modal: ' + err);
    } finally {
      _replayLoading = false;
    }
  }

  function closeReplayModal() {
    if (!_replayModal) return;

    _stopPlayback();
    _stopLiveUpdates();
    window.removeEventListener('keydown', handleReplayKeydown, true);

    // Clean up splitter and ResizeObserver listeners
    if (_replayModal._splitterCleanup) _replayModal._splitterCleanup();
    if (_replayModal._resizeObserverCleanup) _replayModal._resizeObserverCleanup();

    _clearScreenshotCache();
    if (_currentScreenshotBlobURL) {
      try { URL.revokeObjectURL(_currentScreenshotBlobURL); } catch (_) {}
      _currentScreenshotBlobURL = null;
    }

    _replayModal.remove();
    _replayModal = null;
    _replayEntries = [];
    _replaySelectedIdx = -1;
    _replaySessionId = null;
    _replayReplayDir = null;
    _sessionBrowserMode = false;
    // Note: _replayLoading is managed by openReplayModal's try/finally — don't reset here

    log('Replay viewer closed');
  }

  function handleReplayShortcut(e) {
    // Ctrl+Shift+E on all platforms
    if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && e.code === 'KeyE') {
      // Only respond in the ZenRipple workspace
      if (!isCurrentWorkspaceAgent()) return;

      e.preventDefault();
      e.stopPropagation();

      const currentTab = gBrowser.selectedTab;
      if (!currentTab) return;

      const sessionId = currentTab.getAttribute('data-agent-session-id');

      if (sessionId) {
        // Agent tab: toggle or switch to its replay
        if (_replayModal) {
          if (_replaySessionId === sessionId) {
            closeReplayModal();
            return;
          }
          closeReplayModal();
        }
        const session = sessions.get(sessionId);
        openReplayModal(sessionId, session || null).catch(err => log('Replay modal error: ' + err));
      } else {
        // Non-agent tab: try smart matching, then fall back to session browser
        if (_replayModal) { closeReplayModal(); return; }
        (async () => {
          try {
            const matched = await guessSessionForTab(currentTab);
            if (matched) {
              const session = sessions.get(matched.sessionId) || { name: matched.name };
              await openReplayModal(matched.sessionId, session);
            } else {
              // No match — open session browser directly
              await openReplayModal('_browser_', null);
            }
          } catch (err) {
            log('Replay shortcut error: ' + err);
          }
        })();
      }
    }
  }

  function setupReplayShortcut() {
    window.addEventListener('keydown', handleReplayShortcut, true);
    log('Replay viewer shortcut registered (Ctrl+Shift+E)');
  }

  // ── Tab context menu: "Session Replay" ──

  function setupReplayContextMenu() {
    const menu = document.getElementById('tabContextMenu');
    if (!menu) {
      log('tabContextMenu not found — skipping context menu setup');
      return;
    }

    const menuItem = document.createXULElement('menuitem');
    menuItem.id = 'zenripple-context-replay';
    menuItem.setAttribute('label', 'Session Replay');
    menuItem.setAttribute('accesskey', 'R');
    menuItem.addEventListener('command', () => {
      const tab = TabContextMenu.contextTab || gBrowser.selectedTab;
      if (!tab) return;
      const sessionId = tab.getAttribute('data-agent-session-id');
      if (sessionId) {
        if (_replayModal) closeReplayModal();
        const session = sessions.get(sessionId);
        openReplayModal(sessionId, session || null).catch(err => log('Context menu replay error: ' + err));
      }
    });

    // Insert before the first direct-child separator or at end
    const sep = menu.querySelector(':scope > menuseparator');
    if (sep) {
      menu.insertBefore(menuItem, sep);
    } else {
      menu.appendChild(menuItem);
    }

    // Show/hide based on whether the right-clicked tab is an agent tab
    menu.addEventListener('popupshowing', () => {
      const tab = TabContextMenu.contextTab || gBrowser.selectedTab;
      const isAgent = tab && !!tab.getAttribute('data-agent-session-id');
      menuItem.hidden = !isAgent;
    });

    log('Replay context menu item added');
  }

  // ============================================
  // INITIALIZATION
  // ============================================

  let initRetries = 0;
  const MAX_INIT_RETRIES = 20;

  function init() {
    log('Initializing ZenRipple v' + VERSION + '...');

    if (!gBrowser || !gBrowser.tabs) {
      initRetries++;
      if (initRetries > MAX_INIT_RETRIES) {
        log('Failed to initialize after ' + MAX_INIT_RETRIES + ' retries. gBrowser not available.');
        return;
      }
      log('gBrowser not ready, retrying in 500ms (attempt ' + initRetries + '/' + MAX_INIT_RETRIES + ')');
      setTimeout(init, 500);
      return;
    }

    // Guard against uncaught errors — sine's observe() has no try/catch
    // around loadSubScriptWithOptions, so a throw here would prevent all
    // subsequent mods (e.g. ZenLeap) from loading.
    try {
      startServer(); // async — loads auth token then opens socket
      injectAgentTabStyles();
      registerActors();
      setupNavTracking();
      setupDialogObserver();
      setupTabEventTracking();
      setupPopupBlockedTracking();
      setupReplayShortcut();
      setupReplayContextMenu();

      log('ZenRipple v' + VERSION + ' initialized. Server on localhost:' + AGENT_PORT);
    } catch (e) {
      log('Initialization error (non-fatal): ' + e);
      console.error('[ZenRipple] init() failed:', e);
    }
  }

  // Clean up on window close
  window.addEventListener('unload', () => {
    stopServer();
  });

  // Start initialization
  if (document.readyState === 'complete') {
    init();
  } else {
    document.addEventListener('DOMContentLoaded', init);
  }

})();
