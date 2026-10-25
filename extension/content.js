// Runs inside the MadLions driver page. Asks the helper which profile is wanted
// and clicks that profile in the driver's left sidebar.
(() => {
  'use strict';
  if (document.documentElement.dataset.mad60Auto) return; // already running in this page
  document.documentElement.dataset.mad60Auto = '1';

  const FIND_TIMEOUT_MS = 15000;
  const RETRY_MS = 5000;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();

  // every element under root, including inside open shadow roots
  function* allElements(root) {
    for (const el of root.children || []) {
      yield el;
      if (el.shadowRoot) yield* allElements(el.shadowRoot);
      yield* allElements(el);
    }
  }

  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'INPUT', 'TEXTAREA', 'SELECT', 'OPTION', 'TITLE', 'META', 'LINK', 'HEAD']);

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, w: r.width, h: r.height };
  }

  // Element whose whole text is exactly `text`, innermost one, leftmost on screen (= the sidebar).
  function findByText(text, root = document) {
    const want = norm(text);
    if (!want) return null;
    const hits = [];
    for (const el of allElements(root)) {
      if (SKIP.has(el.tagName)) continue;
      if (norm(el.textContent) === want) hits.push(el);
    }
    const inner = hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
    const visible = inner.filter((el) => { const r = rectOf(el); return r.w > 0 && r.h > 0; });
    const pool = visible.length ? visible : inner; // hidden/minimised window: fall back to DOM order
    if (!pool.length) return null;
    pool.sort((a, b) => (rectOf(a).left - rectOf(b).left) || (rectOf(a).top - rectOf(b).top));
    return { el: pool[0], visible: visible.length > 0 };
  }

  function click(el) {
    const r = rectOf(el);
    const init = {
      bubbles: true, cancelable: true, composed: true, view: window, button: 0,
      clientX: r.left + r.w / 2, clientY: r.top + r.h / 2,
    };
    const Ptr = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
    el.dispatchEvent(new Ptr('pointerdown', { ...init, buttons: 1, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    el.dispatchEvent(new MouseEvent('mousedown', { ...init, buttons: 1 }));
    el.dispatchEvent(new Ptr('pointerup', { ...init, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.click();
  }

  function toast(text) {
    try {
      const d = document.createElement('div');
      d.textContent = text;
      d.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483647;' +
        'background:#1f1f1f;color:#fff;border:1px solid #e8452c;padding:8px 14px;border-radius:6px;' +
        'font:13px system-ui,sans-serif;opacity:.95;pointer-events:none';
      document.body.appendChild(d);
      setTimeout(() => d.remove(), 3500);
    } catch (_) { /* cosmetic only */ }
  }

  async function applyProfile(name, applyButtonText) {
    const deadline = Date.now() + FIND_TIMEOUT_MS;
    let found = null;
    while (Date.now() < deadline) {
      found = findByText(name);
      if (found) break;
      await sleep(500);
    }
    if (!found) return { ok: false, detail: `no "${name}" entry found in the driver page (is the profile imported and named exactly that?)` };

    click(found.el);
    toast(`MAD60 auto-profile: ${name}`);
    let detail = found.visible ? 'clicked' : 'clicked (window was hidden, used DOM order)';

    if (applyButtonText) {
      await sleep(500);
      const btn = findByText(applyButtonText);
      if (btn) { click(btn.el); detail += `, then clicked "${applyButtonText}"`; }
      else detail += `, but button "${applyButtonText}" was not found`;
    }
    return { ok: true, detail };
  }

  const ask = (msg) => new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage(msg, (resp) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(resp);
      });
    } catch (e) { reject(e); }
  });

  async function loop() {
    let rev = -1;
    for (;;) {
      let msg;
      try {
        msg = await ask({ type: 'next', since: rev });
      } catch (e) {
        if (String(e).includes('invalidated')) return; // extension was reloaded; page needs a refresh
        await sleep(RETRY_MS);
        continue;
      }
      if (!msg || msg.error) { await sleep(RETRY_MS); continue; } // helper not running yet
      rev = msg.rev;
      if (msg.pending && msg.profile) {
        const result = await applyProfile(msg.profile, msg.applyButtonText);
        try { await ask({ type: 'report', payload: { rev, profile: msg.profile, ...result } }); } catch (_) { /* helper gone */ }
      }
    }
  }

  // test hook (Node only; harmless in the browser)
  if (typeof module !== 'undefined' && module.exports) module.exports = { findByText, norm };
  else loop();
})();
