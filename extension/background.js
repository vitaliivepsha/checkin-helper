// Service worker - does the actual cross-origin request to the bot's API.
// Content scripts can't reliably fetch() cross-origin under Manifest V3
// without going through here even with host_permissions declared, so the
// content script sends a message and this does the network call on its
// behalf (the extension-level fetch isn't subject to the page's CORS).

// ---- Fill these in before use ------------------------------------------
// API_BASE: PUBLIC_BASE_URL from the bot's .env (the ngrok URL, or wherever
// webapp_server.py is reachable). Free-tier ngrok URLs change on every
// restart of the bot - update this AND manifest.json's host_permissions
// whenever that happens.
const API_BASE = "https://bronchial-inger-puddly.ngrok-free.dev";
// API_TOKEN: the LENS_API_TOKEN value from the bot's .env. Treat this like
// a password - anyone with it can read your Untappd history through this
// endpoint.
const API_TOKEN = "e9jqZfhZlKqRgeJ4qeK6-XX62uyTkheSwflb7zKV20I";
// --------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "lensLookup") return false;

  (async () => {
    try {
      const res = await fetch(API_BASE + "/api/lens/lookup", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Lens-Token": API_TOKEN },
        body: JSON.stringify({ items: message.items }),
      });
      const data = await res.json();
      if (!data.ok) {
        sendResponse({ ok: false, error: data.error || "unknown_error" });
        return;
      }
      sendResponse({ ok: true, results: data.results });
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();

  return true; // keep the message channel open for the async sendResponse above
});
