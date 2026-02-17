// ==UserScript==
// @name           Zen AI Agent - Browser Automation for Claude Code
// @description    WebSocket server exposing browser control via MCP for AI agents
// @include        main
// @author         Zen AI Agent
// @version        3.0.0
// ==/UserScript==

(function() {
  'use strict';

  const VERSION = '3.0.0';
  const AGENT_PORT = 9876;
  const WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
  const AGENT_WORKSPACE_NAME = 'Zen AI Agent';

  const logBuffer = [];
  const MAX_LOG_LINES = 200;
  const MAX_SESSION_TABS = 40;
  const MAX_INTERCEPT_RULES = 100;
  const MAX_CLIENT_FRAME_BUFFER = 20 * 1024 * 1024; // Incoming command payloads from MCP client
  const MAX_UPLOAD_SIZE = 8 * 1024 * 1024; // 8MB file input cap
  const MAX_UPLOAD_BASE64_LENGTH = 12 * 1024 * 1024; // ~1.5x file size with headroom

  function log(msg) {
    const line = new Date().toISOString() + ' ' + msg;
    console.log('[Zen AI Agent] ' + msg);
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
      this.tabEvents = [];              // max 200, per-session
      this.tabEventIndex = 0;           // monotonic
      this.recordingActive = false;
      this.recordedActions = [];
      this.createdAt = Date.now();
      this.lastActivity = Date.now();
      this.staleTimer = null;
    }

    pushTabEvent(event) {
      event._index = this.tabEventIndex++;
      this.tabEvents.push(event);
      if (this.tabEvents.length > 200) this.tabEvents.shift();
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

  function createSession() {
    const id = crypto.randomUUID();
    const session = new Session(id);
    sessions.set(id, session);
    log('Session created: ' + id);
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

    // Close ONLY this session's tabs
    // Copy to array first — removeTab triggers TabClose which modifies agentTabs during iteration
    const tabsToRemove = [...session.agentTabs];
    session.agentTabs.clear();
    for (const tab of tabsToRemove) {
      try {
        if (tab.parentNode) gBrowser.removeTab(tab);
      } catch (e) {
        log('Error removing tab during session destroy: ' + e);
      }
    }

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

  // ============================================
  // WEBSOCKET SERVER (XPCOM nsIServerSocket)
  // ============================================

  // Use a browser-global to prevent multiple instances across windows.
  // fx-autoconfig loads .uc.js per-window; we only want one server.
  const GLOBAL_KEY = '__zenleapAgentServer';

  let serverSocket = null;

  function startServer() {
    // Check if another window already started the server
    if (Services.appinfo && globalThis[GLOBAL_KEY]) {
      log('Server already running in another window — skipping');
      return;
    }

    // Clean up any stale server from a previous load
    stopServer();

    try {
      serverSocket = Cc['@mozilla.org/network/server-socket;1']
        .createInstance(Ci.nsIServerSocket);
      serverSocket.init(AGENT_PORT, true, -1); // loopback only
      serverSocket.asyncListen({
        onSocketAccepted(server, transport) {
          log('New connection from ' + transport.host + ':' + transport.port);
          // Accept all connections — multi-session support
          new WebSocketConnection(transport);
        },
        onStopListening(server, status) {
          log('Server stopped: ' + status);
        }
      });
      globalThis[GLOBAL_KEY] = true;
      // Start stale sweep
      staleSweepInterval = setInterval(staleSweep, STALE_SWEEP_MS);
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
    // Clear global state that outlives sessions
    interceptRules.length = 0;
    interceptNextId = 1;
    networkLog.length = 0;
    networkMonitorActive = false;
    pendingDialogs.length = 0;
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
    tabEventCursor = 0;  // index into session's tabEvents

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
        'X-ZenLeap-Session: ' + session.id + '\r\n' +
        'X-ZenLeap-Connection: ' + this.connectionId + '\r\n\r\n';

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
        const result = await Promise.race([
          handler(msg.params || {}, ctx).finally(() => clearTimeout(timeoutId)),
          new Promise((_, reject) => {
            timeoutId = setTimeout(() => reject(new Error('Command timed out after 120s')), 120000);
          })
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

        return { id: msg.id, result };
      } catch (e) {
        log('Error in ' + msg.method + ': ' + e);
        return {
          id: msg.id,
          error: { code: -1, message: e.message }
        };
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
        log('Found workspace: ' + AGENT_WORKSPACE_NAME + ' (' + agentWorkspaceId + ')');
        return agentWorkspaceId;
      }
    }

    // Create new workspace (dontChange=true to avoid UI blocking)
    try {
      const created = await gZenWorkspaces.createAndSaveWorkspace(
        AGENT_WORKSPACE_NAME, undefined, true
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

  function resolveTabScoped(tabId, session, conn) {
    if (!tabId) {
      // Prefer connection's tracked current tab
      if (conn.currentAgentTab && conn.currentAgentTab.linkedBrowser) {
        return conn.currentAgentTab;
      }
      // Fall back to first session tab
      const sessionTabs = getSessionTabs(session.id);
      if (sessionTabs.length > 0) {
        conn.currentAgentTab = sessionTabs[0];
        return sessionTabs[0];
      }
      // No tabs at all — return null (callers should create one or throw)
      return null;
    }

    // Search within session's tabs by data-agent-tab-id
    const sessionTabs = getSessionTabs(session.id);
    for (const tab of sessionTabs) {
      if (tab.getAttribute('data-agent-tab-id') === tabId) return tab;
    }
    // Match by linkedPanel ID
    for (const tab of sessionTabs) {
      if (tab.linkedPanel === tabId) return tab;
    }
    // Match by URL within session
    for (const tab of sessionTabs) {
      if (tab.linkedBrowser?.currentURI?.spec === tabId) return tab;
    }
    return null;
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
          ownerSession.agentTabs.add(tab);
          // Move to shared agent workspace
          if (agentWorkspaceId && gZenWorkspaces) {
            gZenWorkspaces.moveTabToWorkspace(tab, agentWorkspaceId);
          }
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
  // DIALOG HANDLING
  // ============================================

  const pendingDialogs = [];
  const dialogWindowRefs = new Map(); // dialog object → WeakRef(window)

  const dialogObserver = {
    observe(subject, topic, data) {
      if (topic !== 'common-dialog-loaded') return;
      try {
        const dialogWin = subject;
        const args = dialogWin.arguments?.[0];
        if (!args) return;
        const dialogInfo = {
          type: args.promptType || 'unknown', // alertCheck, confirmCheck, prompt
          message: args.text || '',
          default_value: args.value || '',
          timestamp: new Date().toISOString(),
        };
        // Use WeakRef to avoid retaining dialog window in memory
        dialogWindowRefs.set(dialogInfo, new WeakRef(dialogWin));
        pendingDialogs.push(dialogInfo);
        if (pendingDialogs.length > 20) {
          const old = pendingDialogs.shift();
          dialogWindowRefs.delete(old);
        }
        log('Dialog captured: ' + dialogInfo.type + ' — ' + dialogInfo.message.substring(0, 80));
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
  const interceptRules = [];       // {id, pattern: RegExp, action: 'block'|'modify_headers', headers: {}}
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
    'record_save', 'record_replay', 'get_tab_events', 'get_dialogs',
    'list_tabs', 'get_page_info', 'get_navigation_status',
    'network_get_log', 'intercept_list_rules', 'eval_chrome',
    'session_info', 'session_close', 'list_sessions',
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
          return { image: dataUrl, width: canvas.width, height: canvas.height };
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
      return wg.getActor('ZenLeapAgent');
    } catch (e) {
      log('getActor failed: ' + e + ' (url: ' + (browser.currentURI?.spec || '?') + ')');
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
    create_tab: async ({ url }, ctx) => {
      ensureSessionCanOpenTabs(ctx.session, 1);
      const wsId = await ensureAgentWorkspace();
      const tab = gBrowser.addTab(url || 'about:blank', {
        triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()
      });
      // Stamp stable ID and session ID before workspace move
      const stableId = tab.linkedPanel || ('agent-tab-' + Date.now());
      tab.setAttribute('data-agent-tab-id', stableId);
      tab.setAttribute('data-agent-session-id', ctx.session.id);
      ctx.session.agentTabs.add(tab);

      // Move tab to shared agent workspace
      if (wsId && gZenWorkspaces) {
        gZenWorkspaces.moveTabToWorkspace(tab, wsId);
      }
      ctx.connection.currentAgentTab = tab;
      // Only set selectedTab if agent workspace is active
      try {
        if (gZenWorkspaces && gZenWorkspaces.activeWorkspace === wsId) {
          gBrowser.selectedTab = tab;
        }
      } catch (e) { /* ignore — workspace may not be active */ }
      log('Created tab: ' + stableId + ' -> ' + (url || 'about:blank') + ' [session:' + ctx.session.id.substring(0, 8) + ']');

      return {
        tab_id: stableId,
        url: url || 'about:blank'
      };
    },

    close_tab: async ({ tab_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      if (!tab) throw new Error('Tab not found');
      if (ctx.connection.currentAgentTab === tab) ctx.connection.currentAgentTab = null;
      ctx.session.agentTabs.delete(tab);
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

    // --- Dialogs (global — browser-wide) ---
    get_dialogs: async () => {
      return pendingDialogs.map(d => ({
        type: d.type,
        message: d.message,
        default_value: d.default_value,
        timestamp: d.timestamp,
      }));
    },

    handle_dialog: async ({ action, text }) => {
      if (!action) throw new Error('action is required (accept or dismiss)');
      if (pendingDialogs.length === 0) throw new Error('No pending dialogs');
      const dialog = pendingDialogs.shift();
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
        return { success: true, action, type: dialog.type };
      } catch (e) {
        return { success: false, error: e.message };
      }
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

    // --- Observation ---
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
      return await actor.sendQuery('ZenLeapAgent:ExtractDOM', {
        viewport_only: !!viewport_only,
        max_elements: max_elements || 0,
        incremental: !!incremental,
      });
    },

    get_page_text: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:GetPageText');
    },

    get_page_html: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:GetPageHTML');
    },

    get_accessibility_tree: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:GetAccessibilityTree');
    },

    // --- Interaction ---
    click_element: async ({ tab_id, frame_id, index }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:ClickElement', { index }, null, frame_id);
    },

    click_coordinates: async ({ tab_id, frame_id, x, y }, ctx) => {
      if (x === undefined || y === undefined) throw new Error('x and y are required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:ClickCoordinates', { x, y }, null, frame_id);
    },

    fill_field: async ({ tab_id, frame_id, index, value }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      if (value === undefined) throw new Error('value is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:FillField', { index, value: String(value) }, null, frame_id);
    },

    select_option: async ({ tab_id, frame_id, index, value }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      if (value === undefined) throw new Error('value is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:SelectOption', { index, value: String(value) }, null, frame_id);
    },

    type_text: async ({ tab_id, frame_id, text }, ctx) => {
      if (!text) throw new Error('text is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:TypeText', { text }, null, frame_id);
    },

    press_key: async ({ tab_id, frame_id, key, modifiers }, ctx) => {
      if (!key) throw new Error('key is required');
      const mods = modifiers || {};
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:PressKey', { key, modifiers: mods }, { success: true, key }, frame_id);
    },

    scroll: async ({ tab_id, frame_id, direction, amount }, ctx) => {
      if (!direction) throw new Error('direction is required (up/down/left/right)');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:Scroll', { direction, amount: amount || 500 }, null, frame_id);
    },

    hover: async ({ tab_id, frame_id, index }, ctx) => {
      if (index === undefined || index === null) throw new Error('index is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:Hover', { index }, null, frame_id);
    },

    // --- Console / Eval ---
    console_setup: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:SetupConsoleCapture', {}, null, frame_id);
    },

    console_teardown: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(tab, 'ZenLeapAgent:TeardownConsoleCapture', {}, null, frame_id);
    },

    console_get_logs: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:GetConsoleLogs');
    },

    console_get_errors: async ({ tab_id, frame_id }, ctx) => {
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:GetConsoleErrors');
    },

    console_evaluate: async ({ tab_id, frame_id, expression }, ctx) => {
      if (!expression) throw new Error('expression is required');
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:EvalJS', { expression });
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
          const result = await actor.sendQuery('ZenLeapAgent:QuerySelector', { selector });
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
          const result = await actor.sendQuery('ZenLeapAgent:SearchText', { text });
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
      return await actor.sendQuery('ZenLeapAgent:SetCookie', { cookie: cookieStr });
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
      return await actor.sendQuery('ZenLeapAgent:GetStorage', { storage_type, key });
    },

    set_storage: async ({ tab_id, frame_id, storage_type, key, value }, ctx) => {
      if (!storage_type || !key) throw new Error('storage_type and key are required');
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:SetStorage', { storage_type, key, value: String(value) });
    },

    delete_storage: async ({ tab_id, frame_id, storage_type, key }, ctx) => {
      if (!storage_type) throw new Error('storage_type is required');
      const tab = ctx.resolveTab(tab_id);
      const actor = getActorForTab(tab, frame_id);
      return await actor.sendQuery('ZenLeapAgent:DeleteStorage', { storage_type, key });
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
        const re = new RegExp(url_filter, 'i');
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
    intercept_add_rule: async ({ pattern, action, headers }) => {
      if (!pattern || !action) throw new Error('pattern and action are required');
      if (!['block', 'modify_headers'].includes(action)) {
        throw new Error('action must be "block" or "modify_headers"');
      }
      ensureNetworkObserver();
      const compiled = new RegExp(pattern, 'i');
      const normalizedHeaders = headers || {};
      const existing = interceptRules.find(r =>
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
        pattern: compiled,
        action,
        headers: normalizedHeaders,
      });
      return { success: true, rule_id: id };
    },

    intercept_remove_rule: async ({ rule_id }) => {
      if (!rule_id) throw new Error('rule_id is required');
      const idx = interceptRules.findIndex(r => r.id === rule_id);
      if (idx === -1) throw new Error('Rule not found: ' + rule_id);
      interceptRules.splice(idx, 1);
      return { success: true };
    },

    intercept_list_rules: async () => {
      return interceptRules.map(r => ({
        id: r.id,
        pattern: r.pattern.source,
        action: r.action,
        headers: r.headers,
      }));
    },

    // --- Session Persistence (Phase 7) — scoped to session ---
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
          ctx.session.agentTabs.add(tab);
          if (wsId && gZenWorkspaces) {
            gZenWorkspaces.moveTabToWorkspace(tab, wsId);
          }
          tabsRestored++;
        }
      }
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
          const page = await actor.sendQuery('ZenLeapAgent:GetPageText');
          textPreview = (page.text || '').substring(0, 500);
        } catch (e) {
          textPreview = '(unable to get text: ' + e.message + ')';
        }
        results.push({ tab_id: tid, url, title, text_preview: textPreview });
      }
      return results;
    },

    batch_navigate: async ({ urls }, ctx) => {
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
        ctx.session.agentTabs.add(tab);
        if (wsId && gZenWorkspaces) {
          gZenWorkspaces.moveTabToWorkspace(tab, wsId);
        }
        opened.push({ tab_id: stableId, url });
      }
      return { success: true, tabs: opened };
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
        Cu.nukeSandbox(sandbox);
      }
    },

    // --- Drag-and-Drop (Phase 10) ---
    drag_element: async ({ tab_id, frame_id, sourceIndex, targetIndex, steps }, ctx) => {
      if (sourceIndex === undefined || sourceIndex === null) throw new Error('sourceIndex is required');
      if (targetIndex === undefined || targetIndex === null) throw new Error('targetIndex is required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(
        tab, 'ZenLeapAgent:DragElement',
        { sourceIndex, targetIndex, steps: steps || 10 },
        null, frame_id
      );
    },

    drag_coordinates: async ({ tab_id, frame_id, startX, startY, endX, endY, steps }, ctx) => {
      if (startX === undefined || startY === undefined) throw new Error('startX and startY are required');
      if (endX === undefined || endY === undefined) throw new Error('endX and endY are required');
      const tab = ctx.resolveTab(tab_id);
      return await actorInteraction(
        tab, 'ZenLeapAgent:DragCoordinates',
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
          tab, 'ZenLeapAgent:FileUpload',
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
        workspace_name: AGENT_WORKSPACE_NAME,
        workspace_id: agentWorkspaceId,
        connection_id: ctx.connection.connectionId,
        connection_count: ctx.session.connections.size,
        tab_count: getSessionTabs(ctx.session.id).length,
        created_at: ctx.session.createdAt,
      };
    },

    session_close: async (params, ctx) => {
      const sessionId = ctx.session.id;
      const tabCount = getSessionTabs(sessionId).length;
      // Defer destruction so this response is sent first
      setTimeout(() => destroySession(sessionId), 50);
      return { success: true, session_id: sessionId, tabs_closed: tabCount };
    },

    list_sessions: async () => {
      const result = [];
      for (const [id, session] of sessions) {
        result.push({
          session_id: id,
          workspace_name: AGENT_WORKSPACE_NAME,
          connection_count: session.connections.size,
          tab_count: getSessionTabs(id).length,
          created_at: session.createdAt,
        });
      }
      return result;
    },
  };

  // ============================================
  // ACTOR REGISTRATION
  // ============================================

  const ACTOR_GLOBAL_KEY = '__zenleapActorsRegistered';

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
      resProto.setSubstitution('zenleap-agent', Services.io.newFileURI(actorsDir));
      log('Registered resource://zenleap-agent/ -> ' + actorsDir.path);

      const parentURI = 'resource://zenleap-agent/ZenLeapAgentParent.sys.mjs';
      const childURI = 'resource://zenleap-agent/ZenLeapAgentChild.sys.mjs';

      ChromeUtils.registerWindowActor('ZenLeapAgent', {
        parent: { esModuleURI: parentURI },
        child: { esModuleURI: childURI },
        allFrames: true,
        matches: ['*://*/*'],
      });

      globalThis[ACTOR_GLOBAL_KEY] = true;
      log('JSWindowActor ZenLeapAgent registered');
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
  // INITIALIZATION
  // ============================================

  let initRetries = 0;
  const MAX_INIT_RETRIES = 20;

  function init() {
    log('Initializing Zen AI Agent v' + VERSION + '...');

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

    startServer();
    registerActors();
    setupNavTracking();
    setupDialogObserver();
    setupTabEventTracking();

    log('Zen AI Agent v' + VERSION + ' initialized. Server on localhost:' + AGENT_PORT);
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
