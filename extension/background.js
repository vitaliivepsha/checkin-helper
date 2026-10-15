// Service worker - does the actual cross-origin request to the bot's API.
// Content scripts can't reliably fetch() cross-origin under Manifest V3
// without going through here even with host_permissions declared, so the
// content script sends a message and this does the network call on its
// behalf (the extension-level fetch isn't subject to the page's CORS).

// ---- Fill these in before use ------------------------------------------
// API_BASE: PUBLIC_BASE_URL from the bot's .env (wherever webapp_server.py
// is reachable) - update this AND manifest.json's host_permissions if that
// ever changes (e.g. the VM's IP changes and the sslip.io hostname with it).
const API_BASE = "https://35-211-15-162.sslip.io";
// The LENS_API_TOKEN value from the bot's .env is NOT stored in this file (it is committed to git):
// open the extension's options page (chrome://extensions -> Details -> Extension options) and paste
// it there once; it is kept in chrome.storage.local. Treat it like a password - anyone with it can
// read your Untappd history through this endpoint.
function getApiToken() {
  return new Promise((resolve) => chrome.storage.local.get("lensApiToken", (v) => resolve((v && v.lensApiToken) || "")));
}
// --------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "lensLookup") return false;

  // One retry after a few seconds: the bot restarts on every deploy and the proxy in front
  // of it answers an empty body meanwhile ("Unexpected end of JSON input").
  async function lookupOnce() {
    const token = await getApiToken();
    if (!token) throw new Error("no_token: open the extension options and paste LENS_API_TOKEN");
    const res = await fetch(API_BASE + "/api/lens/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Lens-Token": token },
      body: JSON.stringify({ items: message.items }),
    });
    return res.json();
  }

  (async () => {
    try {
      let data;
      try {
        data = await lookupOnce();
      } catch (e) {
        if (String(e.message).startsWith("no_token")) throw e;
        await new Promise((resolve) => setTimeout(resolve, 4000));
        data = await lookupOnce();
      }
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
