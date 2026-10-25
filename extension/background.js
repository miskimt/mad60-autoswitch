// Relays between the driver page and the local helper (kb_autoswitch.py).
// The helper's port must match "port" in config.json (default 47831).
const HELPER = 'http://127.0.0.1:47831';

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === 'next') {
        // long-poll: the helper holds this open (~25s) until the wanted profile changes
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 35000);
        const res = await fetch(`${HELPER}/next?since=${encodeURIComponent(msg.since)}`, {
          signal: ctrl.signal,
          cache: 'no-store',
        });
        clearTimeout(timer);
        sendResponse(await res.json());
      } else if (msg.type === 'report') {
        await fetch(`${HELPER}/report`, {
          method: 'POST',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify(msg.payload),
        });
        sendResponse({ ok: true });
      } else {
        sendResponse({ error: 'unknown message' });
      }
    } catch (e) {
      sendResponse({ error: String(e) });
    }
  })();
  return true; // keep the channel open for the async response
});
