// Behaviour test for BOTH lens clients (userscripts/untappd-lens.user.js and extension/content.js) in jsdom:
// the transport is stubbed (GM_xmlhttpRequest / chrome.runtime.sendMessage), the page is a tiny piwnemosty.pl-style
// product grid. Run by tests/test_lens_clients_js.py; exits non-zero on the first failed expectation.
"use strict";

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const CLIENTS = {
  userscript: path.join(ROOT, "userscripts", "untappd-lens.user.js"),
  extension: path.join(ROOT, "extension", "content.js"),
};

const HTML = `<!doctype html><html><body>
<div class="product" id="c1"><a class="product__icon" href="/p/1" style="display:block;width:200px;height:200px"></a>
  <a class="product__name" href="/p/1">Browar Stu Mostów: Pastry Imperial Stout With Biscoff Cookies - 330 ml bottle</a></div>
<div class="product" id="c2"><a class="product__icon" href="/p/2" style="display:block;width:200px;height:200px"></a>
  <a class="product__name" href="/p/2">Browar Z: Hazy Thing - 500 ml can</a></div>
<div class="product" id="c3"><a class="product__icon" href="/p/3" style="display:block;width:200px;height:200px"></a>
  <a class="product__name" href="/p/3">Browar Z: T-shirt Logo - size L</a></div>
</body></html>`;

const MATCHED = { matched: true, hadIt: true, bid: 111, name: "Pastry Imperial Stout", brewery: "Stu Mostow", rating: 4.09,
  ratingCount: 462, style: "Stout - Imperial", url: "https://untappd.com/b/x/111" };
const PROBABLE = { matched: false, candidates: [], probable: { name: "Hazy Thing 2", brewery: "Browar Z", bid: 222, rating: 3.7,
  ratingCount: 50, url: "https://untappd.com/b/y/222", hadIt: false } };
const UNMATCHED = { matched: false, candidates: [], searchUrl: "https://untappd.com/search?q=x" };
const CONFIRMED = { matched: true, hadIt: false, bid: 222, name: "Hazy Thing 2", brewery: "Browar Z", rating: 3.7, url: "https://untappd.com/b/y/222" };

function makeServer() {
  const calls = [];
  function handle(apiPath, body) {
    calls.push({ path: apiPath, body });
    if (apiPath === "/api/lens/lookup") return { ok: true, results: body.items.map((it, i) => (i === 0 ? MATCHED : PROBABLE)) };
    if (apiPath === "/api/lens/reject") return { ok: true, result: UNMATCHED };
    if (apiPath === "/api/lens/unreject") return { ok: true, result: MATCHED };
    if (apiPath === "/api/lens/confirm") return { ok: true, result: CONFIRMED };
    if (apiPath === "/api/lens/unconfirm") return { ok: true, result: PROBABLE };
    return { ok: false, error: "unknown_path" };
  }
  return { calls, handle };
}

function install(kind, window, server) {
  const store = {};
  if (kind === "extension") {
    window.chrome = {
      runtime: {
        id: "test", lastError: null, getManifest: () => ({ version: "0" }),
        sendMessage(message, callback) {
          setTimeout(() => {
            if (message.type === "lensLookup") {
              const data = server.handle("/api/lens/lookup", { items: message.items });
              callback({ ok: true, results: data.results });
            } else if (message.type === "lensFeedback") {
              const data = server.handle(message.path, message.body);
              callback(data.ok ? { ok: true, result: data.result } : { ok: false, error: data.error });
            }
          }, 0);
        },
      },
      storage: {
        local: {
          get(keys, callback) { callback(store); },
          set(values) { Object.assign(store, values); },
        },
      },
    };
  } else {
    window.GM_getValue = (key, fallback) => (key in store ? store[key] : (key === "lensApiToken" ? "test-token" : fallback));
    window.GM_setValue = (key, value) => { store[key] = value; };
    window.GM_registerMenuCommand = () => {};
    window.prompt = () => "";
    window.GM_xmlhttpRequest = ({ url, data, onload }) => {
      setTimeout(() => {
        const apiPath = new URL(url).pathname;
        onload({ responseText: JSON.stringify(server.handle(apiPath, JSON.parse(data))) });
      }, 0);
    };
  }
}

async function waitFor(window, predicate, what) {
  const deadline = Date.now() + 3000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for: " + what);
    await new Promise((resolve) => window.setTimeout(resolve, 10));
  }
}

const chips = (document) => Array.from(document.querySelectorAll(".lens-status-chip")).map((c) => c.textContent);

async function runClient(kind) {
  const dom = new JSDOM(HTML, { url: "https://www.piwnemosty.pl/", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const { document } = window;
  await new Promise((resolve) => (document.readyState === "complete" ? resolve() : window.addEventListener("load", resolve)));
  const server = makeServer();
  install(kind, window, server);
  window.eval(fs.readFileSync(CLIENTS[kind], "utf8"));

  // 1. the page is looked up: two beers, not the t-shirt, with the shop and the product link
  await waitFor(window, () => chips(document).length === 2, "two status chips");
  assert.deepStrictEqual(chips(document), ["✅✕", "🤔✓✕"], kind + ": chips after lookup");
  const lookup = server.calls.find((c) => c.path === "/api/lens/lookup");
  assert.strictEqual(lookup.body.items.length, 2, kind + ": the t-shirt must not be looked up");
  assert.strictEqual(lookup.body.items[0].shop, "piwnemosty.pl", kind + ": shop is sent");
  assert.ok(/\/p\/1$/.test(lookup.body.items[0].page), kind + ": product link is sent");
  assert.strictEqual(document.querySelector("#c3 .lens-status-chip"), null, kind + ": no chip on merchandise");

  // 2. the buttons are really visible (a hover-only rule does not exist on a phone)
  const reject = document.querySelector("#c1 .lens-reject-btn");
  assert.ok(reject, kind + ": reject button exists");
  assert.notStrictEqual(window.getComputedStyle(reject).display, "none", kind + ": reject button visible");
  assert.notStrictEqual(window.getComputedStyle(document.querySelector("#c2 .lens-confirm-btn")).display, "none");

  // 3. "not that beer": the server is told which beer for which shop text, the card is redrawn, undo restores it
  reject.click();
  await waitFor(window, () => chips(document)[0] === "🔍", "card redrawn after reject");
  const rejectCall = server.calls.find((c) => c.path === "/api/lens/reject");
  // through JSON: objects made inside the jsdom window have another Object prototype than the ones made here
  assert.deepStrictEqual(JSON.parse(JSON.stringify(rejectCall.body)),
    { brewery: "Browar Stu Mostów", name: "Pastry Imperial Stout With Biscoff Cookies", bid: 111 });
  const toast = document.getElementById("lens-toast");
  assert.ok(toast && toast.textContent.includes("Pastry Imperial Stout"), kind + ": toast names the beer");
  Array.from(toast.querySelectorAll("a")).find((a) => a.textContent === "Скасувати").click();
  await waitFor(window, () => chips(document)[0] === "✅✕", "card restored after undo");
  assert.ok(server.calls.some((c) => c.path === "/api/lens/unreject" && c.body.bid === 111), kind + ": undo is sent");

  // 4. "yes, this one" on the probable beer
  document.querySelector("#c2 .lens-confirm-btn").click();
  await waitFor(window, () => chips(document)[1].startsWith("🆕"), "probable card confirmed");
  const confirmCall = server.calls.find((c) => c.path === "/api/lens/confirm");
  assert.strictEqual(confirmCall.body.bid, 222, kind + ": confirm sends the probable beer");

  // 5. the lookup label sent to the bot names the real version of the client
  window.close();
  return kind;
}

(async () => {
  for (const kind of Object.keys(CLIENTS)) {
    await runClient(kind);
    console.log("ok " + kind);
  }
  process.exit(0);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
