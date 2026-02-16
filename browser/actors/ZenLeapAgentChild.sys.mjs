// ZenLeapAgentChild.sys.mjs — Content-process actor for DOM extraction and page content.
// Runs in the content process under Fission; communicates with parent via sendQuery/receiveMessage.

const MAX_TEXT_LENGTH = 200000;  // 200K chars for page text
const MAX_HTML_LENGTH = 500000;  // 500K chars for page HTML

const INTERACTIVE_TAGS = new Set([
  'a', 'button', 'input', 'select', 'textarea', 'details', 'summary',
]);

const INTERACTIVE_ROLES = new Set([
  'button', 'link', 'textbox', 'checkbox', 'radio', 'combobox',
  'menuitem', 'tab', 'switch', 'option',
]);

// Characters requiring Shift modifier (US keyboard layout)
const SHIFT_CHARS = new Set('~!@#$%^&*()_+{}|:"<>?');

// Shifted character → base key (US keyboard layout)
const SHIFT_MAP = {
  '~': '`', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
  '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
  '_': '-', '+': '=', '{': '[', '}': ']', '|': '\\',
  ':': ';', '"': "'", '<': ',', '>': '.', '?': '/',
};

// nsITextInputProcessor flag: key is non-printable (Enter, Tab, Arrow, etc.)
const TIP_KEY_NON_PRINTABLE = 0x02;

export class ZenLeapAgentChild extends JSWindowActorChild {
  #elementMap = new Map(); // index → WeakRef(element)
  #elementMeta = new Map(); // index → {tag, text, href, name, type, ariaLabel} for self-healing
  #previousDOM = null; // previous DOM snapshot for incremental diffing
  #consoleLogs = [];
  #consoleErrors = [];
  #captureSetup = false;
  #cursorOverlay = null;
  #tip = null; // nsITextInputProcessor instance (cached)

  receiveMessage(message) {
    const data = message.data || {};
    switch (message.name) {
      case 'ZenLeapAgent:ExtractDOM':
        return this.#extractDOM(data);
      case 'ZenLeapAgent:GetPageText':
        return this.#getPageText();
      case 'ZenLeapAgent:GetPageHTML':
        return this.#getPageHTML();
      case 'ZenLeapAgent:GetAccessibilityTree':
        return this.#getAccessibilityTree();
      case 'ZenLeapAgent:ClickElement':
        return this.#clickElement(data.index);
      case 'ZenLeapAgent:FillField':
        return this.#fillField(data.index, data.value);
      case 'ZenLeapAgent:SelectOption':
        return this.#selectOption(data.index, data.value);
      case 'ZenLeapAgent:TypeText':
        return this.#typeText(data.text);
      case 'ZenLeapAgent:PressKey':
        return this.#pressKey(data.key, data.modifiers || {});
      case 'ZenLeapAgent:Scroll':
        return this.#scroll(data.direction, data.amount);
      case 'ZenLeapAgent:Hover':
        return this.#hover(data.index);
      case 'ZenLeapAgent:ClickCoordinates':
        return this.#clickCoordinates(data.x, data.y);
      case 'ZenLeapAgent:SetupConsoleCapture':
        return this.#setupConsoleCapture();
      case 'ZenLeapAgent:GetConsoleLogs':
        return { logs: [...this.#consoleLogs] };
      case 'ZenLeapAgent:GetConsoleErrors':
        return { errors: [...this.#consoleErrors] };
      case 'ZenLeapAgent:EvalJS':
        return this.#evalInContent(data.expression);
      case 'ZenLeapAgent:QuerySelector':
        return this.#querySelector(data.selector);
      case 'ZenLeapAgent:SearchText':
        return this.#searchText(data.text);
      case 'ZenLeapAgent:GetStorage':
        return this.#getStorage(data.storage_type, data.key);
      case 'ZenLeapAgent:SetStorage':
        return this.#setStorage(data.storage_type, data.key, data.value);
      case 'ZenLeapAgent:DeleteStorage':
        return this.#deleteStorage(data.storage_type, data.key);
      case 'ZenLeapAgent:SetCookie':
        return this.#setCookie(data.cookie);
      case 'ZenLeapAgent:GetContentCookies':
        return this.#getContentCookies();
      case 'ZenLeapAgent:DragElement':
        return this.#dragElement(data);
      case 'ZenLeapAgent:DragCoordinates':
        return this.#dragCoordinates(data);
      case 'ZenLeapAgent:FileUpload':
        return this.#fileUpload(data.index, data.base64, data.filename, data.mimeType);
      default:
        return { error: 'Unknown message: ' + message.name };
    }
  }

  // --- DOM Extraction ---

  #extractDOM(opts = {}) {
    const doc = this.contentWindow?.document;
    if (!doc?.body) {
      return {
        elements: [],
        url: doc?.location?.href || '',
        title: doc?.title || '',
      };
    }

    const viewportOnly = opts.viewport_only || false;
    const maxElements = opts.max_elements || 0;
    const incremental = opts.incremental || false;
    const viewportH = this.contentWindow.innerHeight;

    const elements = [];
    this.#elementMap.clear();
    this.#elementMeta.clear();
    let index = 0;
    const MAX_DEPTH = 50;

    const walk = (node, depth = 0) => {
      if (node.nodeType !== 1) return; // ELEMENT_NODE only
      if (depth > MAX_DEPTH) return;
      if (maxElements > 0 && index >= maxElements) return;

      const tag = node.tagName.toLowerCase();
      const role = node.getAttribute('role');
      const isInteractive =
        INTERACTIVE_TAGS.has(tag) ||
        INTERACTIVE_ROLES.has(role) ||
        node.hasAttribute('onclick') ||
        (node.hasAttribute('tabindex') && node.getAttribute('tabindex') !== '-1') ||
        node.getAttribute('contenteditable') === 'true';

      // Mark iframes with their browsingContext ID for frame_id targeting
      if (tag === 'iframe' && this.#isVisible(node)) {
        const frameBC = node.browsingContext;
        if (frameBC) {
          const r = node.getBoundingClientRect();
          // Skip if viewport_only and element is outside viewport
          if (viewportOnly && (r.bottom < 0 || r.top > viewportH)) {
            // still walk children but don't index
          } else {
            elements.push({
              index: index,
              tag: 'iframe',
              text: node.getAttribute('title') || node.getAttribute('name') || '',
              attributes: {
                src: node.src || '',
                name: node.name || undefined,
                frame_id: frameBC.id,
              },
              rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
            });
            this.#elementMap.set(index, new WeakRef(node));
            this.#elementMeta.set(index, { tag: 'iframe', text: node.getAttribute('title') || '', href: node.src || '' });
            index++;
          }
        }
      }

      if (isInteractive && this.#isVisible(node)) {
        const rect = node.getBoundingClientRect();

        // Skip off-viewport elements when viewport_only is enabled
        if (viewportOnly && (rect.bottom < 0 || rect.top > viewportH)) {
          // Don't index but still recurse children
        } else if (maxElements > 0 && index >= maxElements) {
          // Already at max — stop adding
        } else {
          const attrs = {};
          if (node.type) attrs.type = node.type;
          if (node.name) attrs.name = node.name;
          if (node.href) attrs.href = node.href;
          if (node.value) attrs.value = node.value.substring(0, 50);
          if (node.checked !== undefined) attrs.checked = node.checked;
          if (node.disabled) attrs.disabled = true;

          const text = this.#getVisibleText(node).substring(0, 100);

          this.#elementMap.set(index, new WeakRef(node));
          // Store metadata for self-healing selector recovery (Phase 9)
          this.#elementMeta.set(index, {
            tag,
            text: text.substring(0, 80),
            href: node.href || '',
            name: node.name || '',
            type: node.type || '',
            ariaLabel: node.getAttribute('aria-label') || '',
          });
          elements.push({
            index: index++,
            tag,
            role: role || undefined,
            text,
            attributes: attrs,
            rect: {
              x: Math.round(rect.x),
              y: Math.round(rect.y),
              w: Math.round(rect.width),
              h: Math.round(rect.height),
            },
          });
        }
      }

      // Enter shadow DOM (openOrClosedShadowRoot is Gecko-specific, handles closed roots)
      const shadow = node.openOrClosedShadowRoot || node.shadowRoot;
      if (shadow) {
        for (const child of shadow.children) walk(child, depth + 1);
      }

      for (const child of node.children) walk(child, depth + 1);
    };

    walk(doc.body);

    const result = {
      elements,
      url: doc.location?.href || '',
      title: doc.title || '',
      total: elements.length,
    };

    // Incremental diffing: compare with previous snapshot
    if (incremental && this.#previousDOM) {
      const diff = this.#computeDOMDiff(this.#previousDOM, elements);
      result.diff = diff;
      result.incremental = true;
    }

    // Store current snapshot for next incremental call
    this.#previousDOM = elements.map(el => ({
      tag: el.tag,
      text: el.text,
      href: el.attributes?.href || '',
      name: el.attributes?.name || '',
    }));

    return result;
  }

  #computeDOMDiff(prev, current) {
    // Key elements by tag|text|href|name for stable identity
    const keyOf = (el) => (el.tag || '') + '|' + (el.text || '') + '|' + (el.href || el.attributes?.href || '') + '|' + (el.name || el.attributes?.name || '');

    const prevKeys = new Set(prev.map(keyOf));
    const currKeys = new Set(current.map(keyOf));

    const added = current.filter(el => !prevKeys.has(keyOf(el))).map(el => ({
      index: el.index, tag: el.tag, text: el.text,
    }));
    const removed = prev.filter(el => !currKeys.has(keyOf(el))).map(el => ({
      tag: el.tag, text: el.text,
    }));

    return {
      added: added.length,
      removed: removed.length,
      total: current.length,
      added_elements: added.slice(0, 20),  // Cap at 20 to limit response size
      removed_elements: removed.slice(0, 20),
    };
  }

  #isVisible(el) {
    const style = this.contentWindow.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  #getVisibleText(el) {
    return (
      el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') ||
      el.getAttribute('alt') ||
      el.getAttribute('title') ||
      el.textContent?.trim() ||
      ''
    );
  }

  // --- Page Text ---

  #getPageText() {
    const doc = this.contentWindow?.document;
    if (!doc?.body) return { text: '' };
    let text = doc.body.innerText || '';
    if (text.length > MAX_TEXT_LENGTH) {
      text = text.substring(0, MAX_TEXT_LENGTH) + '\n[...truncated at 200K chars]';
    }
    return { text };
  }

  // --- Page HTML ---

  #getPageHTML() {
    const doc = this.contentWindow?.document;
    if (!doc?.documentElement) return { html: '' };
    let html = doc.documentElement.outerHTML || '';
    if (html.length > MAX_HTML_LENGTH) {
      html = html.substring(0, MAX_HTML_LENGTH) + '\n<!-- truncated at 500K chars -->';
    }
    return { html };
  }

  // --- Interaction ---

  #getElement(index) {
    const ref = this.#elementMap.get(index);
    if (!ref) throw new Error('Element index ' + index + ' not found — run get_dom first');
    const el = ref.deref();
    if (el && el.isConnected) return el;

    // Self-healing: try to re-find the element using stored metadata
    const meta = this.#elementMeta.get(index);
    if (meta) {
      const healed = this.#tryHealElement(meta);
      if (healed) {
        this.#elementMap.set(index, new WeakRef(healed));
        return healed;
      }
    }

    if (!el) throw new Error('Element index ' + index + ' was garbage collected — run get_dom again');
    throw new Error('Element index ' + index + ' is no longer in the DOM — run get_dom again');
  }

  #tryHealElement(meta) {
    const doc = this.contentWindow?.document;
    if (!doc?.body) return null;

    // Strategy 1: aria-label match
    if (meta.ariaLabel) {
      const candidates = doc.querySelectorAll('[aria-label="' + CSS.escape(meta.ariaLabel) + '"]');
      if (candidates.length === 1) return candidates[0];
    }

    // Strategy 2: href match (for links)
    if (meta.href && meta.tag === 'a') {
      const candidates = doc.querySelectorAll('a[href="' + CSS.escape(meta.href) + '"]');
      if (candidates.length === 1) return candidates[0];
    }

    // Strategy 3: tag + text match
    if (meta.text && meta.tag) {
      const all = doc.querySelectorAll(meta.tag);
      const textMatches = Array.from(all).filter(el =>
        this.#getVisibleText(el).substring(0, 80) === meta.text
      );
      if (textMatches.length === 1) return textMatches[0];
    }

    // Strategy 4: name attribute match (for form elements)
    if (meta.name && meta.tag) {
      const candidates = doc.querySelectorAll(meta.tag + '[name="' + CSS.escape(meta.name) + '"]');
      if (candidates.length === 1) return candidates[0];
    }

    return null;
  }

  #clickElement(index) {
    const el = this.#getElement(index);
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const rect = el.getBoundingClientRect();
    const cx = rect.x + rect.width / 2;
    const cy = rect.y + rect.height / 2;
    this.#showCursor(cx, cy);
    // Use windowUtils.sendMouseEvent for native-level trusted mouse events
    const utils = this.contentWindow?.windowUtils;
    let method;
    if (utils?.sendMouseEvent) {
      method = 'windowUtils';
      utils.sendMouseEvent('mousedown', cx, cy, 0, 1, 0);
      utils.sendMouseEvent('mouseup', cx, cy, 0, 1, 0);
    } else {
      method = 'el.click';
      el.click();
    }
    // Always ensure focus — sendMouseEvent doesn't trigger focus change,
    // and el.click() doesn't always focus either
    el.focus();
    const doc = this.contentWindow?.document;
    return {
      success: true,
      tag: el.tagName.toLowerCase(),
      text: this.#getVisibleText(el).substring(0, 100),
      method,
      focused: doc?.activeElement === el,
    };
  }

  #fillField(index, value) {
    const el = this.#getElement(index);
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && el.getAttribute('contenteditable') !== 'true') {
      throw new Error('Element [' + index + '] is <' + tag + '>, not a fillable field');
    }
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    el.focus();
    if (el.getAttribute('contenteditable') === 'true') {
      el.textContent = value;
    } else {
      // Use the correct prototype's setter based on element type.
      // HTMLInputElement.prototype.value and HTMLTextAreaElement.prototype.value
      // are DIFFERENT setters — using the wrong one throws.
      const nativeSetter = this.#getValueSetter(el);
      if (nativeSetter) {
        nativeSetter.call(el, value);
      } else {
        el.value = value;
      }
    }
    el.dispatchEvent(new this.contentWindow.Event('input', { bubbles: true }));
    el.dispatchEvent(new this.contentWindow.Event('change', { bubbles: true }));
    return { success: true, tag, value: value.substring(0, 50) };
  }

  #getValueSetter(el) {
    const win = this.contentWindow;
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') {
      return Object.getOwnPropertyDescriptor(
        win.HTMLTextAreaElement.prototype, 'value'
      )?.set;
    }
    if (tag === 'input') {
      return Object.getOwnPropertyDescriptor(
        win.HTMLInputElement.prototype, 'value'
      )?.set;
    }
    return null;
  }

  #selectOption(index, value) {
    const el = this.#getElement(index);
    if (el.tagName.toLowerCase() !== 'select') {
      throw new Error('Element [' + index + '] is <' + el.tagName.toLowerCase() + '>, not a <select>');
    }
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    el.focus();
    // Try matching by value first, then by visible text
    let found = false;
    for (const opt of el.options) {
      if (opt.value === value || opt.textContent.trim() === value) {
        el.value = opt.value;
        found = true;
        break;
      }
    }
    if (!found) {
      const available = Array.from(el.options).map(o => o.value + ' ("' + o.textContent.trim() + '")').join(', ');
      throw new Error('Option "' + value + '" not found. Available: ' + available);
    }
    el.dispatchEvent(new this.contentWindow.Event('change', { bubbles: true }));
    return { success: true, value: el.value };
  }

  // --- nsITextInputProcessor (TIP) ---
  // Produces trusted keyboard events (isTrusted:true) through Gecko's full
  // event pipeline. Unlike dispatchEvent, TIP events are indistinguishable
  // from real user keypresses — works for canvas-based apps (Google Sheets),
  // contenteditable, and standard form fields. Targets the specific content
  // window without stealing OS-level focus from other windows.

  #getTextInputProcessor() {
    const win = this.contentWindow;
    if (!win) throw new Error('No content window');
    if (!this.#tip) {
      this.#tip = Cc['@mozilla.org/text-input-processor;1']
        .createInstance(Ci.nsITextInputProcessor);
    }
    // Begin (or re-validate) transaction targeting this content window
    if (!this.#tip.beginInputTransactionForTests(win)) {
      // Stale — create fresh instance
      this.#tip = Cc['@mozilla.org/text-input-processor;1']
        .createInstance(Ci.nsITextInputProcessor);
      if (!this.#tip.beginInputTransactionForTests(win)) {
        throw new Error('Cannot begin text input transaction');
      }
    }
    return this.#tip;
  }

  #charToCode(char) {
    const c = char.toLowerCase();
    if (c >= 'a' && c <= 'z') return 'Key' + c.toUpperCase();
    if (c >= '0' && c <= '9') return 'Digit' + c;
    const map = {
      ' ': 'Space', '-': 'Minus', '=': 'Equal',
      '[': 'BracketLeft', ']': 'BracketRight',
      '\\': 'Backslash', ';': 'Semicolon', "'": 'Quote',
      ',': 'Comma', '.': 'Period', '/': 'Slash', '`': 'Backquote',
    };
    return map[c] || '';
  }

  // Compute DOM keyCode for a character. Apps like Google Sheets check keyCode
  // (not just key/code) to handle character input on their canvas.
  #charToKeyCode(char) {
    // Shifted symbols → use base key's keyCode
    const base = SHIFT_MAP[char];
    if (base) return this.#charToKeyCode(base);
    const c = char.toUpperCase();
    if (c >= 'A' && c <= 'Z') return c.charCodeAt(0);     // 65-90
    if (char >= '0' && char <= '9') return char.charCodeAt(0); // 48-57
    if (char === ' ') return 32;
    const punctMap = {
      ';': 186, '=': 187, ',': 188, '-': 189, '.': 190, '/': 191,
      '`': 192, '[': 219, '\\': 220, ']': 221, "'": 222,
    };
    return punctMap[char] || 0;
  }

  async #typeText(text) {
    let tip;
    try {
      tip = this.#getTextInputProcessor();
    } catch (e) {
      // TIP unavailable — fall back to value-setter approach
      const result = this.#typeTextFallback(text, this.contentWindow);
      result.method = 'fallback';
      return result;
    }

    const win = this.contentWindow;
    const KE = win.KeyboardEvent;
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      try {
        // Control characters → special key presses
        // Tab/Enter trigger async focus changes in apps like Google Sheets.
        // We must wait after sending them so the app can finish cell/row
        // navigation before we type the next character.
        if (char === '\t') {
          const e = new KE('', { key: 'Tab', code: 'Tab' });
          tip.keydown(e, TIP_KEY_NON_PRINTABLE);
          tip.keyup(e, TIP_KEY_NON_PRINTABLE);
          await new Promise(r => win.setTimeout(r, 50));
          try { tip = this.#getTextInputProcessor(); } catch (_) { break; }
          continue;
        }
        if (char === '\n' || char === '\r') {
          const e = new KE('', { key: 'Enter', code: 'Enter' });
          tip.keydown(e, TIP_KEY_NON_PRINTABLE);
          tip.keyup(e, TIP_KEY_NON_PRINTABLE);
          await new Promise(r => win.setTimeout(r, 50));
          try { tip = this.#getTextInputProcessor(); } catch (_) { break; }
          continue;
        }

        // Determine if Shift is needed
        const isUpper = char >= 'A' && char <= 'Z';
        const isShiftSym = SHIFT_CHARS.has(char);
        const needsShift = isUpper || isShiftSym;

        // Physical key code and DOM keyCode (based on base character)
        const code = isShiftSym
          ? this.#charToCode(SHIFT_MAP[char])
          : this.#charToCode(char);
        const keyCode = this.#charToKeyCode(char);

        if (needsShift) {
          tip.keydown(new KE('', { key: 'Shift', code: 'ShiftLeft', keyCode: 16 }));
        }

        const event = new KE('', { key: char, code, keyCode });
        tip.keydown(event);
        tip.keyup(event);

        if (needsShift) {
          tip.keyup(new KE('', { key: 'Shift', code: 'ShiftLeft' }));
        }
      } catch (e) {
        // TIP may fail if page navigated (Tab/Enter can cause this)
        return { success: true, typed: i, total: text.length, method: 'textInputProcessor' };
      }
    }

    return { success: true, length: text.length, method: 'textInputProcessor' };
  }

  #typeTextFallback(text, win) {
    const doc = win.document;
    const target = doc?.activeElement || doc?.body;
    if (!target) throw new Error('No active element to type into');

    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') {
      const nativeSetter = this.#getValueSetter(target);
      const current = target.value || '';
      if (nativeSetter) {
        nativeSetter.call(target, current + text);
      } else {
        target.value = current + text;
      }
      target.dispatchEvent(new win.Event('input', { bubbles: true }));
      target.dispatchEvent(new win.Event('change', { bubbles: true }));
    } else if (target.getAttribute('contenteditable') === 'true') {
      target.textContent = (target.textContent || '') + text;
      target.dispatchEvent(new win.Event('input', { bubbles: true }));
    }
    return { success: true, length: text.length };
  }

  #pressKey(key, modifiers) {
    const win = this.contentWindow;
    if (!win) throw new Error('No content window for key press');

    // Normalize key name
    if (key === 'Space') key = ' ';

    // Keys that can destroy the actor (navigation, focus loss)
    const destructive = new Set(['Tab', 'Escape', 'Enter']);
    const shouldDefer = destructive.has(key);

    const execute = () => {
      let tip;
      try {
        tip = this.#getTextInputProcessor();
      } catch (e) {
        this.#pressKeyFallback(key, modifiers, win);
        return;
      }

      const KE = win.KeyboardEvent;
      const mods = [];

      // Activate modifier keys
      if (modifiers.shift) {
        const e = new KE('', { key: 'Shift', code: 'ShiftLeft' });
        tip.keydown(e); mods.push(e);
      }
      if (modifiers.ctrl) {
        const e = new KE('', { key: 'Control', code: 'ControlLeft' });
        tip.keydown(e); mods.push(e);
      }
      if (modifiers.alt) {
        const e = new KE('', { key: 'Alt', code: 'AltLeft' });
        tip.keydown(e); mods.push(e);
      }
      if (modifiers.meta) {
        const e = new KE('', { key: 'Meta', code: 'MetaLeft' });
        tip.keydown(e); mods.push(e);
      }

      // Determine code, keyCode, and flags
      const isNonPrintable = key.length > 1;
      const code = isNonPrintable ? key : this.#charToCode(key);
      const flags = isNonPrintable ? TIP_KEY_NON_PRINTABLE : 0;
      const keyCode = isNonPrintable ? 0 : this.#charToKeyCode(key);

      const event = new KE('', { key, code, keyCode });
      tip.keydown(event, flags);
      tip.keyup(event, flags);

      // Deactivate modifiers in reverse order
      for (const mod of mods.reverse()) {
        tip.keyup(mod);
      }
    };

    if (shouldDefer) {
      win.setTimeout(() => {
        try { execute(); } catch (e) { /* actor may be destroyed */ }
      }, 0);
    } else {
      execute();
    }

    return { success: true, key, method: 'textInputProcessor' };
  }

  #pressKeyFallback(key, modifiers, win) {
    const doc = win.document;
    const target = doc?.activeElement || doc?.body;
    if (!target) return;
    const opts = {
      key,
      bubbles: true,
      ctrlKey: !!modifiers.ctrl,
      shiftKey: !!modifiers.shift,
      altKey: !!modifiers.alt,
      metaKey: !!modifiers.meta,
    };
    target.dispatchEvent(new win.KeyboardEvent('keydown', opts));
    target.dispatchEvent(new win.KeyboardEvent('keyup', opts));
  }

  #scroll(direction, amount) {
    const win = this.contentWindow;
    if (!win) throw new Error('No content window');
    const px = amount || 500;
    switch (direction) {
      case 'up':    win.scrollBy(0, -px); break;
      case 'down':  win.scrollBy(0, px); break;
      case 'left':  win.scrollBy(-px, 0); break;
      case 'right': win.scrollBy(px, 0); break;
      default: throw new Error('Invalid direction: ' + direction + ' (use up/down/left/right)');
    }
    return {
      success: true,
      scrollX: Math.round(win.scrollX),
      scrollY: Math.round(win.scrollY),
    };
  }

  #hover(index) {
    const el = this.#getElement(index);
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const rect = el.getBoundingClientRect();
    const cx = rect.x + rect.width / 2;
    const cy = rect.y + rect.height / 2;
    // Use windowUtils for native-level mouse events
    const utils = this.contentWindow?.windowUtils;
    if (utils?.sendMouseEvent) {
      utils.sendMouseEvent('mousemove', cx, cy, 0, 0, 0);
    } else {
      const opts = { bubbles: true, clientX: cx, clientY: cy };
      el.dispatchEvent(new this.contentWindow.MouseEvent('mouseenter', opts));
      el.dispatchEvent(new this.contentWindow.MouseEvent('mouseover', opts));
      el.dispatchEvent(new this.contentWindow.MouseEvent('mousemove', opts));
    }
    return { success: true, tag: el.tagName.toLowerCase(), text: this.#getVisibleText(el).substring(0, 100) };
  }

  #clickCoordinates(x, y) {
    const win = this.contentWindow;
    const doc = win?.document;
    if (!doc) throw new Error('No document');
    this.#showCursor(x, y);
    const el = doc.elementFromPoint(x, y);
    // Use windowUtils for native-level trusted mouse events
    const utils = win.windowUtils;
    if (utils?.sendMouseEvent) {
      utils.sendMouseEvent('mousedown', x, y, 0, 1, 0);
      utils.sendMouseEvent('mouseup', x, y, 0, 1, 0);
      // sendMouseEvent doesn't trigger focus change — ensure focus explicitly
      if (el) el.focus();
    } else {
      if (!el) throw new Error('No element at coordinates (' + x + ', ' + y + ')');
      const opts = { bubbles: true, clientX: x, clientY: y };
      el.dispatchEvent(new win.MouseEvent('mousedown', opts));
      el.dispatchEvent(new win.MouseEvent('mouseup', opts));
      el.dispatchEvent(new win.MouseEvent('click', opts));
    }
    return {
      success: true,
      tag: el?.tagName?.toLowerCase() || 'unknown',
      text: el ? this.#getVisibleText(el).substring(0, 100) : '',
    };
  }

  // --- Drag-and-Drop ---

  #performDrag(startX, startY, endX, endY, steps = 10) {
    const win = this.contentWindow;
    const doc = win?.document;
    if (!win || !doc) throw new Error('No content window');

    const utils = win.windowUtils;

    // Phase 1: Native mouse events (mousedown → mousemove steps → mouseup)
    if (utils?.sendMouseEvent) {
      utils.sendMouseEvent('mousedown', startX, startY, 0, 1, 0);
      for (let i = 1; i <= steps; i++) {
        const t = i / steps;
        const cx = startX + (endX - startX) * t;
        const cy = startY + (endY - startY) * t;
        utils.sendMouseEvent('mousemove', cx, cy, 0, 0, 0);
      }
      utils.sendMouseEvent('mouseup', endX, endY, 0, 1, 0);
    }

    // Phase 2: HTML5 DragEvent sequence for apps that use drag-and-drop API
    const sourceEl = doc.elementFromPoint(startX, startY);
    const targetEl = doc.elementFromPoint(endX, endY);
    if (sourceEl) {
      let dataTransfer = null;
      try {
        dataTransfer = new win.DataTransfer();
      } catch (e) {
        // DataTransfer constructor may not be available in all Gecko contexts
      }
      const mkEvent = (type, x, y, target) => {
        const opts = {
          bubbles: true,
          cancelable: true,
          clientX: x,
          clientY: y,
          dataTransfer,
        };
        return new win.DragEvent(type, opts);
      };
      sourceEl.dispatchEvent(mkEvent('dragstart', startX, startY, sourceEl));
      sourceEl.dispatchEvent(mkEvent('drag', startX, startY, sourceEl));
      if (targetEl) {
        targetEl.dispatchEvent(mkEvent('dragenter', endX, endY, targetEl));
        targetEl.dispatchEvent(mkEvent('dragover', endX, endY, targetEl));
        targetEl.dispatchEvent(mkEvent('drop', endX, endY, targetEl));
      }
      sourceEl.dispatchEvent(mkEvent('dragend', endX, endY, sourceEl));
    }

    return {
      success: true,
      from: { x: Math.round(startX), y: Math.round(startY) },
      to: { x: Math.round(endX), y: Math.round(endY) },
      steps,
      source_tag: sourceEl?.tagName?.toLowerCase() || 'unknown',
      target_tag: targetEl?.tagName?.toLowerCase() || 'unknown',
    };
  }

  #dragElement(data) {
    const sourceEl = this.#getElement(data.sourceIndex);
    const targetEl = this.#getElement(data.targetIndex);
    sourceEl.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const srcRect = sourceEl.getBoundingClientRect();
    const tgtRect = targetEl.getBoundingClientRect();
    const srcX = srcRect.x + srcRect.width / 2;
    const srcY = srcRect.y + srcRect.height / 2;
    const tgtX = tgtRect.x + tgtRect.width / 2;
    const tgtY = tgtRect.y + tgtRect.height / 2;
    this.#showCursor(srcX, srcY);
    return this.#performDrag(srcX, srcY, tgtX, tgtY, data.steps || 10);
  }

  #dragCoordinates(data) {
    this.#showCursor(data.startX, data.startY);
    return this.#performDrag(data.startX, data.startY, data.endX, data.endY, data.steps || 10);
  }

  // --- File Upload ---

  #fileUpload(index, base64, filename, mimeType) {
    const win = this.contentWindow;
    const el = this.#getElement(index);
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' || el.type !== 'file') {
      throw new Error('Element [' + index + '] is <' + tag + (el.type ? ' type=' + el.type : '') + '>, not <input type="file">');
    }

    // Decode base64 to binary
    const binaryStr = win.atob(base64);
    const bytes = new Uint8Array(binaryStr.length);
    for (let i = 0; i < binaryStr.length; i++) {
      bytes[i] = binaryStr.charCodeAt(i);
    }

    // Create File via content window constructors
    const blob = new win.Blob([bytes], { type: mimeType });
    const file = new win.File([blob], filename, { type: mimeType });

    // Use DataTransfer to set on the input (waive Xray to allow .files assignment)
    const dt = new win.DataTransfer();
    dt.items.add(file);
    const unwrapped = Cu.waiveXrays(el);
    unwrapped.files = dt.files;

    // Dispatch change event
    el.dispatchEvent(new win.Event('change', { bubbles: true }));
    el.dispatchEvent(new win.Event('input', { bubbles: true }));

    const setFile = el.files?.[0];
    return {
      success: true,
      file_name: setFile?.name || filename,
      file_size: setFile?.size || bytes.length,
      file_type: setFile?.type || mimeType,
    };
  }

  // --- Console Capture ---

  #formatArg(value) {
    if (value === null) return 'null';
    if (value === undefined) return 'undefined';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (value instanceof this.contentWindow.Error) {
      return value.message + (value.stack ? '\n' + value.stack : '');
    }
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }

  #setupConsoleCapture() {
    if (this.#captureSetup) return { success: true, note: 'already setup' };
    const win = this.contentWindow;
    if (!win) throw new Error('No content window');

    const self = this;
    // Access unwrapped console to get originals and set content-visible wrappers.
    // Xray wrappers prevent chrome-scope assignments from being visible to content
    // code, so we must use wrappedJSObject + Cu.exportFunction.
    const unwrapped = win.console.wrappedJSObject;
    const origLog = unwrapped.log.bind(unwrapped);
    const origWarn = unwrapped.warn.bind(unwrapped);
    const origError = unwrapped.error.bind(unwrapped);
    const origInfo = unwrapped.info.bind(unwrapped);

    const makeWrapper = (level, origFn, isError) => {
      return Cu.exportFunction(function(...args) {
        const message = Array.from(args).map(a => self.#formatArg(a)).join(' ');
        self.#consoleLogs.push({ level, message, timestamp: new Date().toISOString() });
        if (self.#consoleLogs.length > 500) self.#consoleLogs.shift();
        if (isError) {
          self.#consoleErrors.push({ type: 'console.error', message, timestamp: new Date().toISOString() });
          if (self.#consoleErrors.length > 100) self.#consoleErrors.shift();
        }
        origFn(...args);
      }, win);
    };

    unwrapped.log = makeWrapper('log', origLog, false);
    unwrapped.warn = makeWrapper('warn', origWarn, false);
    unwrapped.error = makeWrapper('error', origError, true);
    unwrapped.info = makeWrapper('info', origInfo, false);

    // Capture uncaught errors
    win.addEventListener('error', (event) => {
      self.#consoleErrors.push({
        type: 'uncaught_error',
        message: event.message || '',
        filename: event.filename || '',
        lineno: event.lineno || 0,
        colno: event.colno || 0,
        stack: event.error?.stack || '',
        timestamp: new Date().toISOString(),
      });
      if (self.#consoleErrors.length > 100) self.#consoleErrors.shift();
    });

    // Capture unhandled promise rejections
    win.addEventListener('unhandledrejection', (event) => {
      const reason = event.reason;
      self.#consoleErrors.push({
        type: 'unhandled_rejection',
        message: reason?.message || String(reason),
        stack: reason?.stack || '',
        timestamp: new Date().toISOString(),
      });
      if (self.#consoleErrors.length > 100) self.#consoleErrors.shift();
    });

    this.#captureSetup = true;
    return { success: true };
  }

  // --- Virtual Cursor ---

  #showCursor(x, y) {
    const doc = this.contentWindow?.document;
    if (!doc) return;
    // Validate inputs are finite numbers
    const numX = Number(x);
    const numY = Number(y);
    if (!Number.isFinite(numX) || !Number.isFinite(numY)) return;
    // Remove previous cursor
    this.#removeCursor();
    // Create cursor overlay: red crosshair with ring
    const cursor = doc.createElement('div');
    cursor.id = '__zenleap_cursor';
    // Use individual style properties (not cssText concatenation) to prevent CSS injection
    cursor.style.position = 'fixed';
    cursor.style.zIndex = '2147483647';
    cursor.style.pointerEvents = 'none';
    cursor.style.left = (numX - 12) + 'px';
    cursor.style.top = (numY - 12) + 'px';
    cursor.style.width = '24px';
    cursor.style.height = '24px';
    cursor.style.border = '3px solid red';
    cursor.style.borderRadius = '50%';
    cursor.style.background = 'rgba(255,0,0,0.2)';
    cursor.style.boxShadow = '0 0 8px rgba(255,0,0,0.6)';
    // Crosshair lines
    const hLine = doc.createElement('div');
    hLine.style.cssText = 'position:absolute;top:50%;left:-4px;right:-4px;height:1px;background:red;transform:translateY(-50%)';
    const vLine = doc.createElement('div');
    vLine.style.cssText = 'position:absolute;left:50%;top:-4px;bottom:-4px;width:1px;background:red;transform:translateX(-50%)';
    cursor.appendChild(hLine);
    cursor.appendChild(vLine);
    doc.documentElement.appendChild(cursor);
    this.#cursorOverlay = cursor;
    // Auto-remove after 60 seconds (or when cursor moves)
    this.contentWindow.setTimeout(() => this.#removeCursor(), 60000);
  }

  #removeCursor() {
    if (this.#cursorOverlay && this.#cursorOverlay.parentNode) {
      this.#cursorOverlay.parentNode.removeChild(this.#cursorOverlay);
    }
    this.#cursorOverlay = null;
  }

  // --- Element/Text Query ---

  #querySelector(selector) {
    const doc = this.contentWindow?.document;
    if (!doc) return { found: false };
    const el = doc.querySelector(selector);
    if (!el) return { found: false };
    return {
      found: true,
      tag: el.tagName.toLowerCase(),
      text: this.#getVisibleText(el).substring(0, 100),
    };
  }

  #searchText(text) {
    const doc = this.contentWindow?.document;
    if (!doc?.body) return { found: false };
    const bodyText = doc.body.innerText || '';
    return { found: bodyText.includes(text) };
  }

  // --- Cookies ---

  #setCookie(cookieStr) {
    const doc = this.contentWindow?.document;
    if (!doc) throw new Error('No document');
    doc.cookie = cookieStr;
    return { success: true, cookie: doc.cookie.substring(0, 200) };
  }

  #getContentCookies() {
    const doc = this.contentWindow?.document;
    if (!doc) return { cookies: '' };
    return { cookies: doc.cookie };
  }

  // --- Storage ---

  #getStorageObject(type) {
    const win = this.contentWindow;
    if (!win) throw new Error('No content window');
    if (type === 'localStorage') return win.localStorage;
    if (type === 'sessionStorage') return win.sessionStorage;
    throw new Error('Invalid storage_type: ' + type + ' (use localStorage or sessionStorage)');
  }

  #getStorage(type, key) {
    const storage = this.#getStorageObject(type);
    if (key) {
      return { value: storage.getItem(key) };
    }
    // Dump all key-value pairs
    const entries = {};
    for (let i = 0; i < storage.length; i++) {
      const k = storage.key(i);
      entries[k] = storage.getItem(k);
    }
    return { entries, count: storage.length };
  }

  #setStorage(type, key, value) {
    const storage = this.#getStorageObject(type);
    storage.setItem(key, value);
    return { success: true, key, length: storage.length };
  }

  #deleteStorage(type, key) {
    const storage = this.#getStorageObject(type);
    if (key) {
      storage.removeItem(key);
      return { success: true, key, length: storage.length };
    }
    const count = storage.length;
    storage.clear();
    return { success: true, cleared: count, length: 0 };
  }

  // --- Accessibility Tree ---

  #getAccessibilityTree() {
    const win = this.contentWindow;
    if (!win?.document?.body) return { nodes: [], error: null };

    try {
      // nsIAccessibilityService — may not be available if a11y is disabled.
      // Try nsIAccessibilityService first (newer Gecko), fallback to nsIAccessibleRetrieval.
      let accService = null;
      try {
        accService = Cc['@mozilla.org/accessibilityService;1']
          ?.getService(Ci.nsIAccessibilityService);
      } catch (e1) {
        try {
          accService = Cc['@mozilla.org/accessibilityService;1']
            ?.getService(Ci.nsIAccessibleRetrieval);
        } catch (e2) {
          return { nodes: [], error: 'Accessibility service unavailable: ' + e2.message };
        }
      }
      if (!accService) {
        return { nodes: [], error: 'Accessibility service not available' };
      }

      const accDoc = accService.getAccessibleFor(win.document);
      if (!accDoc) {
        return { nodes: [], error: 'No accessible document' };
      }

      const nodes = [];
      const MAX_NODES = 500;

      // Role constant → human-readable name mapping
      const ROLE_NAMES = {
        4: 'menubar', 5: 'scrollbar', 7: 'alert', 8: 'column',
        9: 'cursor', 10: 'dialog', 12: 'document', 13: 'grouping',
        14: 'image', 16: 'list', 17: 'listitem', 20: 'outline',
        21: 'outlineitem', 24: 'graphic', 25: 'pushbutton',
        26: 'checkbutton', 27: 'radiobutton', 28: 'combobox',
        30: 'progressbar', 33: 'row', 34: 'cell', 38: 'link',
        40: 'text_leaf', 42: 'entry', 43: 'caption', 44: 'heading',
        46: 'section', 48: 'footer', 49: 'paragraph', 50: 'header',
        55: 'internal_frame', 57: 'table', 58: 'tree', 59: 'tree_item',
        64: 'option', 66: 'listbox', 68: 'text_container',
        69: 'buttondropdowngrid', 70: 'whitespace', 71: 'pagetab',
        75: 'form', 76: 'label', 79: 'statusbar', 80: 'toolbar',
        82: 'application', 83: 'toggle_button', 95: 'flat_equation',
        100: 'grid', 106: 'switch', 108: 'figure',
        117: 'navigation', 118: 'complementary', 119: 'landmark',
        120: 'content_info', 121: 'banner', 124: 'main',
        125: 'article', 126: 'region', 127: 'note', 134: 'search',
      };

      const walkAcc = (acc, depth = 0) => {
        if (nodes.length >= MAX_NODES) return;
        if (depth > 30) return;

        const role = acc.role;
        const roleName = ROLE_NAMES[role] || 'role_' + role;
        const name = acc.name || '';
        const value = acc.value || '';

        // Skip invisible/empty nodes to reduce noise
        if (roleName === 'whitespace' || roleName === 'text_leaf') {
          // Still walk children, but only include text_leaf with content
          if (roleName === 'text_leaf' && name.trim()) {
            nodes.push({ role: roleName, name: name.substring(0, 100), depth });
          }
        } else {
          nodes.push({
            role: roleName,
            name: name.substring(0, 100),
            value: value.substring(0, 50) || undefined,
            depth,
          });
        }

        // Walk children
        const count = acc.childCount;
        for (let i = 0; i < count && nodes.length < MAX_NODES; i++) {
          try {
            const child = acc.getChildAt(i);
            if (child) walkAcc(child, depth + 1);
          } catch (e) {
            // Child may be invalid
          }
        }
      };

      walkAcc(accDoc, 0);
      return { nodes, total: nodes.length };
    } catch (e) {
      // Graceful fallback if a11y service is unavailable
      return { nodes: [], error: 'Accessibility tree extraction failed: ' + e.message };
    }
  }

  // --- JS Evaluation ---

  #evalInContent(expression) {
    const win = this.contentWindow;
    if (!win) throw new Error('No content window');
    try {
      const result = win.eval(expression);
      return { result: this.#formatArg(result) };
    } catch (e) {
      return { error: e.message, stack: e.stack || '' };
    }
  }
}
