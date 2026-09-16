// ==UserScript==
// @name         Untappd Had-It Overlay
// @namespace    checkin-helper
// @version      2.5.0
// @description  Marks beers you've already had (plus rating + Untappd link) directly on a shop's product listing, using your own check-in history via the checkin-helper bot.
// @match        https://www.piwnemosty.pl/*
// @match        https://*.ontap.pl/*
// @match        https://hoptimaal.com/*
// @match        https://www.hoptimaal.com/*
// @match        https://hopincraftbier.be/*
// @match        https://www.hopincraftbier.be/*
// @match        https://onemorebeer.pl/*
// @match        https://www.onemorebeer.pl/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      bronchial-inger-puddly.ngrok-free.dev
// ==/UserScript==

/* eslint-disable no-undef */
(function () {
  "use strict";

  // ---- Fill these in before use ----------------------------------------
  // API_BASE: PUBLIC_BASE_URL from the bot's .env (the ngrok URL, or
  // wherever webapp_server.py is reachable). Free-tier ngrok URLs change on
  // every restart of the bot - update this line whenever that happens.
  const API_BASE = "https://bronchial-inger-puddly.ngrok-free.dev";
  // API_TOKEN: the LENS_API_TOKEN value from the bot's .env. Treat this
  // like a password - anyone with it can read your Untappd history through
  // this endpoint.
  const API_TOKEN = "e9jqZfhZlKqRgeJ4qeK6-XX62uyTkheSwflb7zKV20I";
  // ------------------------------------------------------------------------

  const CACHE_TTL_MS = 6 * 60 * 60 * 1000; // 6h - had_it_index changes slowly; avoids re-querying every page view

  // ---- Site adapters - add one entry per shop --------------------------
  // Each adapter only needs to know how to find product cards on ITS OWN
  // site and pull {brewery, name} out of one (extract) plus where to
  // anchor the overlay (imageWrapper); all matching/rating/had-it logic
  // lives entirely on the bot side (beer_match.resolve_beer), so a new
  // site is just a new adapter, never new matching logic here. Match by
  // "hosts" (exact hostnames) or "hostSuffix" (that domain and any of its
  // subdomains, e.g. ontap.pl's per-venue subdomains).
  const ADAPTERS = [
    {
      hosts: ["www.piwnemosty.pl"],
      cardSelector: "div.product",
      imageWrapper(card) {
        // position:relative in this theme - overlay badges are absolutely
        // positioned inside it, on top of the can/bottle photo itself.
        return card.querySelector("a.product__icon") || card;
      },
      // Bottom-left (the list chip's own default) sat too far from the
      // status chip and got lost against a pale bottle label - stack it
      // in the SAME top-left corner as status instead, confirmed live via
      // injected test chips: the status chip's own box is ~23px tall, so
      // 40px clears it with a clean ~9px gap (no site badge to dodge here,
      // unlike ontap.pl's 48px which also had to clear a theme text label).
      listCorner: "top-left",
      listStackOffset: 40,
      // "Brewery: Beer Name - 500 ml can" -> {brewery: "Brewery", name: "Beer Name"}
      extract(card) {
        const nameEl = card.querySelector("a.product__name");
        if (!nameEl) return null;
        let text = (nameEl.textContent || "").trim();
        text = text.replace(/\b\d+\s*x\s*[\d.,]+\s*m?l\b\s*(?:can|bottle|keg)?/i, " "); // multipack marker, e.g. "4 x 500 ml can"
        text = text.replace(/\s*-\s*[\d.,]+\s*m?l\b.*$/i, ""); // trailing packaging info, can be several words, e.g. "- 30 l KEG A"
        text = text.replace(/\s+/g, " ").trim();
        const sep = text.indexOf(":");
        const result = sep === -1
          ? { brewery: "", name: text }
          : { brewery: text.slice(0, sep).trim(), name: text.slice(sep + 1).trim() };
        return result.name ? result : null;
      },
      // A mixed/gift assortment (several DIFFERENT beers under one SKU) -
      // there's no single beer name to look up here, so the card is
      // skipped entirely rather than searched and shown as a confusing
      // "not found"/wrong match.
      isBundle(card) {
        const nameEl = card.querySelector("a.product__name");
        return /\b(fan box|zestaw|gift box|mixed pack|box of \d+|\bset\b)\b/i.test((nameEl && nameEl.textContent) || "");
      },
    },
    {
      hostSuffix: "ontap.pl",
      cardSelector: "div.panel.panel-default",
      imageWrapper(card) {
        return card.querySelector(".panel-body.cml_semi") || card;
      },
      // This theme's own tap-number badge sits top-right and its "on tap
      // for Xd" badge sits bottom-right on every card - keep our chips on
      // the left side so nothing overlaps.
      altCorner: "bottom-left",
      // Both right corners are the theme's own badges (see altCorner's own
      // comment) and both left corners are already our status/alt chips -
      // genuinely no free 4th corner here, confirmed live. Stack the list
      // chip in the SAME top-left corner as status, offset further down
      // past both it and the theme's own "PINTA Brewery"-style text label
      // that sits just below it (confirmed live: 40px overlapped that
      // label by ~2px, 48px clears it).
      listCorner: "top-left",
      listStackOffset: 48,
      // The beer name has no element of its own - it's a bare text node
      // sandwiched between two <br> tags inside the same <span> as the
      // brewery name and the ABV line (confirmed live: <span><b
      // class="brewery">X</b><br>NAME <img flag><br>10,5°·4,1%</span>).
      extract(card) {
        const span = card.querySelector("h4.cml_shadow > span");
        if (!span) return null;
        const breweryEl = span.querySelector("b.brewery");
        const brewery = breweryEl ? breweryEl.textContent.replace(/\s+/g, " ").trim() : "";
        const children = Array.from(span.childNodes);
        const brIdx = children.reduce((acc, n, i) => (n.nodeName === "BR" ? acc.concat(i) : acc), []);
        if (!brIdx.length) return null;
        const start = brIdx[0] + 1;
        const end = brIdx.length > 1 ? brIdx[1] : children.length;
        let name = children.slice(start, end)
          .filter((n) => n.nodeType === Node.TEXT_NODE)
          .map((n) => n.textContent.trim())
          .join(" ")
          .replace(/\s+/g, " ")
          .trim();
        // A Plato-degree number sometimes leaks into this text node when a
        // card's markup omits the expected second <br> (proven live: "Atak
        // Chmielu 15,1°") - strip a trailing "NN,N°" the same way a
        // packaging suffix gets stripped on other sites.
        name = name.replace(/\s*[\d.,]+\s*°\s*$/, "").trim();
        return name ? { brewery, name } : null;
      },
    },
    {
      hosts: ["hoptimaal.com", "www.hoptimaal.com"],
      cardSelector: "div.product-item",
      imageWrapper(card) {
        return card.querySelector(".product-item__media") || card;
      },
      // This theme's own quick-add "+" button sits top-right on every
      // card (confirmed live) - keep the rating/candidates chip on the
      // opposite corner so nothing overlaps.
      altCorner: "bottom-right",
      // Product titles are "<Vendor> <BeerName>" with NO separator at all
      // (e.g. vendor "FrauGruber Brewing" + title "FrauGruber The
      // Pretender"), and the vendor's own words don't even always
      // literally prefix the title (vendor "Brasserie Caulier" but title
      // starts with just "Caulier") - splitting this from the text alone
      // would be a guess. Instead fetch the product's own Shopify JSON
      // for its authoritative "vendor" field and pass the (still vendor-
      // prefixed) title straight through as the name - proven live that a
      // duplicated-but-correct brewery word doesn't break the search, and
      // beer_match.py's own prefix-stripping fallback
      // (_brewery_prefix_stripped_variant) cleans it up server-side for
      // the cases where that duplication does cause ambiguity.
      async extract(card) {
        const link = card.querySelector("h3.product-item__product-title a");
        if (!link) return null;
        const name = (link.textContent || "").trim();
        if (!name) return null;
        const m = (link.getAttribute("href") || "").match(/\/products\/([^/?#]+)/);
        if (!m) return { brewery: "", name };
        try {
          const res = await fetch(`/products/${m[1]}.js`);
          if (!res.ok) return { brewery: "", name };
          const data = await res.json();
          // This collection page mixes actual beers with merchandise (a
          // glass, apparel, gift sets) - confirmed live: "Arpus Tumbler
          // Glas" (a plain drinking glass) got shown as a confident 4.25-
          // star "match" because its title happens to contain the
          // brewery's own name plus a real English word ("Glas"/"Glass").
          // Shopify's own product_type field reliably distinguishes it
          // ("Merch" vs. the packaging-format types real beers carry, e.g.
          // "Blik"/can) - skip rather than search at all, same as a bundle.
          if ((data.type || "").trim().toLowerCase() === "merch") return null;
          return { brewery: data.vendor || "", name };
        } catch (e) {
          return { brewery: "", name };
        }
      },
    },
    {
      hosts: ["hopincraftbier.be", "www.hopincraftbier.be"],
      cardSelector: "div.grid-product",
      imageWrapper(card) {
        return card.querySelector(".grid-product__image-wrap") || card;
      },
      // This theme's own "New" ribbon sits top-left, and its own style/
      // ABV/Untappd-rating info bar spans almost the full WIDTH of the
      // image's bottom edge (confirmed live) - top-right is the only
      // corner that's always clear, so both chips go on the right, status
      // above the rating bar's top edge.
      statusCorner: "top-right",
      altCorner: "bottom-right",
      // This shop already shows its own Untappd rating on every card
      // (confirmed live: it's kept up to date, unlike other sites where a
      // shown rating might be stale from whenever the listing was added)
      // - a second star-rating chip here would be pure duplication. Always
      // show a plain Untappd link chip instead, even when we DO have our
      // own rating (see renderOverlay's `!adapter.hideRating` check).
      hideRating: true,
      // Both right corners are already status/alt, and bottom-left is the
      // theme's own rating bar (see altCorner's own comment) - top-left is
      // the only corner left, intermittently shares it with the theme's
      // own "New" ribbon on new listings (accepted trade-off, matches
      // ontap.pl's own "no fully free corner" situation).
      listCorner: "top-left",
      // "Brewery - Beer Name" - split on the FIRST " - " only (some
      // beer names contain a second " - " of their own, e.g. "Fremont -
      // Barrel Aged Dark Star - Double Barrel (2025)", and some brewery/
      // beer names contain a bare hyphen with no surrounding spaces, e.g.
      // "Brasserie du Bas-Canada" - neither is affected since the split
      // looks for " - " specifically, not a bare "-").
      extract(card) {
        const titleEl = card.querySelector(".grid-product__title-inner");
        if (!titleEl) return null;
        const text = (titleEl.textContent || "").replace(/\s+/g, " ").trim();
        if (!text) return null;
        const sep = text.indexOf(" - ");
        const result = sep === -1
          ? { brewery: "", name: text }
          : { brewery: text.slice(0, sep).trim(), name: text.slice(sep + 3).trim() };
        return result.name ? result : null;
      },
      // Same mixed/gift assortment pattern as piwnemosty.pl - no single
      // beer to look up, so skip the card entirely.
      isBundle(card) {
        const titleEl = card.querySelector(".grid-product__title-inner");
        return /\b(fan box|gift box|mixed pack|box of \d+|\bset\b|assortment|bundle)\b/i.test(
          (titleEl && titleEl.textContent) || ""
        );
      },
    },
    {
      hosts: ["onemorebeer.pl", "www.onemorebeer.pl"],
      cardSelector: ".one-product-list-view__tile",
      imageWrapper(card) {
        return card.querySelector(".one-product-tile-gallery") || card;
      },
      // This theme's own "new"/promo icon column sits top-left (confirmed
      // live) - keep both chips on the right, same defensive split used on
      // other sites with a theme badge in that corner.
      statusCorner: "top-right",
      altCorner: "bottom-right",
      // The title is "<Brewery words><Beer Name> <packaging> <deposit>",
      // ALL in one string with no separator between brewery and name at
      // all (e.g. "ZA MIASTEM WRZUĆ NA LUZ BUT. 0,5 L") - same situation as
      // hoptimaal.com, so the (still brewery-duplicated) name is passed
      // through as-is; beer_match.py's own prefix-stripping fallback
      // handles the duplication server-side when needed. The packaging/
      // deposit suffix has to be stripped HERE though, since it's not a
      // brewery-name problem: confirmed live, "PINTA Pivečko 11,0°" (with
      // the trailing Plato-degree number) found nothing on Untappd, and
      // neither did the packaging words themselves left in.
      extract(card) {
        const titleEl = card.querySelector("h2.d-inline");
        if (!titleEl) return null;
        let text = (titleEl.textContent || "").replace(/\s+/g, " ").trim();
        if (!text) return null;
        text = text.replace(/\b(BUT\.?|BUTELKA|PUSZKA|KEG)\s*[\d.,]+\s*L\b/gi, " "); // "BUT./BUTELKA 0,5 L" / "PUSZKA 0,44 L" / "KEG 30 L" / "KEG 30L"
        text = text.replace(/\bKAUCJA\b/gi, " "); // bottle/can deposit note
        text = text.replace(/\b(?:B\.?)?ZW\b\.?/gi, " "); // returnable/non-returnable bottle marker: "ZW", "BZW", "BZW.", "BZW. BUT."
        text = text.replace(/\(\s*gazetka\s*\)/gi, " "); // "featured in this week's flyer" tag, e.g. "(gazetka)"
        text = text.replace(/\bdata\s+wa[żz]no[śs]ci\s+\d{1,2}[./]\d{1,2}[./]\d{2,4}\b/gi, " "); // "best before" date, e.g. "data ważności 15.10.2026"
        text = text.replace(/[\d.,]+\s*°/g, " "); // leaked Plato-degree number, e.g. "11,0°" - proven live to break search
        text = text.replace(/\s+/g, " ").trim();
        if (!text) return null;

        const producentRow = Array.from(
          card.querySelectorAll(".one-product-tile-information__row__title")
        ).find((el) => el.textContent.trim().startsWith("Producent"));
        const brewery = producentRow ? (producentRow.nextElementSibling?.textContent || "").trim() : "";

        return { brewery, name: text };
      },
      isBundle(card) {
        const titleEl = card.querySelector("h2.d-inline");
        return /\b(zestaw|fan box|gift box|mixed pack|box of \d+|\bset\b)\b/i.test(
          (titleEl && titleEl.textContent) || ""
        );
      },
    },
  ];
  // ------------------------------------------------------------------------

  function findAdapter() {
    const host = location.hostname;
    return ADAPTERS.find((a) =>
      (a.hosts && a.hosts.includes(host)) ||
      (a.hostSuffix && (host === a.hostSuffix || host.endsWith("." + a.hostSuffix)))
    );
  }

  const adapter = findAdapter();
  if (!adapter) return;

  function apiLookup(items) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: API_BASE + "/api/lens/lookup",
        headers: { "Content-Type": "application/json", "X-Lens-Token": API_TOKEN },
        data: JSON.stringify({ items }),
        onload(res) {
          let data;
          try {
            data = JSON.parse(res.responseText);
          } catch (e) {
            reject(new Error("bad_response: " + res.responseText.slice(0, 200)));
            return;
          }
          if (!data.ok) {
            reject(new Error(data.error || "unknown_error"));
            return;
          }
          resolve(data.results);
        },
        onerror() {
          reject(new Error("network_error"));
        },
      });
    });
  }

  function cacheKey(brewery, name) {
    return (brewery + "|" + name).toLowerCase();
  }

  function loadCache() {
    try {
      return JSON.parse(GM_getValue("lensCache", "{}"));
    } catch (e) {
      return {};
    }
  }

  function saveCache(cache) {
    GM_setValue("lensCache", JSON.stringify(cache));
  }

  const CHIP_STYLE =
    "position:absolute;z-index:20;display:flex;align-items:center;gap:3px;" +
    "font-size:12px;font-weight:600;line-height:1;padding:4px 7px;border-radius:999px;" +
    "background:#fff;color:#222;box-shadow:0 1px 4px rgba(0,0,0,0.35);" +
    "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;";

  // "top-left" -> "top:8px;left:8px;" - lets each adapter place chips
  // wherever its own theme has free space (see ontap.pl's altCorner).
  // `offset` (default 8) lets a SECOND chip stack in the same corner as an
  // existing one, further out along the same edge, for a theme (ontap.pl)
  // crowded enough that all four corners are otherwise already spoken for
  // - see the list-chip's own listCorner/listStackOffset comment below.
  function cornerStyle(corner, offset) {
    const [v, h] = (corner || "top-right").split("-");
    return `${v}:${offset || 8}px;${h}:8px;`;
  }

  // Two small corner chips overlaid directly on the product photo (not a
  // text line below it): status (icon + hover tooltip) defaults to top-
  // left, rating (clickable through to the beer's Untappd page) OR, when
  // the backend couldn't confidently pick one beer among lookalikes (see
  // beer_match.resolve_beer's "candidates" field), up to 3 numbered links
  // to each candidate instead, defaults to top-right - the two never both
  // apply to the same result. Either corner can be overridden per-adapter
  // (statusCorner/altCorner) to dodge a theme's own badges. The status
  // chip's border color doubles as a fast visual scan cue across a whole
  // page of cards (green = already had, blue = new to you, amber =
  // ambiguous).
  function renderOverlay(card, result) {
    const wrapper = adapter.imageWrapper(card);
    if (!wrapper || wrapper.querySelector(".lens-status-chip")) return; // don't double-render on re-runs
    const prevPosition = getComputedStyle(wrapper).position;
    if (prevPosition === "static") wrapper.style.position = "relative";

    const statusCorner = adapter.statusCorner || "top-left";
    const altCorner = adapter.altCorner || "top-right";

    let icon, tooltip, borderColor;
    if (result.matched) {
      if (result.hadIt === true) { icon = "✅"; tooltip = "Вже пив"; borderColor = "#2e7d32"; }
      else if (result.hadIt === false) { icon = "🆕"; tooltip = "Ще не пив"; borderColor = "#1565c0"; }
      else { icon = "❓"; tooltip = "Невідомо, чи пив"; borderColor = "#bbb"; }
    } else if (result.candidates && result.candidates.length) {
      icon = "🔍";
      tooltip = "Кілька можливих варіантів:\n" + result.candidates
        .map((c, i) => `${i + 1}. ${c.name}${c.brewery ? " (" + c.brewery + ")" : ""}`)
        .join("\n");
      borderColor = "#e6a800";
    } else {
      icon = "🔍"; tooltip = "Не знайдено на Untappd"; borderColor = "#bbb";
    }

    // Not matched -> the status chip itself becomes a link to Untappd's
    // own search page (see beer_match.build_search_url) - a fallback for
    // the user to search manually themselves, no extra visual element
    // needed since the chip is otherwise just informational in this case.
    const statusChip = document.createElement(!result.matched && result.searchUrl ? "a" : "span");
    statusChip.className = "lens-status-chip";
    statusChip.style.cssText = CHIP_STYLE + `${cornerStyle(statusCorner)}border:2px solid ${borderColor};text-decoration:none;`;
    statusChip.textContent = icon;
    if (!result.matched && result.searchUrl) {
      statusChip.href = result.searchUrl;
      statusChip.target = "_blank";
      statusChip.rel = "noopener";
      statusChip.title = tooltip + "\n(клік — пошук на Untappd)";
      statusChip.addEventListener("click", (e) => e.stopPropagation());
    } else {
      statusChip.title = tooltip;
    }
    wrapper.appendChild(statusChip);

    if (result.rating && !adapter.hideRating) {
      const ratingChip = document.createElement(result.url ? "a" : "span");
      ratingChip.className = "lens-rating-chip";
      ratingChip.title = "Відкрити на Untappd";
      ratingChip.style.cssText = CHIP_STYLE + `${cornerStyle(altCorner)}text-decoration:none;cursor:pointer;`;
      ratingChip.textContent = `⭐ ${result.rating.toFixed(2)}${result.ratingCount ? ` (${result.ratingCount})` : ""}`;
      if (result.url) {
        ratingChip.href = result.url;
        ratingChip.target = "_blank";
        ratingChip.rel = "noopener";
        // The image wrapper is often itself a link to the product page -
        // stop the click from bubbling into it so this chip reliably
        // opens Untappd instead.
        ratingChip.addEventListener("click", (e) => e.stopPropagation());
      }
      wrapper.appendChild(ratingChip);
    } else if (result.matched && result.url) {
      // Matched, but Untappd has no rating for it yet (a brand new or very
      // rare beer) - still worth a link through rather than leaving this
      // corner blank.
      const linkChip = document.createElement("a");
      linkChip.className = "lens-rating-chip";
      linkChip.title = "Відкрити на Untappd";
      // Untappd's own brand yellow - visually distinct from the plain
      // white rating/candidate chips, doubles as a "no rating yet" cue.
      linkChip.style.cssText =
        CHIP_STYLE + `${cornerStyle(altCorner)}text-decoration:none;cursor:pointer;background:#ffc300;color:#1a1300;`;
      linkChip.textContent = "Untappd";
      linkChip.href = result.url;
      linkChip.target = "_blank";
      linkChip.rel = "noopener";
      linkChip.addEventListener("click", (e) => e.stopPropagation());
      wrapper.appendChild(linkChip);
    } else if (!result.matched && result.candidates && result.candidates.length) {
      // Same corner as the rating chip (mutually exclusive with it) but
      // stacked VERTICALLY as separate circular buttons rather than
      // cramped inline numbers - each needs a real click target, not a
      // few pixels of text.
      const altChip = document.createElement("div");
      altChip.className = "lens-rating-chip";
      altChip.style.cssText = `position:absolute;${cornerStyle(altCorner)}z-index:20;display:flex;flex-direction:column;gap:5px;`;
      result.candidates.slice(0, 3).forEach((c, i) => {
        const link = document.createElement("a");
        link.href = c.url || "#";
        link.target = "_blank";
        link.rel = "noopener";
        link.title = c.name + (c.brewery ? ` (${c.brewery})` : "");
        link.textContent = String(i + 1);
        link.style.cssText =
          "display:flex;align-items:center;justify-content:center;width:26px;height:26px;" +
          "border-radius:50%;background:#fff;color:#1565c0;font-weight:700;font-size:13px;" +
          "text-decoration:none;box-shadow:0 1px 4px rgba(0,0,0,0.35);";
        link.addEventListener("click", (e) => e.stopPropagation());
        altChip.appendChild(link);
      });
      wrapper.appendChild(altChip);
    }

    // A separate, third chip - not merged into the status chip's icon -
    // for "this beer is saved somewhere": the classic Untappd Wishlist
    // (inWishlist) and/or a personal Google Sheet standing in for
    // Untappd's own unreachable "Lists" feature (inSheetList, see
    // WISHLIST_SHEET_CSV_URL in webapp_server.py). Only rendered when at
    // least one is true, so it never eats a corner on the common case.
    // listCorner defaults to "bottom-left", the one corner still free on
    // most adapters after status(top-left)/alt(top-right); adapters that
    // already use bottom-left for something else override it - ontap.pl
    // has NO fully free corner at all (both right corners are its own
    // theme badges), so it stacks this chip in the SAME top-left corner
    // as the status chip instead, offset further out
    // (listStackOffset) rather than overlapping it.
    if (result.matched && (result.inWishlist || result.inSheetList)) {
      const listChip = document.createElement("span");
      listChip.className = "lens-list-chip";
      const listCorner = adapter.listCorner || "bottom-left";
      const listOffset = adapter.listStackOffset;
      // Plain CHIP_STYLE (white bg), same as the status chip - a solid
      // yellow fill used to make the 🔖/📋 icons themselves hard to see,
      // especially against a pale/tan bottle label.
      listChip.style.cssText = CHIP_STYLE + cornerStyle(listCorner, listOffset);
      let listIcon = "";
      let listTooltip = "";
      if (result.inWishlist) { listIcon += "🔖"; listTooltip += "У вішлісті\n"; }
      if (result.inSheetList) { listIcon += "📋"; listTooltip += "Є в твоєму списку\n"; }
      listChip.textContent = listIcon;
      listChip.title = listTooltip.trim();
      wrapper.appendChild(listChip);
    }
  }

  function addRefreshButton() {
    const btn = document.createElement("button");
    btn.textContent = "🔄 Оновити";
    btn.title = "Очистити кеш і перезапитати всі пива на сторінці";
    btn.style.cssText =
      "position:fixed;bottom:16px;right:16px;z-index:99999;padding:8px 14px;" +
      "border:none;border-radius:8px;background:#333;color:#fff;font-size:13px;" +
      "cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,0.3);";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "…";
      saveCache({}); // wipe the 6h cache - see CACHE_TTL_MS
      document.querySelectorAll(".lens-status-chip, .lens-rating-chip, .lens-list-chip").forEach((el) => el.remove());
      await run();
      btn.textContent = original;
      btn.disabled = false;
    });
    document.body.appendChild(btn);
  }

  async function run() {
    const cache = loadCache();
    const now = Date.now();
    const cardsByKey = new Map();
    const toFetch = [];

    // adapter.extract may be async (e.g. hoptimaal.com fetches the
    // product's own vendor field) - awaiting each one in parallel keeps a
    // page of 40-60 cards fast instead of a serial per-card round trip.
    // A plain (synchronous) extract() still works fine here: awaiting a
    // non-Promise value just resolves immediately.
    const cards = Array.from(document.querySelectorAll(adapter.cardSelector)).filter(
      (card) => !(adapter.isBundle && adapter.isBundle(card)) // mixed/gift set, no single beer to look up
    );
    const extractedList = await Promise.all(cards.map((card) => adapter.extract(card)));

    cards.forEach((card, i) => {
      const extracted = extractedList[i];
      if (!extracted || !extracted.name) return;
      const { brewery, name } = extracted;
      const key = cacheKey(brewery, name);
      cardsByKey.set(key, card);
      const cached = cache[key];
      if (cached && now - cached.at < CACHE_TTL_MS) {
        renderOverlay(card, cached.result);
      } else {
        toFetch.push({ brewery, name, key });
      }
    });

    if (!toFetch.length) return;

    try {
      const results = await apiLookup(toFetch.map((it) => ({ name: it.name, brewery: it.brewery })));
      results.forEach((result, i) => {
        const { key } = toFetch[i];
        cache[key] = { at: now, result };
        const card = cardsByKey.get(key);
        if (card) renderOverlay(card, result);
      });
      saveCache(cache);
    } catch (e) {
      console.error("[UntappdLens] lookup failed:", e);
    }
  }

  // Some shops paginate/filter the product grid client-side (History API +
  // an AJAX swap of the grid's contents, no real page load) - confirmed
  // live on hopincraftbier.be: clicking "Next" replaces the card grid via
  // pushState, so DOMContentLoaded never fires again and cards just sit
  // unprocessed until the user notices and hits "Оновити" manually. Watch
  // for card elements being ADDED anywhere in the page and re-run
  // automatically, debounced so a burst of mutations only triggers one
  // run(). Filtered to additions that actually match/contain the card
  // selector so this never fires on OUR OWN mutations (the status/rating
  // chips run() itself appends aren't cards and don't match the
  // selector) - no risk of triggering itself in a loop.
  function watchForNewCards() {
    let debounceTimer = null;
    const observer = new MutationObserver((mutations) => {
      const hasNewCards = mutations.some((m) =>
        Array.from(m.addedNodes).some(
          (n) =>
            n.nodeType === Node.ELEMENT_NODE &&
            (n.matches?.(adapter.cardSelector) || n.querySelector?.(adapter.cardSelector))
        )
      );
      if (!hasNewCards) return;
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(run, 400);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  function init() {
    addRefreshButton();
    run();
    watchForNewCards();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
