(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    applyTelegramTheme();
    tg.onEvent("themeChanged", applyTelegramTheme);
  }

  // The app draws its own light/dark palette (see style.css's :root) rather
  // than Telegram's theme colors - this only mirrors WHICH scheme Telegram
  // is in onto <html data-theme>, then paints Telegram's own header/
  // background/bottom bar in the app's matching --bg so there's no
  // mismatched strip around the page. Each setter is version-gated by
  // Telegram and may throw on older clients.
  function applyTelegramTheme() {
    document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    if (!bg) return;
    try { tg.setHeaderColor(bg); } catch (e) { /* unsupported client */ }
    try { tg.setBackgroundColor(bg); } catch (e) { /* unsupported client */ }
    try { if (tg.setBottomBarColor) tg.setBottomBarColor(bg); } catch (e) { /* unsupported client */ }
  }
  const initData = tg ? tg.initData : "";

  // Public, read-only festival map page (/map - see webapp_server.py's
  // handle_public_index): same file, no Telegram login. Only the map, its
  // brewery lists and a festival-database search work; everything personal
  // (queue, check-ins, wishlist, settings...) is cut off at apiPost below,
  // so the many startup calls those features make just quietly no-op.
  const PUBLIC_MODE = window.PUBLIC_MAP === true;
  const PUBLIC_FEST = new URLSearchParams(window.location.search).get("fest");
  const PUBLIC_API_PATHS = {
    "/api/checkin/i18n": "/api/public/i18n",
    "/api/checkin/festival_map/get": "/api/public/festival_map/get",
    "/api/checkin/festival/brewery": "/api/public/festival/brewery",
    "/api/checkin/festival/meta": "/api/public/festival/meta",
    "/api/public/search": "/api/public/search",
  };

  const DEFAULT_LABEL_URL = "https://assets.untappd.com/site/assets/images/temp/badge-beer-default.png";

  // Flat-design icon set (inline SVG, currentColor-driven - see style.css's
  // own .icon/.icon-filled utility classes) replacing the old ad-hoc emoji
  // used throughout this file's dynamically-built row/button markup.
  // Reference the same <symbol> definitions as index.html's own sprite
  // block (see its comment) - one shape per icon, shared by both this
  // file's dynamic HTML and index.html's static buttons.
  const ICON_CLOSE = '<svg class="icon"><use href="#icon-close"/></svg>';
  const ICON_LINK = '<svg class="icon"><use href="#icon-external-link"/></svg>';
  const ICON_CHECK = '<svg class="icon"><use href="#icon-check"/></svg>';
  const ICON_CLIPBOARD = '<svg class="icon"><use href="#icon-clipboard-list"/></svg>';
  const ICON_PIN = '<svg class="icon"><use href="#icon-pin"/></svg>';
  const ICON_COMPASS = '<svg class="icon"><use href="#icon-compass"/></svg>';
  const ICON_CHAT = '<svg class="icon"><use href="#icon-chat"/></svg>';
  const ICON_TROPHY = '<svg class="icon"><use href="#icon-trophy"/></svg>';
  const ICON_BEER = '<svg class="icon"><use href="#icon-beer"/></svg>';
  const ICON_STAR = '<svg class="icon icon-filled"><use href="#icon-star"/></svg>';
  const ICON_HEART = '<svg class="icon icon-filled"><use href="#icon-heart"/></svg>';
  const ICON_AWARD = '<svg class="icon"><use href="#icon-award"/></svg>';
  const ICON_REFRESH = '<svg class="icon"><use href="#icon-refresh"/></svg>';
  // Maxed-out badge marker, in front of its name (replaces the old trophy emoji).
  const doneMark = () => `<span class="badge-done-mark" title="${escapeHtml(T("app_badge_max_level"))}">${ICON_TROPHY}</span>`;

  // Telegram's own WebView doesn't reliably handle a plain <a target="_blank">
  // - tg.openLink is the documented way to hand a URL off to the system
  // browser/app from inside a Mini App.
  function openExternalLink(url) {
    if (tg && tg.openLink) {
      tg.openLink(url);
    } else {
      window.open(url, "_blank");
    }
  }

  // Escape valve for the Untappd MCP quota (100/rolling-hour per access
  // token, confirmed live via get_untappd_api_usage) - opening the beer's
  // real Untappd page costs nothing on our side, so it's always available
  // even when the quota's tight or check_in itself is failing.
  function openUntappdBeer(beerId, e) {
    if (e) e.stopPropagation();
    openExternalLink(`https://untappd.com/beer/${beerId}`);
  }

  // Android-Gallery-style long-press-to-select (queue + "Мій список") -
  // Pointer Events unify touch/mouse so this also works with a held-down
  // mouse click during desktop testing. Cancels on real movement (a scroll
  // drag) or an early release, so it only fires on a genuine press-and-hold.
  function bindLongPress(el, onLongPress) {
    const LONG_PRESS_MS = 500;
    const MOVE_CANCEL_PX = 10;
    let timer = null;
    let startX = 0;
    let startY = 0;
    const cancel = () => { clearTimeout(timer); timer = null; };
    el.addEventListener("pointerdown", (e) => {
      startX = e.clientX;
      startY = e.clientY;
      cancel();
      timer = setTimeout(() => { timer = null; onLongPress(); }, LONG_PRESS_MS);
    });
    el.addEventListener("pointerup", cancel);
    el.addEventListener("pointercancel", cancel);
    el.addEventListener("pointermove", (e) => {
      if (timer && (Math.abs(e.clientX - startX) > MOVE_CANCEL_PX || Math.abs(e.clientY - startY) > MOVE_CANCEL_PX)) cancel();
    });
  }

  const state = {
    selectedBeer: null,
    rating: 4,
    venues: null,
    selectedVenue: null,
    lastVenue: null,     // remembered from the previous check-in - festival venue doesn't change mid-day
    lastKnownLocation: null, // cached after a successful nearby-venues tap - reused to geo-bias text search
    lastVenueSearch: null,   // {type:"nearby",lat,lng} | {type:"query",query} - replayed when a filter checkbox toggles
    queue: [],          // shared, server-backed - everyone in the group sees the same list
    wishlist: [],       // personal - own items merged server-side with the user's Google Sheet rows
    wishlistSort: "date",
    pendingCheckins: [], // failed check-in attempts saved for manual retry - see fetchPendingCheckins
    currentSession: null, // which session's drill-down list is currently open
    origin: "search",    // where to return after rate/confirm: "search", "queue" or "session-beers"
    queueItemId: null,   // the server's item id, not an array index (another phone can remove items)
    badgesRaw: [],       // last /api/checkin/badges/get fetch - re-filtered/sorted client-side, no re-fetch needed
    badgesSort: "closest",
    badgesView: "grid",
    selectedBadge: null, // drill-down target for screen-badge-detail
    festivalMode: false,        // mirrors festival_mode.py - gates festival-only UI, see applyFestivalModeUI
    autoToastAvailable: false,  // combined with festivalMode below - see updateAutoToastRowVisibility
    festivalSwitchAvailable: false, // owner-only, see updateFestivalSwitchRowVisibility
    festivals: [],       // last /api/checkin/festival/list fetch - [{key, label}]
    activeFestivalKey: null,
    myFestivals: [],      // last /api/checkin/festival/my/get fetch - open to everyone, no availability gate
    myPersonalKey: null,  // this viewer's own override, or null (falls back to their group/the default)
    myEffectiveKey: null, // what they'd actually see right now, after the full resolution chain
    commandFlagsAvailable: false, // owner-only, see updateCommandFlagsRowVisibility
    commandFlags: [],     // last /api/checkin/command_flags/get fetch - [{command, label, description, enabled}]
    photoRecognitionEnabled: true,
    festivalMap: { zones: {}, bonusCategories: {} }, // shared, server-backed - see festival_map.py
  };
  let festivalMapEditMode = false;
  let mapDragActive = false; // true while a pointer drag is in progress - blocks fetchFestivalMap's re-render

  function $(id) { return document.getElementById(id); }

  // Wraps a search input with a right-aligned "x" clear button, shown only
  // once there's text. Clearing dispatches a real "input" event so each
  // field's own existing listener (debounced fetch, live filter, whatever
  // it already does on typing) fires exactly as if the user had cleared it
  // by hand - no per-field special-casing needed here.
  function addSearchClearButton(inputId) {
    const input = $(inputId);
    const wrap = document.createElement("div");
    wrap.className = "search-field-wrap";
    // Named so a screen that hides #<inputId> via CSS (e.g. edit mode
    // hiding the map's own search box) can also target this wrapper
    // specifically - the wrapper (and its clear button) is a SEPARATE
    // element from the input itself, so hiding just the input id alone
    // left the clear button behind, floating over whatever content came
    // after - confirmed live, on the festival map's edit screen.
    wrap.id = `${inputId}-wrap`;
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "search-clear-btn hidden";
    clearBtn.dataset.i18nAria = "app_clear";
    clearBtn.innerHTML = ICON_CLOSE;
    wrap.appendChild(clearBtn);

    const syncVisibility = () => clearBtn.classList.toggle("hidden", !input.value);
    input.addEventListener("input", syncVisibility);
    syncVisibility();

    clearBtn.addEventListener("click", () => {
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    });
  }

  // Festival mode toggles which UI even makes sense: the festival-specific
  // tools (progress/sessions, the festival-priority search filter) are
  // clutter on a normal day and only relevant while actually AT a
  // festival, while auto-toast (a passive day-to-day feature) is the one
  // thing that's actively paused server-side during festival mode (see
  // festival_mode.py) - so its settings row is hidden right along with it
  // rather than left showing a now-inert toggle. Also flips the whole
  // app's accent color (see style.css's body.festival-mode-active) as an
  // at-a-glance "you're in festival mode" cue.
  function applyFestivalModeUI() {
    document.body.classList.toggle("festival-mode-active", state.festivalMode);
    $("stats-bar-btn").classList.toggle("hidden", !state.festivalMode);
    $("festival-priority-label").classList.toggle("hidden", !state.festivalMode);
    // The festival map is only relevant while actually at the festival -
    // its bottom-nav tab takes the Badges tab's slot while festival mode is
    // on, the same trade-off stats-bar-btn already makes the other way
    // (festival-only tools aren't worth a spot the rest of the year).
    $("badges-bar-btn").classList.toggle("hidden", state.festivalMode);
    $("festival-map-bar-btn").classList.toggle("hidden", !state.festivalMode);
    // Mirrors festivalMode exactly, both ways: turning festival mode ON
    // should turn festival-priority search back on too (that's the whole
    // point of the checkbox), and turning it OFF shouldn't leave an
    // invisible checkbox silently still boosting festival results.
    const festivalCheckbox = $("festival-priority-checkbox");
    if (festivalCheckbox.checked !== state.festivalMode) {
      festivalCheckbox.checked = state.festivalMode;
      rerunSearchIfActive();
    }
    updateAutoToastRowVisibility();
  }

  function updateAutoToastRowVisibility() {
    $("settings-row-autotoast").classList.toggle("hidden", !state.autoToastAvailable || state.festivalMode);
  }

  function updateFestivalSwitchRowVisibility() {
    $("settings-row-festivalswitch").classList.toggle("hidden", !state.festivalSwitchAvailable);
  }

  function updateCommandFlagsRowVisibility() {
    $("settings-row-commandflags").classList.toggle("hidden", !state.commandFlagsAvailable);
  }

  // Search row's "..." overflow menu (wishlist toggle + Untappd link) - a
  // single shared, body-level element (see index.html's own comment on it)
  // repositioned/repopulated per row on each "..." tap, rather than one
  // nested inside every row: nesting it in a dimmed .result-row.had-it
  // made it visibly inherit that row's opacity, with no CSS way for the
  // menu to opt back out of an ancestor's opacity via its own opacity:1.
  const sharedRowMenu = $("shared-row-menu");
  const sharedRowMenuWishlistBtn = $("shared-row-menu-wishlist");
  const sharedRowMenuLinkBtn = $("shared-row-menu-link");
  let openRowMenuActions = null; // the .row-actions currently owning the shared menu, if any

  function closeAllRowMenus() {
    sharedRowMenu.classList.remove("open");
    openRowMenuActions = null;
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".row-menu") && !e.target.closest(".row-menu-btn")) closeAllRowMenus();
  });

  function openRowMenu(beer, actionsEl) {
    if (openRowMenuActions === actionsEl) { closeAllRowMenus(); return; }
    const menuBtn = actionsEl.querySelector(".row-menu-btn");
    setWishlistBtnState(sharedRowMenuWishlistBtn, actionsEl.dataset.wishlistItemId || null);
    sharedRowMenuWishlistBtn.onclick = async (e) => {
      e.stopPropagation();
      await toggleWishlistOnRow(beer, actionsEl);
    };
    sharedRowMenuLinkBtn.onclick = (e) => {
      openUntappdBeer(beer.beerId, e);
      closeAllRowMenus();
    };
    const rect = menuBtn.getBoundingClientRect();
    sharedRowMenu.style.top = `${rect.top + window.scrollY}px`;
    sharedRowMenu.style.left = `${rect.right + window.scrollX}px`;
    sharedRowMenu.classList.add("open");
    openRowMenuActions = actionsEl;
  }

  let queuePollHandle = null;
  let statsPollHandle = null;
  let mapPollHandle = null;
  // Screens have no scroll container of their own (see .screen's plain
  // display:none/block toggle in style.css) - the page itself scrolls, so
  // "where you'd scrolled to" is just window.scrollY. Saved whenever
  // LEAVING the badges list (a detail tap, a different tab, doesn't
  // matter) and restored once fetchBadgeStats re-renders it - without
  // this, going search -> badge detail -> back always dumped you back at
  // the very top of a ~130-badge list.
  let badgesListScrollY = 0;
  // Screens don't map 1:1 to bottom-nav tabs (e.g. session-beers/badge-
  // detail are drill-downs with no tab of their own) - this maps only the
  // ones that DO have an obvious "you're in this section" tab.
  const NAV_TAB_FOR_SCREEN = {
    search: "home-bar-btn",
    queue: "queue-bar-btn",
    stats: "stats-bar-btn",
    "session-beers": "stats-bar-btn",
    wishlist: "wishlist-bar-btn",
    "pending-checkins": "pending-checkins-bar-btn",
    badges: "badges-bar-btn",
    "badge-detail": "badges-bar-btn",
    "festival-map": "festival-map-bar-btn",
  };

  // Rule for every screen dispatched below that fetches a list/grid: clear
  // that list's own container (and set a loading status, if it has
  // one) BEFORE the fetch starts, never only after it resolves - a fetch
  // function that clears at the END (or not at all) leaves whatever was
  // last rendered there (a previous visit's data, a different festival's,
  // a stale search) visible for the whole round-trip, which reads as a
  // flash of WRONG content rather than a loading state (confirmed live,
  // on the festival map screen, before this was written). For a screen
  // that's also on a poll interval (queue, festival-map), do the clear in
  // THIS function's own dispatch - guarded by "the poll handle is still
  // null", i.e. a genuinely fresh open - not inside the polled fetch
  // function itself, which would otherwise flash the list empty on every
  // routine poll tick while the viewer is just sitting on the screen.
  function showScreen(name) {
    if (document.getElementById("screen-badges")?.classList.contains("active") && name !== "badges") {
      badgesListScrollY = window.scrollY;
    }
    document.querySelectorAll(".screen").forEach((el) => el.classList.remove("active"));
    $("screen-" + name).classList.add("active");
    document.querySelectorAll(".nav-tab.active").forEach((el) => el.classList.remove("active"));
    const activeTabId = NAV_TAB_FOR_SCREEN[name];
    if (activeTabId) $(activeTabId).classList.add("active");
    // Long-press selection mode (queue/wishlist) shouldn't survive leaving
    // (or re-entering) that screen - stale selected ids from a previous
    // visit would otherwise silently carry over.
    if (queueSelectionMode) exitQueueSelectionMode();
    if (wishlistSelectionMode) exitWishlistSelectionMode();
    // The shared row menu (see openRowMenu) lives at body level, outside
    // every .screen - it wouldn't get hidden by the .screen swap above on
    // its own, so a menu left open on the way out of search would keep
    // floating over whatever screen comes next.
    closeAllRowMenus();
    if (name === "queue") {
      // Only on a genuinely fresh open (`!queuePollHandle`, since leaving
      // this screen always clears the handle - see the `else` below) -
      // NOT on every 5s poll tick, which would otherwise flash the whole
      // list empty every 5 seconds while just sitting on the screen.
      if (!queuePollHandle) $("queue-list").innerHTML = "";
      fetchQueue();
      if (!queuePollHandle) queuePollHandle = setInterval(fetchQueue, 5000);
    } else if (queuePollHandle) {
      clearInterval(queuePollHandle);
      queuePollHandle = null;
    }
    if (name === "stats") {
      fetchFestivalStats();
      // Slower than the queue's 5s - personal festival progress changes
      // far less often per tick than a live shared queue does.
      if (!statsPollHandle) statsPollHandle = setInterval(fetchFestivalStats, 15000);
    } else if (statsPollHandle) {
      clearInterval(statsPollHandle);
      statsPollHandle = null;
    }
    if (name === "session-beers") {
      fetchSessionBeers();
    }
    if (name === "brewery-beers") {
      fetchBreweryBeers();
    }
    if (name === "wishlist") {
      fetchWishlist();
    }
    if (name === "pending-checkins") {
      fetchPendingCheckins();
    }
    if (name === "autotoast") {
      fetchAutoToastFriends();
    }
    if (name === "festival-watch") {
      fetchFestivalWatch();
    }
    if (name === "settings") {
      fetchSettingsStatus();
    }
    if (name === "events") {
      fetchEvents();
    }
    if (name === "badges") {
      // Restored synchronously, BEFORE the async fetch below, against
      // whatever's still in #badges-list from the last render - without
      // this, the screen paints at scroll 0 for one frame (the fetch/
      // render below only finishes after an await) and then visibly jumps,
      // instead of just already being there. fetchBadgeStats' own restore
      // (after the fresh render) is the fallback for when a count/order
      // change shifts the list's height enough to matter.
      window.scrollTo(0, badgesListScrollY);
      fetchBadgeStats();
      fetchSpecialBadges();
    }
    if (name === "badge-detail") {
      renderBadgeDetail();
    }
    if (name === "style-info") {
      renderStyleInfo();
    }
    if (name === "festival-switch") {
      fetchFestivalList();
    }
    if (name === "my-festival") {
      fetchMyFestivalList();
    }
    if (name === "command-flags") {
      fetchCommandFlags();
    }
    if (name === "festival-map") {
      // Only on a genuinely fresh open (see the queue screen's identical
      // note just above) - otherwise whatever was left in #festival-map-
      // zones from the LAST time this screen was open (a different
      // festival's layout, or just stale positions) stays visible for
      // the whole fetch round-trip - confirmed live as a visible flash
      // of wrong content on tapping into this screen.
      if (!mapPollHandle) $("festival-map-zones").innerHTML = "";
      fetchFestivalMap();
      // Same 5s cadence as the queue - the precedent for "live shared state."
      if (!mapPollHandle) mapPollHandle = setInterval(fetchFestivalMap, 5000);
    } else if (mapPollHandle) {
      clearInterval(mapPollHandle);
      mapPollHandle = null;
      clearActiveSearchHighlight(); // leaving the map entirely - the "selected from search" marker shouldn't survive that
    }
    // Telegram's native BackButton, not just a UI convenience: on Android it
    // replaces the system back button/gesture too (which otherwise just
    // minimizes the whole Mini App instead of navigating within it). Hidden
    // on the top-level search screen - nowhere further back to go from there.
    if (tg && tg.BackButton) {
      if (name === "search") tg.BackButton.hide();
      else tg.BackButton.show();
    }
  }

  // Reuses whatever back control the current screen already has (a
  // [data-back] button, or screen-rate's dedicated #rate-back-btn) instead
  // of duplicating each screen's "where does back go" logic a second time.
  if (tg && tg.BackButton) {
    tg.BackButton.onClick(() => {
      const active = document.querySelector(".screen.active");
      const backBtn = active && active.querySelector("[data-back], #rate-back-btn");
      if (backBtn) backBtn.click();
    });
  }

  document.querySelectorAll("[data-back]").forEach((btn) => {
    btn.addEventListener("click", () => showScreen(btn.dataset.back));
  });

  async function apiPost(path, body) {
    if (PUBLIC_MODE) {
      const publicPath = PUBLIC_API_PATHS[path];
      if (!publicPath) return { ok: false, status: 403, data: {} };
      path = publicPath;
      body = Object.assign({}, body, { fest: PUBLIC_FEST, lang: navigator.language });
    }
    const resp = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": PUBLIC_MODE ? "" : initData,
      },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json().catch(() => ({}));
    return { ok: resp.ok, status: resp.status, data };
  }

  // ---- UI strings in the viewer's own language ----
  // The table comes from the server (i18n.py's "app_" keys, picked by this
  // Telegram client's language_code) - never hardcode user-facing text in
  // this file; add a key to i18n.py for every language instead. Static HTML
  // text uses data-i18n / data-i18n-placeholder, filled in by loadI18n.
  let I18N = {};
  let I18N_LANG = "en";
  function T(key, params) {
    let text = I18N[key] != null ? I18N[key] : key;
    if (params) {
      Object.keys(params).forEach((k) => { text = text.split(`{${k}}`).join(params[k]); });
    }
    return text;
  }
  // Plural forms: keys base_one / base_few / base_many (uk) or base_one /
  // base_other (everything else), each with {n} in the text.
  function TP(base, n, params) {
    const m10 = n % 10, m100 = n % 100;
    let cat;
    if (I18N_LANG === "uk" || I18N_LANG === "ru") {
      cat = (m10 === 1 && m100 !== 11) ? "one"
        : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) ? "few" : "many";
    } else {
      cat = n === 1 ? "one" : "other";
    }
    return T(`${base}_${cat}`, Object.assign({ n }, params));
  }
  async function loadI18n() {
    const { ok, data } = await apiPost("/api/checkin/i18n", {});
    if (ok && data.strings) { I18N = data.strings; I18N_LANG = data.lang || "en"; }
    document.documentElement.lang = I18N_LANG;
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = T(el.dataset.i18n); });
    // Only for trusted, static strings from i18n.py that carry markup.
    document.querySelectorAll("[data-i18n-html]").forEach((el) => { el.innerHTML = T(el.dataset.i18nHtml); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = T(el.dataset.i18nPlaceholder); });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = T(el.dataset.i18nTitle); });
    document.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", T(el.dataset.i18nAria)); });
    if (PUBLIC_MODE) {
      $("festival-map-search-input").placeholder = T("app_public_search_ph");
      document.title = T("app_map_title");
    }
  }
  const i18nReady = loadI18n();

  // ---- Shared queue (solves "which beer is in which glass / who brought what") ----
  // Server-backed so the whole group sees the same list on every phone.

  async function fetchQueue() {
    const { ok, data } = await apiPost("/api/checkin/queue/list", {});
    state.queue = ok ? (data.items || []) : [];
    state.queueTotal = ok ? (data.total || 0) : 0;
    // noGroup (no /join_group run yet) is distinct from a genuinely empty
    // queue - see webapp_server.py's handle_queue_list.
    state.queueNoGroup = ok ? !!data.noGroup : false;
    state.queueGroupTitle = ok ? (data.groupTitle || "") : "";
    renderQueueList();
  }

  function notify(message) {
    if (tg && tg.showAlert) tg.showAlert(message);
    else alert(message);
  }

  // Small corner badge on every beer row (search/session-beers/brewery-
  // beers/wishlist) answering "have I already queued this?" before
  // tapping "+" again - queueStatus comes from _annotate_queue_status
  // (webapp_server.py), "active" for a beer currently in this viewer's
  // own queue view, "was_in_queue" for one they've since hidden or
  // completed. Same tooltip text either way, tap-triggered (not CSS
  // hover) since that's the only reliable way to show it inside
  // Telegram's mobile WebView.
  function queueStatusCornerHtml(beer) {
    if (beer.queueStatus === "active") {
      return `<span class="queue-status-corner active">${ICON_CLIPBOARD}</span>`;
    }
    if (beer.queueStatus === "was_in_queue") {
      return `<span class="queue-status-corner was">${ICON_CLIPBOARD}</span>`;
    }
    return "";
  }
  function bindQueueStatusCorner(row) {
    const el = row.querySelector(".queue-status-corner");
    if (!el) return;
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      notify(T("app_in_queue_tip"));
    });
  }

  // Inline badge for a queue row the viewer already tried to check in -
  // Untappd failed at the time, so it's sitting in "Відкладені" instead of
  // completing this queue item (see pendingForMe, set server-side in
  // handle_queue_list from pending_checkins.list_items). Without this the
  // row looks untouched even though it's already been attempted and is
  // just waiting on a manual retry. Tapping it jumps straight there.
  function pendingForMeBadgeHtml(beer) {
    return beer.pendingForMe
      ? `<span class="badge badge-pending" title="${escapeHtml(T("app_pending_badge_title"))}">${ICON_REFRESH}</span>`
      : "";
  }

  async function addToQueue(beer, btn) {
    const { ok, data } = await apiPost("/api/checkin/queue/add", beer);
    if (!ok || !data.ok) {
      // "Спробуйте ще раз" would be wrong advice here - retrying can't help
      // until someone runs /join_group, so this gets its own message rather
      // than falling into the generic failure text below.
      if (data && data.error === "no_active_group") {
        notify(T("app_queue_join_group"));
      } else {
        notify(T("app_queue_add_failed"));
      }
      return;
    }
    // See checkin_queue.add_item's own docstring for what each status means -
    // "already_active" is a plain duplicate tap (nothing to do), the two
    // "revived_from_*" cases did change something (the beer is back in this
    // user's own queue view) so they still get the success flash below, just
    // with an explanation of why it wasn't a fresh add.
    if (data.status === "already_active") {
      notify(T("app_queue_already"));
      return;
    }
    if (data.status === "revived_from_completed") {
      notify(T("app_queue_had_it_festival"));
    } else if (data.status === "revived_from_hidden") {
      notify(T("app_queue_removed_before"));
    }
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    if (btn) {
      const original = btn.innerHTML;
      btn.innerHTML = ICON_CHECK;
      setTimeout(() => { btn.innerHTML = original; }, 1000);
    }
    updateQueueCountOnly();
  }

  async function updateQueueCountOnly() {
    const { ok, data } = await apiPost("/api/checkin/queue/list", {});
    const items = ok ? (data.items || []) : [];
    const total = ok ? (data.total || 0) : 0;
    $("queue-count").textContent = String(items.length);
    // Hide only when the shared queue is genuinely empty - not when it's
    // just empty *for you* (e.g. you added a beer you'd already had, which
    // is immediately filtered from your own view but still there for others).
    $("queue-bar-btn").classList.toggle("hidden", total === 0);
  }

  // Long-press-to-select ("delete selected") - see bindLongPress's own
  // comment. Entering/leaving mode just flips this state and re-renders
  // (cheap, no network); toggleQueueSelected auto-exits once the set empties.
  let queueSelectionMode = false;
  const queueSelectedIds = new Set();

  function enterQueueSelectionMode(firstId) {
    queueSelectionMode = true;
    queueSelectedIds.clear();
    queueSelectedIds.add(firstId);
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("medium");
    renderQueueList();
  }
  function exitQueueSelectionMode() {
    queueSelectionMode = false;
    queueSelectedIds.clear();
    renderQueueList();
  }
  function toggleQueueSelected(id) {
    if (queueSelectedIds.has(id)) queueSelectedIds.delete(id); else queueSelectedIds.add(id);
    if (queueSelectedIds.size === 0) { exitQueueSelectionMode(); return; }
    renderQueueList();
  }

  // Ukrainian plural form: 1 пиво, 2-4 пива, 5+ пив (11-14 always "many").
  function renderQueueList() {
    const listEl = $("queue-list");
    listEl.innerHTML = "";
    listEl.classList.toggle("selection-mode", queueSelectionMode);
    $("queue-header-normal").classList.toggle("hidden", queueSelectionMode);
    $("queue-selection-bar").classList.toggle("hidden", !queueSelectionMode);
    $("queue-selection-count").textContent = T("app_selected_count", { n: queueSelectedIds.size });
    $("queue-count").textContent = String(state.queue.length);
    // "4 пива · 2 учасники" under the title - participants are the distinct
    // people who added what's currently visible in this viewer's queue.
    // Prefixed with the active group's own name (when known) so it's clear
    // whose queue this is, since it's no longer the single global one.
    const people = new Set(state.queue.map((b) => (b.addedBy && (b.addedBy.userId ?? b.addedBy.name)) || "?"));
    const groupPrefix = state.queueGroupTitle ? `${state.queueGroupTitle} · ` : "";
    $("queue-subtitle").textContent = state.queue.length
      ? `${groupPrefix}${TP("app_beers", state.queue.length)} · ${TP("app_participants", people.size)}`
      : "";
    $("queue-select-hint").classList.toggle("hidden", state.queue.length < 2 || queueSelectionMode);
    $("queue-bar-btn").classList.toggle("hidden", (state.queueTotal || 0) === 0);
    $("queue-status").textContent = state.queue.length
      ? ""
      : (state.queueNoGroup
        ? T("app_queue_not_joined")
        : (state.queueTotal
          ? T("app_queue_all_done")
          : T("app_queue_empty")));
    state.queue.forEach((beer, idx) => {
      const row = document.createElement("div");
      const checked = queueSelectedIds.has(beer.id);
      const addedBy = beer.addedBy && beer.addedBy.name ? beer.addedBy.name : "?";
      // The queue is append-only, so the first item this viewer still sees
      // is the oldest one they haven't checked in or hidden - shown as a
      // featured "Наступне" card. Selection mode falls back to plain rows
      // so every item gets the same checkbox layout.
      const featured = idx === 0 && !queueSelectionMode;
      if (idx === 1 && !queueSelectionMode) {
        const label = document.createElement("div");
        label.className = "queue-section-label";
        label.textContent = T("app_queue_later");
        listEl.appendChild(label);
      }
      row.className = "result-row queue-row" + (featured ? " queue-next" : "") + (beer.hadIt ? " had-it" : "");
      if (featured) {
        row.innerHTML = `
        <div class="queue-next-head">
          <span class="queue-next-label">${escapeHtml(T("app_queue_next"))}</span>
          <span class="queue-next-by">${escapeHtml(T("app_added_by", { name: addedBy }))}</span>
        </div>
        <div class="queue-next-body">
          <div class="thumb"><img src="${beer.labelUrl || DEFAULT_LABEL_URL}" alt=""></div>
          <div class="result-main">
            <div class="result-name">${beer.hadIt ? `<span class="badge">${ICON_CHECK}</span>` : ""}${pendingForMeBadgeHtml(beer)}<span class="result-name-text">${escapeHtml(beer.name || "")}</span></div>
            ${metaLine(beer.brewery)}
            ${metaChips(beer)}
          </div>
          <div class="row-actions">
            <button class="queue-remove-btn" data-id="${beer.id}" aria-label="${escapeHtml(T("app_queue_remove_aria"))}">${ICON_CLOSE}</button>
            <button class="untappd-link-btn" title="${escapeHtml(T("app_open_in_untappd"))}">${ICON_LINK}</button>
          </div>
        </div>
        <button class="primary-btn queue-next-btn">${ICON_CHECK} ${escapeHtml(T("app_rate_and_checkin"))}</button>`;
      } else row.innerHTML = `
        <span class="row-checkbox${checked ? " checked" : ""}">${checked ? ICON_CHECK : ""}</span>
        <div class="queue-number">${idx + 1}</div>
        <div class="thumb"><img src="${beer.labelUrl || DEFAULT_LABEL_URL}" alt=""></div>
        <div class="result-main">
          <div class="result-name">${beer.hadIt ? `<span class="badge">${ICON_CHECK}</span>` : ""}${pendingForMeBadgeHtml(beer)}<span class="result-name-text">${escapeHtml(beer.name || "")}</span></div>
          ${metaLine(beer.brewery)}
          ${metaChips(beer)}
          <div class="result-meta">${escapeHtml(T("app_added_by", { name: addedBy }))}</div>
        </div>
        <div class="row-actions">
          <button class="queue-remove-btn" data-id="${beer.id}">${ICON_CLOSE}</button>
          <button class="untappd-link-btn" title="${escapeHtml(T("app_open_in_untappd"))}">${ICON_LINK}</button>
        </div>`;
      row.addEventListener("click", (e) => {
        if (queueSelectionMode) { toggleQueueSelected(beer.id); return; }
        if (e.target.closest(".queue-remove-btn") || e.target.closest(".untappd-link-btn") || e.target.closest(".badge-pending")) return;
        selectBeer(beer, { origin: "queue", queueItemId: beer.id });
      });
      bindLongPress(row, () => { if (!queueSelectionMode) enterQueueSelectionMode(beer.id); });
      row.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(beer.beerId, e));
      const pendingBadge = row.querySelector(".badge-pending");
      if (pendingBadge) {
        pendingBadge.addEventListener("click", (e) => {
          e.stopPropagation();
          showScreen("pending-checkins");
        });
      }
      listEl.appendChild(row);
    });
    listEl.querySelectorAll(".queue-remove-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        await apiPost("/api/checkin/queue/remove", { id: btn.dataset.id });
        fetchQueue();
      });
    });
  }

  $("home-bar-btn").addEventListener("click", () => showScreen("search"));
  $("queue-bar-btn").addEventListener("click", () => showScreen("queue"));

  $("queue-clear-btn").addEventListener("click", () => {
    const doClear = async () => {
      const { ok, data } = await apiPost("/api/checkin/queue/clear", {});
      if (ok && data.ok && tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      fetchQueue();
    };
    // Telegram's own confirm dialog when running as a real Mini App (matches
    // the host's UI instead of a browser-native prompt); plain confirm() as
    // a fallback for local/non-Telegram testing.
    if (tg && tg.showConfirm) {
      tg.showConfirm(T("app_queue_clear_confirm"), (confirmed) => { if (confirmed) doClear(); });
    } else if (confirm(T("app_queue_clear_confirm"))) {
      doClear();
    }
  });

  $("queue-selection-delete-btn").addEventListener("click", async () => {
    for (const id of queueSelectedIds) {
      await apiPost("/api/checkin/queue/remove", { id });
    }
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    exitQueueSelectionMode();
    fetchQueue();
  });
  $("queue-selection-cancel-btn").addEventListener("click", exitQueueSelectionMode);

  updateQueueCountOnly();
  updatePendingCheckinsCountOnly();

  // ---- Personal editable wishlist ("Мій список") ----
  // Per-user, unlike the shared queue above - own items (wishlist_items.py,
  // added/removed here) merged server-side with the user's registered
  // Google Sheet rows (wishlist_sheets.py), which show up read-only since
  // this app only ever reads that CSV, never writes it. Distinct from the
  // ❤️ "вішліст" search checkbox/badge, which boosts Untappd's own classic
  // Wishlist in search results - hence this uses 📝 everywhere instead.

  async function fetchWishlist() {
    // Cleared BEFORE the await, not after - otherwise whatever was in
    // #wishlist-list from a PREVIOUS visit (or a different account's
    // stale rows, in dev/preview testing) stays visible for the whole
    // round-trip and only gets replaced once the fetch resolves, which
    // reads as a flash of wrong data rather than a loading state. Same
    // "clear first, fetch after" rule applies to every list-rendering
    // fetch* function - see showScreen's own note for the polled ones.
    $("wishlist-list").innerHTML = "";
    $("wishlist-status").textContent = T("app_loading");
    const { ok, data } = await apiPost("/api/checkin/wishlist/list", {});
    state.wishlist = ok ? (data.items || []) : [];
    renderWishlistList();
  }

  function setWishlistBtnState(btn, itemId) {
    if (itemId) {
      btn.dataset.itemId = itemId;
      btn.innerHTML = `${ICON_CHECK} ${escapeHtml(T("app_wishlist_in_list"))}`;
      btn.classList.add("active");
    } else {
      delete btn.dataset.itemId;
      btn.innerHTML = `${ICON_CLIPBOARD} ${escapeHtml(T("app_wishlist_add"))}`;
      btn.classList.remove("active");
    }
  }

  // Patches a still-mounted search-result row (DOM persists across screens
  // - see .screen{display:none}) when this beer's list membership changes
  // from elsewhere (e.g. removed via the "Мій список" tab) - without this,
  // navigating back to search after removing there kept showing the old
  // "in list" state until the next fresh search.
  function syncSearchRowWishlistState(beerId, itemId) {
    const actions = document.querySelector(`.row-actions[data-beer-id="${beerId}"]`);
    if (!actions) return;
    actions.dataset.wishlistItemId = itemId || "";
    const menuBtn = actions.querySelector(".row-menu-btn");
    if (menuBtn) menuBtn.classList.toggle("has-wishlist-item", !!itemId);
  }

  // Toggle, not a one-way add: a row remembers whether it's already a
  // native list item via its own .row-actions[data-wishlist-item-id] (kept
  // in sync here and by syncSearchRowWishlistState), so a second tap
  // removes it again instead of being a same-bid no-op (see
  // wishlist_items.add_item's own dedupe).
  async function toggleWishlistOnRow(beer, actionsEl) {
    const currentItemId = actionsEl.dataset.wishlistItemId || "";
    const removing = !!currentItemId;
    const { ok, data } = removing
      ? await apiPost("/api/checkin/wishlist/remove", { id: currentItemId })
      : await apiPost("/api/checkin/wishlist/add", beer);
    if (!ok || !data.ok) {
      alert(T(removing ? "app_wishlist_remove_failed" : "app_wishlist_add_failed"));
      return;
    }
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    const newItemId = removing ? null : data.item.id;
    syncSearchRowWishlistState(beer.beerId, newItemId);
    setWishlistBtnState(sharedRowMenuWishlistBtn, newItemId);
    closeAllRowMenus();
  }

  function wishlistMatchesQuery(beer, query) {
    if (!query) return true;
    return `${beer.name || ""} ${beer.brewery || ""}`.toLowerCase().includes(query);
  }

  // Long-press-to-select, same mechanism as the queue's own (see its
  // comment above) - only native items participate (id set), since a
  // Google-Sheet-sourced row already has no remove button of its own to
  // begin with (see wishlist_items.py/_get_my_list_items).
  let wishlistSelectionMode = false;
  const wishlistSelectedIds = new Set();

  function enterWishlistSelectionMode(firstId) {
    wishlistSelectionMode = true;
    wishlistSelectedIds.clear();
    wishlistSelectedIds.add(firstId);
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("medium");
    renderWishlistList();
  }
  function exitWishlistSelectionMode() {
    wishlistSelectionMode = false;
    wishlistSelectedIds.clear();
    renderWishlistList();
  }
  function toggleWishlistSelected(id) {
    if (wishlistSelectedIds.has(id)) wishlistSelectedIds.delete(id); else wishlistSelectedIds.add(id);
    if (wishlistSelectedIds.size === 0) { exitWishlistSelectionMode(); return; }
    renderWishlistList();
  }

  // Sorts the already-fetched, already-merged (native + sheet) list
  // client-side - same rationale as sortedBadges(): re-fetching per toggle
  // would just round-trip data that's already sitting in state.wishlist.
  // "date" (default) puts real native items newest-added first; sheet rows
  // carry no addedAt at all, so they fall through to 0 and naturally sink
  // to the bottom - same place they already sat in the unsorted list.
  function sortedWishlist() {
    const list = state.wishlist.slice();
    if (state.wishlistSort === "alpha") {
      list.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    } else if (state.wishlistSort === "brewery") {
      list.sort((a, b) => (a.brewery || "").localeCompare(b.brewery || "") || (a.name || "").localeCompare(b.name || ""));
    } else if (state.wishlistSort === "abv") {
      list.sort((a, b) => (b.abv ?? -1) - (a.abv ?? -1));
    } else {
      list.sort((a, b) => (b.addedAt || 0) - (a.addedAt || 0));
    }
    return list;
  }

  function renderWishlistList() {
    const query = $("wishlist-search-input").value.trim().toLowerCase();
    const items = sortedWishlist().filter((beer) => wishlistMatchesQuery(beer, query));
    const listEl = $("wishlist-list");
    listEl.innerHTML = "";
    listEl.classList.toggle("selection-mode", wishlistSelectionMode);
    $("wishlist-header-normal").classList.toggle("hidden", wishlistSelectionMode);
    $("wishlist-selection-bar").classList.toggle("hidden", !wishlistSelectionMode);
    $("wishlist-selection-count").textContent = T("app_selected_count", { n: wishlistSelectedIds.size });
    if (!state.wishlist.length) {
      $("wishlist-status").textContent = T("app_wishlist_empty");
    } else {
      $("wishlist-status").textContent = items.length ? "" : T("app_nothing_found");
    }
    items.forEach((beer) => {
      const row = document.createElement("div");
      const isNative = beer.source === "native";
      const checked = isNative && wishlistSelectedIds.has(beer.id);
      row.className = "result-row" + (beer.hadIt ? " had-it" : "");
      row.innerHTML = `
        ${isNative ? `<span class="row-checkbox${checked ? " checked" : ""}">${checked ? ICON_CHECK : ""}</span>` : ""}
        <div class="thumb">
          <img src="${beer.labelUrl || DEFAULT_LABEL_URL}" alt="">
          ${beer.hadIt ? `<span class="had-it-corner">${ICON_CHECK}</span>` : ""}
          ${queueStatusCornerHtml(beer)}
        </div>
        <div class="result-main">
          <div class="result-name"><span class="result-name-text">${escapeHtml(beer.name || "")}</span>${ratingBadge(beer)}</div>
          ${metaLine(beer.brewery)}
          ${metaChips(beer)}
        </div>
        <div class="row-actions">
          ${isNative
            ? `<button class="wishlist-remove-btn" data-id="${beer.id}" data-beer-id="${beer.beerId}" data-name="${escapeHtml(beer.name || "")}">${ICON_CLOSE}</button>`
            : `<span class="wishlist-sheet-tag" title="${escapeHtml(T("app_wishlist_sheet_tag_title"))}">${escapeHtml(T("app_wishlist_sheet_tag"))}</span>`}
          <button class="untappd-link-btn" title="${escapeHtml(T("app_open_in_untappd"))}">${ICON_LINK}</button>
        </div>`;
      row.addEventListener("click", (e) => {
        if (wishlistSelectionMode) { if (isNative) toggleWishlistSelected(beer.id); return; }
        if (e.target.closest(".wishlist-remove-btn") || e.target.closest(".untappd-link-btn") || e.target.closest(".queue-status-corner")) return;
        selectBeer(beer, { origin: "wishlist" });
      });
      if (isNative) {
        bindLongPress(row, () => { if (!wishlistSelectionMode) enterWishlistSelectionMode(beer.id); });
      }
      row.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(beer.beerId, e));
      bindQueueStatusCorner(row);
      listEl.appendChild(row);
    });
    listEl.querySelectorAll(".wishlist-remove-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const doRemove = async () => {
          await apiPost("/api/checkin/wishlist/remove", { id: btn.dataset.id });
          syncSearchRowWishlistState(Number(btn.dataset.beerId), null);
          fetchWishlist();
        };
        // Same Telegram-native-confirm-with-browser-fallback pattern as
        // the queue's own "Очистити всю чергу?" - this one-tap "x" has no
        // selection step in front of it (unlike the bulk selection-delete
        // flow below), so it's the one most prone to an accidental tap.
        const msg = T("app_wishlist_remove_confirm", { name: btn.dataset.name });
        if (tg && tg.showConfirm) {
          tg.showConfirm(msg, (confirmed) => { if (confirmed) doRemove(); });
        } else if (confirm(msg)) {
          doRemove();
        }
      });
    });
  }

  $("wishlist-bar-btn").addEventListener("click", () => showScreen("wishlist"));
  $("wishlist-search-input").addEventListener("input", renderWishlistList);

  document.querySelectorAll("#wishlist-sort-pills .pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.wishlistSort = btn.dataset.sort;
      document.querySelectorAll("#wishlist-sort-pills .pill").forEach((p) => p.classList.toggle("active", p === btn));
      renderWishlistList();
    });
  });

  $("wishlist-selection-delete-btn").addEventListener("click", async () => {
    // Same reason the single-item remove handler above calls this: a
    // still-mounted search row (DOM persists across screens) needs to be
    // told this beer is no longer listed, or navigating back to search
    // keeps showing the stale "in list" state.
    const toDelete = state.wishlist.filter((b) => wishlistSelectedIds.has(b.id));
    for (const beer of toDelete) {
      await apiPost("/api/checkin/wishlist/remove", { id: beer.id });
      syncSearchRowWishlistState(beer.beerId, null);
    }
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    exitWishlistSelectionMode();
    fetchWishlist();
  });
  $("wishlist-selection-cancel-btn").addEventListener("click", exitWishlistSelectionMode);

  // ---- Pending check-ins ("Відкладені чекіни") ----
  // A check-in that failed (Untappd rate-limited, or any other error) is
  // saved server-side instead of just being lost (see webapp_server.py's
  // handle_submit) - this tab surfaces that list so the exact same rating/
  // comment/venue can be retried with one tap, without starting over. Tab
  // itself stays hidden (see updatePendingCheckinsCountOnly) whenever the
  // list is empty, same convention as the queue tab.

  async function fetchPendingCheckins() {
    $("pending-checkins-list").innerHTML = "";
    $("pending-checkins-status").textContent = T("app_loading");
    const { ok, data } = await apiPost("/api/checkin/pending/list", {});
    state.pendingCheckins = ok ? (data.items || []) : [];
    renderPendingCheckinsList();
  }

  async function updatePendingCheckinsCountOnly() {
    const { ok, data } = await apiPost("/api/checkin/pending/list", {});
    const items = ok ? (data.items || []) : [];
    $("pending-checkins-count").textContent = String(items.length);
    $("pending-checkins-bar-btn").classList.toggle("hidden", items.length === 0);
  }

  const PENDING_FAIL_REASON_KEY = {
    rate_limited: "app_pending_reason_rate_limited",
    checkin_failed: "app_pending_reason_checkin_failed",
  };

  function renderPendingCheckinsList() {
    const listEl = $("pending-checkins-list");
    listEl.innerHTML = "";
    $("pending-checkins-status").textContent = state.pendingCheckins.length
      ? "" : T("app_pending_empty");
    state.pendingCheckins.forEach((item) => {
      const row = document.createElement("div");
      row.className = "result-row";
      row.innerHTML = `
        <div class="thumb"><img src="${item.labelUrl || DEFAULT_LABEL_URL}" alt=""></div>
        <div class="result-main">
          <div class="result-name"><span class="result-name-text">${escapeHtml(item.beerName || "")}</span></div>
          ${metaLine(item.brewery)}
          <div class="hint">
            ${item.rating ? `★ ${item.rating}` : ""}${item.venueName ? ` · 📍 ${escapeHtml(item.venueName)}` : ""}
          </div>
          <div class="hint pending-fail-reason">${escapeHtml(T(PENDING_FAIL_REASON_KEY[item.failReason] || "app_pending_reason_default"))}</div>
        </div>
        <div class="row-actions pending-checkin-actions">
          <button class="pending-retry-btn" data-id="${item.id}">${ICON_REFRESH} ${escapeHtml(T("app_pending_retry"))}</button>
          <button class="wishlist-remove-btn" data-id="${item.id}" data-name="${escapeHtml(item.beerName || "")}">${ICON_CLOSE}</button>
        </div>`;
      row.querySelector(".pending-retry-btn").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = T("app_pending_retrying");
        const { ok, data } = await apiPost("/api/checkin/pending/retry", { id: item.id });
        if (ok && data.ok) {
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
          state.pendingCheckins = state.pendingCheckins.filter((p) => p.id !== item.id);
          renderPendingCheckinsList();
          updatePendingCheckinsCountOnly();
        } else {
          btn.disabled = false;
          btn.innerHTML = `${ICON_REFRESH} ${escapeHtml(T("app_pending_retry"))}`;
          row.querySelector(".pending-fail-reason").textContent = T("app_pending_retry_failed");
        }
      });
      row.querySelector(".wishlist-remove-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        const doRemove = async () => {
          await apiPost("/api/checkin/pending/remove", { id: item.id });
          state.pendingCheckins = state.pendingCheckins.filter((p) => p.id !== item.id);
          renderPendingCheckinsList();
          updatePendingCheckinsCountOnly();
        };
        const msg = T("app_pending_remove_confirm", { name: item.beerName });
        if (tg && tg.showConfirm) {
          tg.showConfirm(msg, (confirmed) => { if (confirmed) doRemove(); });
        } else if (confirm(msg)) {
          doRemove();
        }
      });
      listEl.appendChild(row);
    });
  }

  $("pending-checkins-bar-btn").addEventListener("click", () => showScreen("pending-checkins"));

  // ---- Festival progress screen ----
  // Personal, like the rest of the app's had-it-driven features - counts
  // only what *this viewer's* Untappd history (had_it_index, background-
  // synced) shows as tried, not a group-wide tally. Without a connected
  // account there's no personal history to draw on, so everything shows
  // as not-yet-tried - same degrade as the had-it badge in search.

  // Only the 4 original festival colors get a translated Ukrainian name -
  // any other raw session key (e.g. "friday"/"saturday" from a different
  // festival's JSON) falls back to showing the key itself, capitalized, via
  // sessionLabel() below. The backend no longer forces sessions into these
  // 4 buckets (a 5th+ session just reuses a color cosmetically), so this
  // list only ever needs entries for names worth localizing.
  const SESSION_NAME_KEYS = {
    yellow: "app_session_yellow", blue: "app_session_blue", red: "app_session_red", green: "app_session_green",
  };

  function sessionLabel(session) {
    if (SESSION_NAME_KEYS[session]) return T(SESSION_NAME_KEYS[session]);
    const name = String(session || "");
    return T("app_session_named", { name: `${name.charAt(0).toUpperCase()}${name.slice(1)}` });
  }

  $("stats-bar-btn").addEventListener("click", () => showScreen("stats"));

  async function fetchFestivalStats() {
    const { ok, data } = await apiPost("/api/checkin/festival/stats", {});
    if (!ok) {
      $("stats-summary").textContent = T("app_stats_load_failed");
      return;
    }
    const pct = data.total ? Math.round((100 * data.checked) / data.total) : 0;
    $("stats-summary").textContent = T("app_stats_summary", { checked: data.checked, total: data.total, pct });
    const listEl = $("stats-sessions");
    listEl.innerHTML = "";
    (data.sessions || []).forEach((s) => {
      const row = document.createElement("div");
      row.className = "venue-item";
      const sPct = s.total ? Math.round((100 * s.checked) / s.total) : 0;
      row.innerHTML = `
        <div>${sessionDot(s.color)} ${escapeHtml(sessionLabel(s.session))} — ${s.checked} / ${s.total} (${sPct}%)</div>
        <div class="stats-bar"><div class="stats-bar-fill" style="width:${sPct}%"></div></div>`;
      row.addEventListener("click", () => openSessionBeers(s.session, s.color));
      listEl.appendChild(row);
    });
  }

  // ---- Badge progress (real Untappd style/country badges, computed from
  // had_it_index's already-synced beer/style/country history - see
  // badge_stats.py) ----

  $("badges-bar-btn").addEventListener("click", () => showScreen("badges"));
  $("festival-map-bar-btn").addEventListener("click", () => showScreen("festival-map"));

  async function fetchBadgeStats() {
    $("badges-status").textContent = T("app_loading");
    const { ok, data } = await apiPost("/api/checkin/badges/get", {});
    if (!ok) {
      $("badges-status").textContent = T("app_badges_load_failed");
      return;
    }
    state.badgesRaw = data.badges || [];
    renderBadgesList();
    // Restores whatever position showScreen saved on the way out (see
    // badgesListScrollY) - 0 on a genuinely first visit, a no-op scroll.
    window.scrollTo(0, badgesListScrollY);
  }

  // Untappd's own time-limited promotional badges (special_badges.json,
  // hand-curated from untappd.com/blog - see badge_stats.compute_special_badges'
  // own docstring) - a short list of "currently earnable" cards above the
  // regular ~130-badge grid, not merged into it: these aren't in the
  // permanent catalog those rows come from, and (unlike every other badge
  // here) this app has no way to confirm one's actually been earned, only
  // to suggest what would count - a fundamentally different kind of row,
  // so it gets its own small section instead of pretending to be one more
  // entry in the sortable/filterable list.
  let specialBadgesRaw = [];
  // Collapsed by default (the full style list can run to 30+ names - see
  // the Sour Beer Day catalog entry - which swamped the screen before this
  // existed). Transient, not persisted: which cards are expanded doesn't
  // need to survive a fresh fetch or outlive the screen, same as
  // badgesCollapsedKinds above.
  const specialBadgesExpanded = new Set();

  async function fetchSpecialBadges() {
    const { ok, data } = await apiPost("/api/checkin/special_badges/get", {});
    specialBadgesRaw = ok ? (data.badges || []) : [];
    renderSpecialBadges();
  }

  function renderSpecialBadges() {
    const el = $("special-badges-list");
    if (!specialBadgesRaw.length) { el.innerHTML = ""; return; }
    el.innerHTML = specialBadgesRaw.map((b) => {
      const expanded = specialBadgesExpanded.has(b.badge);
      const urgent = b.daysRemaining <= 0;
      const daysText = urgent
        ? T("app_badge_last_day")
        : TP("app_badge_days_left", b.daysRemaining);
      const iconHtml = b.icon
        ? `<img class="special-badge-icon" src="${b.icon}" alt="">`
        : `<svg class="icon special-badge-icon-fallback"><use href="#icon-sparkle"/></svg>`;
      let detailsHtml = "";
      if (expanded) {
        const criteriaLabel = T(b.kind === "style" ? "app_badge_criteria_styles" : "app_badge_criteria_countries");
        const criteriaValue = b.kind === "style"
          ? b.styles.map(escapeHtml).join(", ")
          : (b.countries || []).map(escapeHtml).join(", ");
        const chainNote = b.venueChain
          ? `<div class="special-badge-chain"><svg class="icon"><use href="#icon-pin"/></svg>${escapeHtml(T("app_badge_chain_only", { chain: b.venueChain }))}</div>`
          : "";
        const examples = b.matchingKnownBeers.length
          ? `<div class="special-badge-examples"><svg class="icon"><use href="#icon-sparkle"/></svg>${escapeHtml(T("app_badge_examples"))} ${b.matchingKnownBeers.slice(0, 3).map((m) => escapeHtml(m.name)).join(", ")}</div>`
          : "";
        detailsHtml = `
          <div class="special-badge-details">
            <div class="special-badge-criteria"><span class="special-badge-criteria-label">${criteriaLabel}:</span> ${criteriaValue}</div>
            ${chainNote}
            ${examples}
            <div class="special-badge-footer">
              <a href="${b.sourceUrl}" target="_blank" rel="noopener" class="special-badge-link">${escapeHtml(T("app_source"))} <svg class="icon"><use href="#icon-external-link"/></svg></a>
              <button class="special-badge-dismiss-btn" data-badge="${escapeHtml(b.badge)}">${escapeHtml(T("app_badge_got_it"))}</button>
            </div>
          </div>`;
      }
      return `
        <div class="special-badge-card">
          <div class="special-badge-header" data-badge="${escapeHtml(b.badge)}">
            ${iconHtml}
            <div class="special-badge-header-text">
              <div class="special-badge-title">${escapeHtml(b.badge)}</div>
              <div class="special-badge-days${urgent ? " urgent" : ""}">${daysText}</div>
            </div>
            <svg class="icon special-badge-chevron ${expanded ? "expanded" : ""}"><use href="#icon-chevron-right"/></svg>
          </div>
          ${detailsHtml}
        </div>`;
    }).join("");

    el.querySelectorAll(".special-badge-header").forEach((header) => {
      header.addEventListener("click", () => {
        const name = header.dataset.badge;
        if (specialBadgesExpanded.has(name)) specialBadgesExpanded.delete(name);
        else specialBadgesExpanded.add(name);
        renderSpecialBadges();
      });
    });
    el.querySelectorAll(".special-badge-dismiss-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const name = btn.dataset.badge;
        await apiPost("/api/checkin/special_badges/dismiss", { badge: name });
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        specialBadgesRaw = specialBadgesRaw.filter((b) => b.badge !== name);
        renderSpecialBadges();
      });
    });
  }

  // "За типом" groups by the same `kind` compute_progress/compute_*_progress
  // already tags each row with (see badge_stats.py's _row) - style/venue
  // first (the two concrete, easy-to-picture kinds), "Різне" (the abstract
  // distinct-count badges like Wheel of Styles/Brewery Pioneer) last per
  // explicit user feedback on the first cut of this feature.
  const BADGE_KIND_ORDER = ["style", "venue", "country", "range", "distinct"];
  const BADGE_KIND_GROUP_LABEL_KEY = {
    style: "app_badge_group_style", country: "app_badge_group_country", distinct: "app_badge_group_distinct",
    range: "app_badge_group_range", venue: "app_badge_group_venue",
  };
  // Per-kind collapse state for the "За типом" grouping - a plain Set (not
  // state.*, this is transient view state that doesn't need to survive a
  // fresh badges fetch or outlive the screen) - collapsing a group hides its
  // cards but always keeps the header (with a count) visible.
  const badgesCollapsedKinds = new Set();

  // Sorts/filters the already-fetched list client-side - a fresh fetch per
  // toggle would be pointless round-tripping for a fixed ~170-row list that
  // doesn't change mid-session.
  function sortedBadges() {
    const list = state.badgesRaw.slice();
    if (state.badgesSort === "closest") {
      // Closest to the next level first (pct = progress within the current
      // level); maxed-out badges have no next level, so they go last.
      list.sort((a, b) => (a.done - b.done) || (b.pct - a.pct)
        || ((a.nextThreshold - a.current) - (b.nextThreshold - b.current)));
    } else if (state.badgesSort === "alpha") {
      list.sort((a, b) => a.name.localeCompare(b.name));
    } else if (state.badgesSort === "kind") {
      list.sort((a, b) => (BADGE_KIND_ORDER.indexOf(a.kind) - BADGE_KIND_ORDER.indexOf(b.kind))
        || b.level - a.level || b.pct - a.pct);
    } else {
      list.sort((a, b) => b.level - a.level || b.pct - a.pct);
    }
    return list;
  }

  // Matches the search box against the badge name AND its underlying tags
  // (styles/countries/venue categories) - e.g. "France" or "IPA" find every
  // badge that actually cares about that tag, not just ones with it in the
  // display name (most badges have a stylized name that says nothing about
  // what earns it - "Zwickel City" doesn't mention "Kellerbier" at all).
  function badgeMatchesQuery(b, query) {
    if (!query) return true;
    if ((b.name || "").toLowerCase().includes(query)) return true;
    return (b.tags || []).some((t) => t.toLowerCase().includes(query));
  }

  // "N у процесі · M майже готові" under the title - over the whole list,
  // not the current search filter. "Майже готові" = 80%+ of the way to the
  // next level, same idea as the "Майже готові" sort.
  const BADGE_CLOSE_PCT = 80;
  function renderBadgesSubtitle() {
    const open = state.badgesRaw.filter((b) => !b.done);
    const close = open.filter((b) => b.pct >= BADGE_CLOSE_PCT).length;
    $("badges-subtitle").textContent = state.badgesRaw.length
      ? `${T("app_badges_in_progress", { n: open.length })} · ${TP("app_badges_almost", close)}`
      : "";
  }

  function renderBadgesList() {
    renderBadgesSubtitle();
    const query = $("badges-search-input").value.trim().toLowerCase();
    const badges = sortedBadges().filter((b) => badgeMatchesQuery(b, query));
    if (!state.badgesRaw.length) {
      $("badges-status").textContent = T("app_badges_accumulating");
    } else {
      $("badges-status").textContent = badges.length ? "" : T("app_nothing_found");
    }
    const listEl = $("badges-list");
    listEl.innerHTML = "";
    const grid = state.badgesView === "grid";
    listEl.classList.toggle("badges-grid", grid);
    const grouped = state.badgesSort === "kind";
    let lastKind = null;
    let groupCollapsed = false;
    badges.forEach((b) => {
      if (grouped && b.kind !== lastKind) {
        lastKind = b.kind;
        groupCollapsed = badgesCollapsedKinds.has(b.kind);
        const count = badges.filter((x) => x.kind === b.kind).length;
        const header = document.createElement("div");
        header.className = "badges-group-header" + (groupCollapsed ? " collapsed" : "");
        header.innerHTML =
          `${escapeHtml(BADGE_KIND_GROUP_LABEL_KEY[b.kind] ? T(BADGE_KIND_GROUP_LABEL_KEY[b.kind]) : b.kind)} ` +
          `<span class="badges-group-header-count">(${count})</span>` +
          `<svg class="icon"><use href="#icon-chevron-right"/></svg>`;
        header.addEventListener("click", () => {
          if (badgesCollapsedKinds.has(b.kind)) badgesCollapsedKinds.delete(b.kind);
          else badgesCollapsedKinds.add(b.kind);
          renderBadgesList();
        });
        listEl.appendChild(header);
      }
      if (grouped && groupCollapsed) return;
      const target = b.nextThreshold ?? b.current;
      const pct = Math.max(0, Math.min(100, b.pct));
      const row = document.createElement("div");
      if (grid) {
        // Progress ring around the badge icon: r=28 in a 64-unit box, so
        // the dash length is pct% of the circumference.
        const circ = 2 * Math.PI * 28;
        row.className = "badge-card" + (b.done ? " done" : "");
        row.innerHTML = `
          <div class="badge-card-ring">
            <svg viewBox="0 0 64 64" aria-hidden="true">
              <circle cx="32" cy="32" r="28" class="badge-card-ring-track"/>
              <circle cx="32" cy="32" r="28" class="badge-card-ring-fill"
                      stroke-dasharray="${(circ * pct / 100).toFixed(1)} ${circ.toFixed(1)}"/>
            </svg>
            <img src="${b.icon || DEFAULT_LABEL_URL}" alt="">
          </div>
          <div class="badge-card-title">${b.done ? doneMark() : ""}${escapeHtml(b.name || "")}</div>
          ${b.level ? `<div class="badge-card-level">${escapeHtml(T("app_badge_level", { n: b.level }))}</div>` : ""}
          <div class="badge-card-count"><span>${b.current}</span> / ${target}</div>`;
      } else {
        row.className = "venue-item badge-row" + (b.done ? " done" : "");
        row.innerHTML = `
          <img class="badge-row-icon" src="${b.icon || DEFAULT_LABEL_URL}" alt="">
          <div class="badge-row-main">
            <div class="badge-row-title">${b.done ? doneMark() : ""}${escapeHtml(b.name || "")}</div>
            <div class="badge-row-progress">${b.current} / ${target}${b.level ? " · " + escapeHtml(T("app_badge_level", { n: b.level })) : ""}</div>
            <div class="stats-bar"><div class="stats-bar-fill" style="width:${pct}%"></div></div>
          </div>`;
      }
      row.addEventListener("click", () => openBadgeDetail(b));
      listEl.appendChild(row);
    });
  }

  // Grid/list view for the badges screen - a per-device display preference,
  // so localStorage (wrapped: private mode / blocked storage just falls back
  // to the grid default each time).
  const BADGES_VIEW_KEY = "checkin.badgesView";
  try { if (localStorage.getItem(BADGES_VIEW_KEY) === "list") state.badgesView = "list"; } catch (e) { /* ignore */ }

  function syncBadgesViewToggle() {
    const grid = state.badgesView === "grid";
    // The button shows the view you'd switch TO, not the current one.
    $("badges-view-toggle-icon").querySelector("use").setAttribute("href", grid ? "#icon-list" : "#icon-grid");
    $("badges-view-toggle").setAttribute("aria-label", T(grid ? "app_badges_view_list" : "app_badges_view_grid"));
  }
  syncBadgesViewToggle();

  $("badges-view-toggle").addEventListener("click", () => {
    state.badgesView = state.badgesView === "grid" ? "list" : "grid";
    try { localStorage.setItem(BADGES_VIEW_KEY, state.badgesView); } catch (e) { /* ignore */ }
    syncBadgesViewToggle();
    renderBadgesList();
  });

  $("badges-search-input").addEventListener("input", renderBadgesList);

  document.querySelectorAll("#badges-sort-pills .pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.badgesSort = btn.dataset.sort;
      document.querySelectorAll("#badges-sort-pills .pill").forEach((p) => p.classList.toggle("active", p === btn));
      renderBadgesList();
    });
  });

  // ---- Badge detail drill-down ----

  const BADGE_KIND_LABEL_KEY = {
    style: "app_badge_kind_style", country: "app_badge_kind_country", venue: "app_badge_kind_venue",
    distinct: "app_badge_kind_distinct", range: "app_badge_kind_range",
  };

  function openBadgeDetail(b) {
    state.selectedBadge = b;
    showScreen("badge-detail");
  }

  function renderBadgeDetail() {
    const b = state.selectedBadge;
    if (!b) return;
    const target = b.nextThreshold ?? b.current;
    const pct = Math.max(0, Math.min(100, b.pct));
    $("badge-detail-icon").src = b.icon || DEFAULT_LABEL_URL;
    $("badge-detail-name").innerHTML = (b.done ? doneMark() : "") + escapeHtml(b.name || "");
    $("badge-detail-progress").textContent =
      `${b.current} / ${target}${b.level ? " · " + T("app_badge_level", { n: b.level }) : ""}`;
    $("badge-detail-bar").style.width = pct + "%";
    const kindLabel = T(BADGE_KIND_LABEL_KEY[b.kind] || "app_badge_kind_tag");
    $("badge-detail-howto").textContent = b.done
      ? T("app_badge_max_reached", { n: b.current })
      : T("app_badge_level_up", { need: target - b.current, per: b.countPerLevel, kind: kindLabel });
    const tagsEl = $("badge-detail-tags");
    tagsEl.innerHTML = "";
    (b.tags || []).forEach((tag) => {
      const el = document.createElement("span");
      el.className = "badge-tag";
      el.textContent = tag;
      // Only style tags open the BJCP info screen - a country/venue-
      // category tag (b.kind) isn't a beer style, nothing to look up.
      if (b.kind === "style") {
        el.classList.add("clickable");
        el.addEventListener("click", () => openStyleInfo(tag));
      }
      tagsEl.appendChild(el);
    });
    // personalUrl (untappd.com/user/{username}/badges/{user_badge_id}) is
    // the REAL per-earned-instance page Untappd itself generates when you
    // share a badge - confirmed live to work, and on the untappd.com domain
    // that's known to hand off to the native app. Only known once this
    // exact badge has actually shown up in the venue backfill's already-
    // fetched check-in history (see badge_index.py) - falls back to the
    // generic badges.untappd.com catalog page (b.url) otherwise. The
    // earlier untappd://badge/{id} scheme attempt is gone - unconfirmed to
    // even exist, and produced a visible error instead of silently no-op'ing.
    const openUrl = b.personalUrl || b.url;
    const untappdBtn = $("badge-detail-untappd-btn");
    if (openUrl) {
      untappdBtn.classList.remove("hidden");
      untappdBtn.onclick = () => openExternalLink(openUrl);
    } else {
      untappdBtn.classList.add("hidden");
    }
    // personalUrlStaleLevel (webapp_server.py's handle_badges_get) - the
    // link above is real but a FROZEN snapshot of an older award moment,
    // offered anyway once there's nothing left to passively discover (see
    // that handler's own comment) - this caption is what keeps it from
    // reading as "your current level" when the page itself shows a lower
    // number than what's already displayed above.
    const staleHint = $("badge-detail-stale-hint");
    if (b.personalUrlStaleLevel != null) {
      staleHint.textContent = T("app_badge_stale_hint", { level: b.personalUrlStaleLevel });
      staleHint.classList.remove("hidden");
    } else {
      staleHint.classList.add("hidden");
    }
  }

  // ---- Style info (BJCP description for a badge-detail style tag) ----
  // Untappd has no style-description page/API of its own (confirmed -
  // see bjcp_styles.py's own docstring) - BJCP's real 2021 guidelines are
  // the only real source, and only cover official styles, so a modern/
  // informal Untappd-only term (Pastry, Milkshake, Smoothie, ...) often
  // has no confident match at all - handled as its own graceful state
  // below, not an error.

  function openStyleInfo(style) {
    state.selectedStyle = style;
    showScreen("style-info");
  }

  async function renderStyleInfo() {
    const style = state.selectedStyle;
    if (!style) return;
    $("style-info-title").textContent = style;
    $("style-info-status").textContent = T("app_loading");
    $("style-info-body").innerHTML = "";
    const bjcpLink = $("style-info-bjcp-link");
    bjcpLink.classList.add("hidden");

    const { ok, data } = await apiPost("/api/checkin/style_info", { style });
    if (!ok) {
      $("style-info-status").textContent = T("app_style_load_failed");
      return;
    }

    if (!data.matched) {
      $("style-info-status").textContent =
        T("app_style_no_bjcp");
      $("style-info-bjcp-link-text").textContent = T("app_bjcp_guidelines_link");
      bjcpLink.onclick = () => openExternalLink(data.guideUrl);
      bjcpLink.classList.remove("hidden");
      return;
    }

    // approximate: true covers two different DISTINCT cases from
    // bjcp_styles.py, both meaning "not a literal named-style identification"
    // but for different reasons - data.note (when present) explains which:
    // either a hand-curated editorial opinion for a style BJCP has no
    // category for at all (e.g. "Stout - Pastry" -> Sweet Stout, no note),
    // or an actual BJCP classification RULE ("<style> - Fruited" always
    // falls under Fruit Beer by BJCP's own stated definition - note
    // present). Neither should be worded as if BJCP named that exact tag.
    if (data.approximate) {
      $("style-info-status").textContent = data.note
        ? `${data.note} ${data.styleId ? data.styleId + ". " : ""}${data.name}`
        : T("app_style_approx", { style: `${data.styleId ? data.styleId + ". " : ""}${data.name}` });
    } else {
      $("style-info-status").textContent = T("app_style_closest", { style: `${data.styleId ? data.styleId + ". " : ""}${data.name}` });
    }
    const sections = [
      [T("app_style_overall"), data.overallImpression],
      [T("app_style_aroma"), data.aroma],
      [T("app_style_appearance"), data.appearance],
      [T("app_style_flavor"), data.flavor],
      [T("app_style_mouthfeel"), data.mouthfeel],
    ];
    let bodyHtml = sections
      .filter(([, text]) => text)
      .map(([label, text]) => `<div class="style-info-section"><h3>${escapeHtml(label)}</h3><p>${escapeHtml(text)}</p></div>`)
      .join("");
    // alternates: other BJCP entries tied for the same best-effort score
    // as the primary one (see bjcp_styles.py's find_style) - a genuine
    // ambiguity, not a single confident answer, so offered as extra
    // links rather than silently hidden.
    if (data.alternates && data.alternates.length) {
      bodyHtml += `<div class="style-info-section style-info-alternates">
        <h3>${escapeHtml(T("app_style_also_fits"))}</h3>
        ${data.alternates
          .map(
            (a) =>
              `<button class="secondary-btn style-info-alt-btn" data-url="${escapeHtml(a.url || "")}">${escapeHtml(a.styleId ? a.styleId + ". " : "")}${escapeHtml(a.name || "")}</button>`
          )
          .join("")}
      </div>`;
    }
    $("style-info-body").innerHTML = bodyHtml;
    $("style-info-body").querySelectorAll(".style-info-alt-btn").forEach((btn) => {
      btn.addEventListener("click", () => openExternalLink(btn.dataset.url));
    });
    $("style-info-bjcp-link-text").textContent = T("app_bjcp_open");
    bjcpLink.onclick = () => openExternalLink(data.url);
    bjcpLink.classList.remove("hidden");
  }

  // ---- Session drill-down (list of beers in one session, with search) ----

  function openSessionBeers(session, color) {
    state.currentSession = session;
    $("session-beers-title").innerHTML = `${sessionDot(color)} ${escapeHtml(sessionLabel(session))}`;
    $("session-search-input").value = "";
    showScreen("session-beers");
  }

  async function fetchSessionBeers() {
    if (!state.currentSession) return;
    const query = $("session-search-input").value.trim();
    // Cleared BEFORE the await (see fetchWishlist's own note) - otherwise
    // the PREVIOUS session's (or a stale search's) rows stay visible for
    // the whole round-trip instead of a loading state.
    $("session-beers-list").innerHTML = "";
    $("session-beers-status").textContent = T("app_loading");
    const { ok, data } = await apiPost("/api/checkin/festival/session", {
      session: state.currentSession, query,
    });
    if (!ok) {
      $("session-beers-status").textContent = T("app_list_load_failed");
      return;
    }
    const beers = data.beers || [];
    $("session-beers-status").textContent = beers.length ? "" : T("app_nothing_found");
    const listEl = $("session-beers-list");
    beers.forEach((b) => {
      const row = document.createElement("div");
      row.className = "result-row" + (b.hadIt ? " had-it" : "");
      row.innerHTML = `
        <div class="thumb">
          <img src="${DEFAULT_LABEL_URL}" alt="">
          ${b.hadIt ? `<span class="had-it-corner">${ICON_CHECK}</span>` : ""}
          ${queueStatusCornerHtml(b)}
        </div>
        <div class="result-main">
          <div class="result-name"><span class="result-name-text">${escapeHtml(b.name || "")}</span>${ratingBadge(b)}</div>
          ${metaLine(b.brewery)}
          ${metaChips(b)}
        </div>
        <div class="row-actions">
          <button class="untappd-link-btn" title="${escapeHtml(T("app_open_in_untappd"))}">${ICON_LINK}</button>
        </div>`;
      row.addEventListener("click", (e) => {
        if (e.target.closest(".untappd-link-btn") || e.target.closest(".queue-status-corner")) return;
        selectBeer(b, { origin: "session-beers" });
      });
      row.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(b.beerId, e));
      bindQueueStatusCorner(row);
      listEl.appendChild(row);
    });
  }

  let sessionSearchDebounce = null;
  $("session-search-input").addEventListener("input", () => {
    clearTimeout(sessionSearchDebounce);
    sessionSearchDebounce = setTimeout(fetchSessionBeers, 350);
  });

  // ---- Brewery drill-down (opened from a festival map pill) - beers
  // grouped by session, each group showing a tried/total line ----

  let currentBreweryBeers = null;

  function openBreweryBeers(brewery) {
    currentBreweryBeers = brewery;
    $("brewery-beers-title").textContent = brewery;
    showScreen("brewery-beers");
  }

  async function fetchBreweryBeers() {
    if (!currentBreweryBeers) return;
    // Cleared BEFORE the await (see fetchWishlist's own note) - otherwise
    // the PREVIOUS brewery's rows stay visible for the whole round-trip.
    $("brewery-beers-list").innerHTML = "";
    $("brewery-beers-status").textContent = T("app_loading");
    const { ok, data } = await apiPost("/api/checkin/festival/brewery", { brewery: currentBreweryBeers });
    if (!ok) {
      $("brewery-beers-status").textContent = T("app_list_load_failed");
      return;
    }
    const beers = data.beers || [];
    $("brewery-beers-status").textContent = beers.length ? "" : T("app_breweries_no_beers");
    renderBreweryBeers(beers);
  }

  function breweryBeerRow(b) {
    const row = document.createElement("div");
    row.className = "result-row" + (b.hadIt ? " had-it" : "");
    row.innerHTML = `
      <div class="thumb">
        <img src="${DEFAULT_LABEL_URL}" alt="">
        ${b.hadIt ? `<span class="had-it-corner">${ICON_CHECK}</span>` : ""}
        ${queueStatusCornerHtml(b)}
      </div>
      <div class="result-main">
        <div class="result-name"><span class="result-name-text">${escapeHtml(b.name || "")}</span>${ratingBadge(b)}</div>
        ${metaChips(b)}
      </div>
      <div class="row-actions">
        ${PUBLIC_MODE ? "" : `<button class="add-queue-btn" title="${escapeHtml(T("app_add_to_queue_title"))}">+</button>`}
        <button class="untappd-link-btn" title="${escapeHtml(T("app_open_in_untappd"))}">${ICON_LINK}</button>
      </div>`;
    row.addEventListener("click", (e) => {
      if (e.target.closest(".add-queue-btn") || e.target.closest(".untappd-link-btn") || e.target.closest(".queue-status-corner")) return;
      if (PUBLIC_MODE) { openUntappdBeer(b.beerId); return; }
      selectBeer(b, { origin: "brewery-beers" });
    });
    const addBtn = row.querySelector(".add-queue-btn");
    if (addBtn) {
      addBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        addToQueue(b, addBtn);
      });
    }
    row.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(b.beerId, e));
    bindQueueStatusCorner(row);
    return row;
  }

  // A beer poured across multiple sessions appears under each one it
  // belongs to (see _beer_sessions' own docstring in webapp_server.py) -
  // intentional here too, since a session's tried/total line should count
  // every beer that's actually part of it.
  function renderBreweryBeers(beers) {
    const listEl = $("brewery-beers-list");
    listEl.innerHTML = "";
    const groups = new Map();
    beers.forEach((b) => {
      const sessions = b.sessions && b.sessions.length ? b.sessions : [null];
      sessions.forEach((session) => {
        if (!groups.has(session)) groups.set(session, []);
        groups.get(session).push(b);
      });
    });
    groups.forEach((groupBeers, session) => {
      const color = sessionColorMap[session] || session;
      const header = document.createElement("div");
      header.className = "brewery-session-header";
      if (session) header.innerHTML = `${sessionDot(color)} ${escapeHtml(sessionLabel(session))}`;
      else header.textContent = T("app_group_other");
      listEl.appendChild(header);
      const tried = groupBeers.filter((b) => b.hadIt).length;
      const stats = document.createElement("div");
      stats.className = "hint brewery-session-stats";
      stats.textContent = T("app_tried_of", { tried, total: groupBeers.length });
      listEl.appendChild(stats);
      groupBeers.forEach((b) => listEl.appendChild(breweryBeerRow(b)));
    });
  }

  // ---- Search screen ----

  // Festival and wishlist priority are independent toggles - either can be
  // on alone (wishlist first if festival is off) or both (festival first,
  // then wishlist).
  const festivalPriorityCheckbox = $("festival-priority-checkbox");
  const wishlistPriorityCheckbox = $("wishlist-priority-checkbox");
  function rerunSearchIfActive() {
    if ($("search-input").value.trim().length >= 2) runSearch($("search-input").value.trim());
  }
  festivalPriorityCheckbox.addEventListener("change", rerunSearchIfActive);
  wishlistPriorityCheckbox.addEventListener("change", rerunSearchIfActive);

  // Plain hint text (loading/errors/empty) vs runSearch's two-part
  // "Результати · N знайдено" heading, which swaps in its own class.
  function setSearchStatus(text) {
    const el = $("search-status");
    el.classList.remove("results-heading");
    el.textContent = text;
  }

  let searchDebounce = null;
  $("search-input").addEventListener("input", (e) => {
    const q = e.target.value.trim();
    clearTimeout(searchDebounce);
    if (q.length < 2) {
      $("results").innerHTML = "";
      setSearchStatus("");
      $("home-hero").classList.remove("hidden");
      return;
    }
    $("home-hero").classList.add("hidden");
    setSearchStatus(T("app_searching"));
    searchDebounce = setTimeout(() => runSearch(q), 350);
  });

  // Toggling a filter checkbox fires a fresh (non-debounced) search while a
  // debounced typed one may still be in flight - with no ordering guarantee
  // on which response lands first, rendering unconditionally could show
  // stale results for a query the user has already changed. A monotonic
  // request id, checked when the response lands, discards any response
  // that isn't from the most recent call.
  let searchRequestId = 0;

  async function runSearch(query) {
    const requestId = ++searchRequestId;
    const festivalPriority = festivalPriorityCheckbox.checked;
    const wishlistPriority = wishlistPriorityCheckbox.checked;
    const { ok, status, data } = await apiPost("/api/checkin/search", { query, festivalPriority, wishlistPriority });
    if (requestId !== searchRequestId) return; // superseded by a newer search - discard
    if (!ok) {
      setSearchStatus(status === 429
        ? T("app_rate_limited_retry")
        : T("app_search_error"));
      return;
    }
    const beers = data.beers || [];
    if (beers.length) {
      const el = $("search-status");
      el.classList.add("results-heading");
      el.innerHTML = `<span class="results-heading-label">${escapeHtml(T("app_results"))}</span><span>${escapeHtml(T("app_found_count", { n: beers.length }))}</span>`;
    } else {
      setSearchStatus(T("app_nothing_found"));
    }
    closeAllRowMenus(); // about to remove whatever row it was anchored to
    $("results").innerHTML = "";
    beers.forEach((b) => {
      const row = document.createElement("div");
      row.className = "result-row" + (b.hadIt ? " had-it" : "");
      row.innerHTML = `
        <div class="thumb">
          <img src="${b.labelUrl || DEFAULT_LABEL_URL}" alt="">
          ${b.hadIt ? `<span class="had-it-corner">${ICON_CHECK}</span>` : ""}
          ${queueStatusCornerHtml(b)}
        </div>
        <div class="result-main">
          <div class="result-name">${sourceBadge(b)}<span class="result-name-text">${escapeHtml(b.name || "")}</span>${ratingBadge(b)}</div>
          ${metaLine(b.brewery)}
          ${metaChips(b)}
        </div>
        <div class="row-actions" data-beer-id="${b.beerId}" data-wishlist-item-id="${b.wishlistItemId || ""}">
          <button class="add-queue-btn" title="${escapeHtml(T("app_add_to_queue_title"))}">+</button>
          <button class="row-menu-btn${b.wishlistItemId ? " has-wishlist-item" : ""}" title="${escapeHtml(T("app_row_more_title"))}">⋯</button>
        </div>`;
      const actionsEl = row.querySelector(".row-actions");
      row.addEventListener("click", (e) => {
        if (e.target.closest(".add-queue-btn") || e.target.closest(".row-menu-btn") || e.target.closest(".queue-status-corner")) return;
        selectBeer(b, { origin: "search" });
      });
      const addBtn = row.querySelector(".add-queue-btn");
      addBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        addToQueue(b, addBtn);
      });
      row.querySelector(".row-menu-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        openRowMenu(b, actionsEl);
      });
      bindQueueStatusCorner(row);
      $("results").appendChild(row);
    });
  }

  function ratingBadge(b) {
    if (!b.hadIt || typeof b.userRating !== "number") return "";
    return ` <span class="badge had-it-badge" title="${escapeHtml(T("app_your_rating"))}">${ICON_STAR}${b.userRating.toFixed(2)}</span>`;
  }

  // Session color -> a small CSS dot (see style.css's .session-dot); an
  // unknown/non-color session gets the neutral grey dot.
  const SESSION_DOT_COLORS = { yellow: "#f5c451", blue: "#3478f6", red: "#e5484d", green: "#2fbf7a" };
  function sessionDot(color) {
    const c = SESSION_DOT_COLORS[color];
    return `<span class="session-dot"${c ? ` style="background:${c}"` : ""}></span>`;
  }
  // raw session key -> color, filled in once from /api/checkin/festival/meta
  // (see the bottom of this file) - covers non-color session names.
  const sessionColorMap = {};

  function sourceBadge(b) {
    if (b.source === "festival") {
      const sessions = b.sessions && b.sessions.length ? b.sessions : [null];
      const dots = sessions.map((s) => sessionDot(sessionColorMap[s] || s)).join("");
      return `<span class="badge session-dots">${dots}</span> `;
    }
    if (b.source === "wishlist") {
      return `<span class="badge wishlist-mark" title="${escapeHtml(T("app_from_wishlist"))}">${ICON_HEART}</span> `;
    }
    return "";
  }

  // Folds accented Latin letters to their plain ASCII base (ą->a, ć->c,
  // ń->n, ó->o, ś->s, ź/ż->z, ā->a, etc.) - same technique and same "ł"/"Ł"
  // special case as beer_match.py's own _fold_diacritics (Unicode NFKD
  // decomposes most accented letters into a base letter + a separate
  // combining mark, which is then dropped; "ł"/"Ł" has no such
  // decomposition, so it's substituted by hand first). Used to make the
  // festival-map brewery search diacritic-insensitive in general, instead
  // of hand-maintaining a plain-ASCII display alias for every brewery whose
  // real name happens to have special characters.
  function foldDiacritics(text) {
    return (text || "")
      .replace(/ł/g, "l").replace(/Ł/g, "L")
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // Joins non-empty parts with " · ", skipping missing ones (festival beers
  // often have no style/ABV in the source data) - a plain fixed template
  // left dangling "· ·" separators when a field was blank.
  function joinMeta(...parts) {
    return parts.filter((p) => p != null && p !== "").map(escapeHtml).join(" · ");
  }

  // Brewery and style/ABV each get their own line (rather than one shared,
  // truncatable line) - both are prone to being long enough to need the
  // whole row width to themselves, and used to hide each other behind a
  // single ellipsis. Renders nothing (not even an empty line) when there's
  // no non-empty part to show.
  function metaLine(...parts) {
    const text = joinMeta(...parts);
    return text ? `<div class="result-meta">${text}</div>` : "";
  }

  // Global rating / style / ABV as separate chips under the brewery line.
  // The rating chip only appears when the server actually has one - only
  // Untappd-sourced results carry it (festival/wishlist rows come back with
  // rating: null, and fetching it per beer would burn API quota). Renders
  // nothing when all three are missing.
  function metaChips(b) {
    const chips = [];
    if (typeof b.rating === "number" && b.rating > 0) {
      chips.push(`<span class="meta-chip meta-chip-rating">${ICON_STAR}${b.rating.toFixed(2)}</span>`);
    }
    if (b.style) chips.push(`<span class="meta-chip meta-chip-style">${escapeHtml(b.style)}</span>`);
    if (b.abv != null && b.abv !== "") chips.push(`<span class="meta-chip">${escapeHtml(b.abv + "%")}</span>`);
    return chips.length ? `<div class="meta-chips">${chips.join("")}</div>` : "";
  }

  // ---- Rate screen ----

  function setSelectedVenueDisplay(text) {
    const el = $("venue-selected");
    el.innerHTML = text ? `${ICON_PIN} ${escapeHtml(text)}` : "";
    el.classList.toggle("hidden", !text);
  }

  function selectBeer(beer, { origin = "search", queueItemId = null } = {}) {
    state.selectedBeer = beer;
    state.origin = origin;
    state.queueItemId = queueItemId;
    // Pre-fill with the rating from a previous check-in of this same beer,
    // if we know one (hadIt + a real userRating) - saves re-entering the
    // same score for a beer someone's rating a second time.
    const prevRating = (beer.hadIt && typeof beer.userRating === "number" && beer.userRating > 0)
      ? beer.userRating
      : 4;
    state.rating = prevRating;
    state.selectedVenue = state.lastVenue;
    $("rate-beer-card").innerHTML = `
      <div class="name">${escapeHtml(beer.name || "")}</div>
      <div class="meta">${joinMeta(beer.brewery, beer.style, beer.abv != null ? beer.abv + "%" : null)}</div>`;
    $("rating-slider").value = String(prevRating);
    $("rating-readout").textContent = prevRating.toFixed(2);
    $("shout-input").value = "";
    $("venue-list").classList.add("hidden");
    setSelectedVenueDisplay(state.lastVenue ? (state.lastVenue.name || "") : "");
    updatePillHighlight();
    // Reset the submit button back to its default state - without this,
    // whatever it last showed (Зачекінено!/Збережено/a disabled "Надсилаю…")
    // from a PREVIOUS check-in attempt stuck around on every later beer
    // opened via this same screen, since nothing else ever touches it
    // until the next submit.
    const confirmBtn = $("to-confirm-btn");
    confirmBtn.disabled = false;
    confirmBtn.innerHTML = `${ICON_CHECK} ${escapeHtml(T("app_checkin_btn"))}`;
    $("submit-status").textContent = "";
    showScreen("rate");
  }

  function screenForOrigin(origin) {
    if (origin === "queue") return "queue";
    if (origin === "session-beers") return "session-beers";
    if (origin === "brewery-beers") return "brewery-beers";
    if (origin === "wishlist") return "wishlist";
    return "search";
  }

  $("rate-back-btn").addEventListener("click", () => {
    showScreen(screenForOrigin(state.origin));
  });

  const PILL_VALUES = [3.75, 4, 4.25, 4.5, 4.75, 5];
  const pillsEl = $("rating-pills");
  PILL_VALUES.forEach((v) => {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.textContent = v.toFixed(2).replace(/0$/, "").replace(/\.$/, "");
    pill.dataset.value = v;
    pill.addEventListener("click", () => {
      state.rating = v;
      $("rating-slider").value = String(v);
      $("rating-readout").textContent = v.toFixed(2);
      updatePillHighlight();
    });
    pillsEl.appendChild(pill);
  });

  function updatePillHighlight() {
    pillsEl.querySelectorAll(".pill").forEach((p) => {
      p.classList.toggle("active", parseFloat(p.dataset.value) === state.rating);
    });
  }

  $("rating-slider").addEventListener("input", (e) => {
    state.rating = parseFloat(e.target.value);
    $("rating-readout").textContent = state.rating.toFixed(2);
    updatePillHighlight();
  });

  function renderVenueList(venues) {
    const listEl = $("venue-list");
    listEl.innerHTML = "";
    venues.forEach((v) => {
      const item = document.createElement("div");
      item.className = "venue-item";
      let html = escapeHtml(v.name || v.foursquareId);
      if (v.category) html += ` <span class="venue-category">· ${escapeHtml(v.category)}</span>`;
      if (v.matchedBadges && v.matchedBadges.length) {
        const badgesHtml = v.matchedBadges.map((b) => b.icon
          ? `<span class="venue-badge"><img src="${escapeHtml(b.icon)}" alt="" class="badge-icon"> ${escapeHtml(b.name)}</span>`
          : `<span class="venue-badge">${ICON_AWARD} ${escapeHtml(b.name)}</span>`
        ).join("");
        html += `<div class="venue-badges">${badgesHtml}</div>`;
      }
      item.innerHTML = html;
      item.addEventListener("click", () => {
        state.selectedVenue = v;
        setSelectedVenueDisplay((v.name || "") + (v.category ? ` (${v.category})` : ""));
        listEl.classList.add("hidden");
      });
      listEl.appendChild(item);
    });
    listEl.classList.remove("hidden");
  }

  // Telegram's LocationManager only fires its init() callback the first
  // time it's actually initialized - calling init() again on later taps
  // never calls back at all, which is what left the "🧭 Шукаю…" button
  // stuck forever on a second use. Initialize it (and its inherently async,
  // possibly-slow permission prompt) at most once per session and reuse it.
  let _locationManagerInit = null;
  function ensureLocationManager() {
    const lm = tg && tg.LocationManager;
    if (!lm) return Promise.resolve(null);
    if (!_locationManagerInit) {
      _locationManagerInit = new Promise((resolve) => lm.init(() => resolve(lm)));
    }
    return _locationManagerInit;
  }

  $("venue-toggle-btn").addEventListener("click", async () => {
    const listEl = $("venue-list");
    if (!listEl.classList.contains("hidden")) {
      listEl.classList.add("hidden");
      return;
    }
    if (state.venues === null) {
      $("venue-toggle-btn").innerHTML = `${ICON_PIN} ${escapeHtml(T("app_loading"))}`;
      const { ok, data } = await apiPost("/api/checkin/venues", {});
      if (!ok && data.error === "not_connected") {
        alert(T("app_not_connected"));
        $("venue-toggle-btn").innerHTML = `${ICON_PIN} ${escapeHtml(T("app_my_venues"))}`;
        return;
      }
      state.venues = ok ? (data.venues || []) : [];
      $("venue-toggle-btn").innerHTML = `${ICON_PIN} ${escapeHtml(T("app_my_venues"))}`;
    }
    renderVenueList(state.venues);
  });

  $("venue-nearby-btn").addEventListener("click", async () => {
    const listEl = $("venue-list");
    if (!listEl.classList.contains("hidden")) {
      listEl.classList.add("hidden");
      return;
    }
    if (!tg || !tg.LocationManager) {
      alert(T("app_geo_unavailable_add"));
      return;
    }
    const btn = $("venue-nearby-btn");
    btn.disabled = true;
    btn.innerHTML = `${ICON_COMPASS} ${escapeHtml(T("app_searching"))}`;

    // Safety net: if getLocation's callback never fires for any reason
    // (a stuck permission prompt, a Telegram client quirk), don't leave
    // the button stuck on "Шукаю…" forever - reset after a timeout.
    let settled = false;
    const resetBtn = () => { btn.disabled = false; btn.innerHTML = `${ICON_COMPASS} ${escapeHtml(T("app_nearby_venues"))}`; };
    const timeoutId = setTimeout(() => {
      if (settled) return;
      settled = true;
      resetBtn();
      alert(T("app_geo_timeout"));
    }, 12000);

    const lm = await ensureLocationManager();
    if (!lm || !lm.isLocationAvailable) {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      resetBtn();
      alert(T("app_geo_unavailable_add"));
      return;
    }
    lm.getLocation(async (location) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      resetBtn();
      if (!location) {
        alert(T("app_geo_denied"));
        return;
      }
      state.lastKnownLocation = { lat: location.latitude, lng: location.longitude };
      state.lastVenueSearch = { type: "nearby", lat: location.latitude, lng: location.longitude };
      await runVenueQuery({ lat: location.latitude, lng: location.longitude });
    });
  });

  let venueSearchDebounce = null;
  $("venue-search-input").addEventListener("input", (e) => {
    const q = e.target.value.trim();
    clearTimeout(venueSearchDebounce);
    if (q.length < 2) {
      return;
    }
    venueSearchDebounce = setTimeout(() => {
      state.lastVenueSearch = { type: "query", query: q };
      runVenueSearch(q);
    }, 350);
  });

  // Toggling either filter checkbox re-runs whatever the last search was -
  // otherwise the visible list just silently goes stale (looks like the
  // filter itself is broken when really it just hasn't re-fetched yet).
  function rerunLastVenueSearch() {
    const last = state.lastVenueSearch;
    if (!last) return;
    if (last.type === "nearby") {
      runVenueQuery({ lat: last.lat, lng: last.lng });
    } else {
      runVenueSearch(last.query);
    }
  }
  $("unique-venue-checkbox").addEventListener("change", rerunLastVenueSearch);
  $("badge-venue-checkbox").addEventListener("change", rerunLastVenueSearch);

  async function runVenueSearch(query) {
    const loc = state.lastKnownLocation;
    await runVenueQuery({ query, lat: loc ? loc.lat : null, lng: loc ? loc.lng : null });
  }

  async function runVenueQuery({ query, lat, lng }) {
    const uniqueOnly = $("unique-venue-checkbox").checked;
    const badgeOnly = $("badge-venue-checkbox").checked;
    const { ok, data } = await apiPost("/api/checkin/venues/nearby", {
      query, lat, lng, uniqueOnly, badgeOnly,
    });
    if (!ok) {
      alert(data.error === "rate_limited"
        ? T("app_foursquare_rate_limited")
        : T("app_venues_failed"));
      return;
    }
    renderVenueList(data.venues || []);
  }

  // ---- Submit (no separate confirm screen - rate screen submits directly) ----

  $("to-confirm-btn").addEventListener("click", async () => {
    const btn = $("to-confirm-btn");
    btn.disabled = true;
    btn.textContent = T("app_sending");
    $("submit-status").textContent = "";

    const venue = state.selectedVenue;
    const beer = state.selectedBeer;
    const body = {
      beerId: beer.beerId,
      rating: state.rating,
      shout: $("shout-input").value.trim(),
      foursquareId: venue ? venue.foursquareId : null,
      geolat: venue ? venue.lat : null,
      geolng: venue ? venue.lng : null,
      venueName: venue ? venue.name : null,
      queueItemId: state.origin === "queue" ? state.queueItemId : null,
      // Display-only - only ever used if this attempt ends up saved as a
      // pending check-in (see webapp_server.py's handle_submit), so that
      // screen can render a normal-looking row without an extra lookup.
      beerName: beer.name, brewery: beer.brewery, style: beer.style,
      abv: beer.abv, labelUrl: beer.labelUrl,
    };

    const { ok, status, data } = await apiPost("/api/checkin/submit", body);
    if (ok && data.pending) {
      // Untappd failed (rate-limited or otherwise) - saved for manual retry
      // instead of lost (see pending_checkins.py), so this is shown as a
      // handled outcome, not a hard error. Checked BEFORE the generic
      // data.ok branch below - handle_submit sets data.ok=true on this
      // response too (the HTTP round-trip itself succeeded), so pending
      // must win the check or it's indistinguishable from a real check-in.
      btn.innerHTML = `${ICON_REFRESH} ${escapeHtml(T("app_saved"))}`;
      $("submit-status").textContent = T("app_submit_pending_status");
      updatePendingCheckinsCountOnly();

      setTimeout(() => {
        if (state.origin === "search") {
          $("search-input").value = "";
          $("results").innerHTML = "";
        }
        showScreen(screenForOrigin(state.origin));
      }, 2000);
    } else if (ok && data.ok) {
      if (venue) {
        // Keep in-memory state in sync with what the server just persisted,
        // so the next beer in this same session is pre-filled without
        // waiting for a reload/refetch of /api/checkin/usage.
        state.lastVenue = venue;
      }
      btn.innerHTML = data.dryRun ? `${ICON_CHECK} ${escapeHtml(T("app_done_dry"))}` : `${ICON_CHECK} ${escapeHtml(T("app_checked_in"))}`;
      $("submit-status").textContent = data.dryRun
        ? T("app_dry_run_status")
        : T("app_done");

      setTimeout(() => {
        if (state.origin === "search") {
          $("search-input").value = "";
          $("results").innerHTML = "";
        }
        showScreen(screenForOrigin(state.origin));
      }, state.origin === "queue" ? 1200 : 1500);
    } else {
      btn.disabled = false;
      btn.innerHTML = `${ICON_CHECK} ${escapeHtml(T("app_checkin_btn"))}`;
      if (data.error === "not_connected") {
        $("submit-status").textContent = T("app_not_connected");
      } else {
        $("submit-status").textContent = status === 429
          ? T("app_rate_limited_retry")
          : T("app_error_retry");
      }
    }
  });

  // ---- Auto-toast screen (personal - only the connected account manages
  // its own watch list here) ----
  // Friends are fetched once per screen-open (backend caches for an hour -
  // a full list can be many Untappd API calls, see webapp_server.py's
  // _fetch_all_friends); every checkbox flip immediately persists the
  // *whole* checked set via set_targets - there's no separate "save" step,
  // matching how the queue/venue pickers already work elsewhere in this app.

  let autoToastFriends = [];

  // No standalone bottom-nav tab for this anymore - the settings row itself
  // opens the full screen (friend search etc.), except a tap on the toggle
  // switch, which should just flip on/off without navigating away.
  $("settings-row-autotoast").addEventListener("click", (e) => {
    if (e.target.closest(".toggle-switch")) return;
    showScreen("autotoast");
  });

  async function fetchAutoToastFriends() {
    $("autotoast-status").textContent = T("app_loading");
    $("autotoast-friends-list").innerHTML = "";
    const { ok, data } = await apiPost("/api/checkin/autotoast/friends", {});
    if (!ok) {
      $("autotoast-status").textContent = (data && data.error === "not_connected")
        ? T("app_not_connected")
        : T("app_autotoast_load_failed");
      autoToastFriends = [];
      return;
    }
    autoToastFriends = data.friends || [];
    $("autotoast-enabled-toggle").checked = !!data.enabled;
    $("autotoast-status").textContent = autoToastFriends.length ? "" : T("app_autotoast_empty");
    renderAutoToastFriends();
  }

  function renderAutoToastFriends() {
    const query = $("autotoast-search-input").value.trim().toLowerCase();
    const listEl = $("autotoast-friends-list");
    listEl.innerHTML = "";
    const filtered = query
      ? autoToastFriends.filter((f) =>
          f.username.toLowerCase().includes(query) || (f.name || "").toLowerCase().includes(query))
      : autoToastFriends;
    filtered.forEach((f) => {
      const row = document.createElement("label");
      row.className = "result-row";
      row.innerHTML = `
        <div class="thumb"><img src="${f.avatar || DEFAULT_LABEL_URL}" alt=""></div>
        <div class="result-main">
          <div class="result-name"><span class="result-name-text">${escapeHtml(f.name || f.username)}</span></div>
          <div class="result-meta">@${escapeHtml(f.username)}</div>
        </div>
        <input type="checkbox" class="autotoast-friend-checkbox" data-username="${escapeHtml(f.username)}" ${f.enabled ? "checked" : ""}>
      `;
      listEl.appendChild(row);
    });
    listEl.querySelectorAll(".autotoast-friend-checkbox").forEach((cb) => {
      cb.addEventListener("change", onAutoToastCheckboxChange);
    });
  }

  async function onAutoToastCheckboxChange(e) {
    const username = e.target.dataset.username;
    const entry = autoToastFriends.find((f) => f.username === username);
    if (entry) entry.enabled = e.target.checked;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
    const targets = autoToastFriends.filter((f) => f.enabled).map((f) => f.username);
    await apiPost("/api/checkin/autotoast/set_targets", { targets });
  }

  let autoToastSearchDebounce = null;
  $("autotoast-search-input").addEventListener("input", () => {
    clearTimeout(autoToastSearchDebounce);
    autoToastSearchDebounce = setTimeout(renderAutoToastFriends, 200);
  });

  $("autotoast-enabled-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await apiPost("/api/checkin/autotoast/toggle", { enabled: e.target.checked });
  });

  // ---- Festival novelty watch screen ----
  // Personal, like auto-toast - notifies when a friend checks in something
  // near a saved point that isn't on the festival's own beer list (a tap
  // change, a surprise release). Doesn't touch Untappd at all - the point
  // and radius just get saved, the actual watching happens in the
  // background (_check_festival_novelty, riding along on auto-toast's own
  // feed poll - see README.md for why it's coupled that way for now).

  // No standalone bottom-nav tab for this anymore - see the autotoast row's
  // own comment above for why (same pattern, same reasoning).
  $("settings-row-festivalwatch").addEventListener("click", (e) => {
    if (e.target.closest(".toggle-switch")) return;
    showScreen("festival-watch");
  });

  async function fetchFestivalWatch() {
    const { ok, data } = await apiPost("/api/checkin/festival_watch/get", {});
    if (!ok) {
      $("festival-watch-status").textContent = T("app_load_failed");
      return;
    }
    $("festival-watch-enabled-toggle").checked = !!data.enabled;
    $("festival-watch-notify-listed-toggle").checked = !!data.notifyListedBeers;
    $("festival-watch-radius-input").value = data.radiusMeters || 500;
    $("festival-watch-status").textContent = data.lat != null
      ? T("app_watch_point", { label: data.label || `${data.lat.toFixed(5)}, ${data.lng.toFixed(5)}` })
      : T("app_watch_no_point");
    // venueId set = the watch point resolved to a real Untappd venue, so
    // everyone checking in THERE is seen; the radius still matters because
    // the friends-only radius check keeps running alongside it (catches a
    // friend logging the pour at a neighbouring venue) - see
    // _check_festival_novelty server-side.
    const venueMode = data.venueId != null;
    const hintEl = $("festival-watch-venue-hint");
    hintEl.classList.toggle("hidden", !venueMode);
    if (venueMode) {
      hintEl.innerHTML = `<svg class="icon"><use href="#icon-pin"/></svg> ${escapeHtml(T("app_watch_venue_hint", { venue: data.venueName || T("app_watch_this_venue") }))}`;
    }
    const extrasEl = $("festival-watch-extra-list");
    extrasEl.innerHTML = "";
    (data.extraVenues || []).forEach((v) => {
      const card = document.createElement("div");
      card.className = "venue-selected-card";
      card.innerHTML = `${ICON_PIN} <span>${escapeHtml(v.venueName || String(v.venueId))}</span>
        <button class="queue-remove-btn" aria-label="${escapeHtml(T("app_remove"))}">${ICON_CLOSE}</button>`;
      card.querySelector("button").addEventListener("click", async () => {
        await apiPost("/api/checkin/festival_watch/remove_extra_venue", { venueId: v.venueId });
        await fetchFestivalWatch();
      });
      extrasEl.appendChild(card);
    });
  }

  $("festival-watch-enabled-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await apiPost("/api/checkin/festival_watch/toggle", { enabled: e.target.checked });
  });

  // Off by default - at the very start of a session almost nothing is
  // queued yet, so "on the festival's list but not queued" would fire for
  // nearly every check-in anyone makes (pure noise). Meant to be switched
  // on partway through, once most of the list IS already queued, so this
  // signal actually means something (a keg change, a limited tap).
  $("festival-watch-notify-listed-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await apiPost("/api/checkin/festival_watch/set_notify_listed", { enabled: e.target.checked });
  });

  let festivalWatchRadiusDebounce = null;
  $("festival-watch-radius-input").addEventListener("input", (e) => {
    const meters = parseInt(e.target.value, 10);
    if (!meters || meters <= 0) return;
    clearTimeout(festivalWatchRadiusDebounce);
    festivalWatchRadiusDebounce = setTimeout(() => {
      apiPost("/api/checkin/festival_watch/set_radius", { radiusMeters: meters });
    }, 500);
  });

  async function setFestivalWatchLocation(lat, lng, label, foursquareId) {
    await apiPost("/api/checkin/festival_watch/set_location", { lat, lng, label, foursquareId });
    $("festival-watch-search-results").classList.add("hidden");
    $("festival-watch-search-input").value = "";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await fetchFestivalWatch();
  }

  $("festival-watch-here-btn").addEventListener("click", async () => {
    if (!tg || !tg.LocationManager) {
      alert(T("app_geo_unavailable"));
      return;
    }
    const btn = $("festival-watch-here-btn");
    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `${ICON_COMPASS} ${escapeHtml(T("app_searching"))}`;

    let settled = false;
    const reset = () => { btn.disabled = false; btn.innerHTML = originalHtml; };
    const timeoutId = setTimeout(() => {
      if (settled) return;
      settled = true;
      reset();
      alert(T("app_geo_timeout"));
    }, 12000);

    const lm = await ensureLocationManager();
    if (!lm || !lm.isLocationAvailable) {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      reset();
      alert(T("app_geo_unavailable"));
      return;
    }
    lm.getLocation(async (location) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      reset();
      if (!location) {
        alert(T("app_geo_denied"));
        return;
      }
      // Also seeds state.lastKnownLocation - same shared spot "Локації
      // поруч" fills, so the search box right below (and any other
      // location-biased search this session) gets a real geo bias too,
      // not just this one saved watch point.
      state.lastKnownLocation = { lat: location.latitude, lng: location.longitude };
      await setFestivalWatchLocation(location.latitude, location.longitude, T("app_watch_my_location"));
    });
  });

  let festivalWatchSearchDebounce = null;
  $("festival-watch-search-input").addEventListener("input", (e) => {
    const q = e.target.value.trim();
    clearTimeout(festivalWatchSearchDebounce);
    if (q.length < 2) {
      $("festival-watch-search-results").classList.add("hidden");
      return;
    }
    festivalWatchSearchDebounce = setTimeout(async () => {
      // Without lat/lng, Foursquare falls back to its own IP-based geo
      // bias - and since this call runs server-side, that's the SERVER's
      // location, not the phone's (confirmed live: always Warsaw,
      // regardless of where the actual user is). state.lastKnownLocation
      // is the same GPS point "Локації поруч" already captured this
      // session, if any - reused here so text search is geo-biased to the
      // real device location instead.
      const loc = state.lastKnownLocation;
      const { ok, data } = await apiPost("/api/checkin/venues/nearby", {
        query: q, lat: loc ? loc.lat : null, lng: loc ? loc.lng : null,
      });
      if (!ok) return;
      renderFestivalWatchResults(data.venues || []);
    }, 350);
  });

  function renderFestivalWatchResults(venues) {
    const listEl = $("festival-watch-search-results");
    listEl.innerHTML = "";
    venues.forEach((v) => {
      const item = document.createElement("div");
      item.className = "venue-item";
      item.textContent = v.name || v.foursquareId;
      item.addEventListener("click", () => setFestivalWatchLocation(v.lat, v.lng, v.name, v.foursquareId));
      listEl.appendChild(item);
    });
    listEl.classList.remove("hidden");
  }

  let festivalWatchExtraSearchDebounce = null;
  $("festival-watch-extra-search-input").addEventListener("input", (e) => {
    const q = e.target.value.trim();
    clearTimeout(festivalWatchExtraSearchDebounce);
    if (q.length < 2) {
      $("festival-watch-extra-search-results").classList.add("hidden");
      return;
    }
    festivalWatchExtraSearchDebounce = setTimeout(async () => {
      const loc = state.lastKnownLocation;
      const { ok, data } = await apiPost("/api/checkin/venues/nearby", {
        query: q, lat: loc ? loc.lat : null, lng: loc ? loc.lng : null,
      });
      if (!ok) return;
      const listEl = $("festival-watch-extra-search-results");
      listEl.innerHTML = "";
      (data.venues || []).forEach((v) => {
        const item = document.createElement("div");
        item.className = "venue-item";
        item.textContent = v.name || v.foursquareId;
        item.addEventListener("click", async () => {
          const res = await apiPost("/api/checkin/festival_watch/add_extra_venue", {
            foursquareId: v.foursquareId, name: v.name,
          });
          if (!res.ok) {
            const err = res.data && res.data.error;
            alert(T(err === "venue_not_found"
              ? "app_watch_extra_not_found"
              : err === "already_listed_or_full"
                ? "app_watch_extra_duplicate"
                : "app_watch_extra_failed"));
            return;
          }
          listEl.classList.add("hidden");
          $("festival-watch-extra-search-input").value = "";
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
          await fetchFestivalWatch();
        });
        listEl.appendChild(item);
      });
      listEl.classList.remove("hidden");
    }, 350);
  });

  // ---- Festival map screen (a festival's brewery zones, live/shared) ----
  // Same "shared, server-backed, poll-refreshed" idea as the queue - every
  // connected phone sees (and can edit) the same layout, seeded once from
  // the festival JSON's real per-brewery Area N location rather than
  // alphabetically. Zone names/count come from the server on every fetch
  // (see webapp_server.py's _festival_editable_zone_names) rather than
  // being hardcoded here - MBCC has 4, another festival might have just
  // one, or a dozen. Bonus categories (MBCC's "Lagerland") are shown for
  // reference only - not part of the editable zones, never sent to the
  // move endpoint.

  let MAP_ZONES = []; // populated from the server on every fetchFestivalMap()

  function findZoneEl(zone) {
    return [...document.querySelectorAll(".map-zone")].find((el) => el.dataset.zone === zone);
  }

  $("settings-row-festivalmap").addEventListener("click", () => {
    showScreen("festival-map");
  });

  $("settings-row-festivalswitch").addEventListener("click", () => {
    showScreen("festival-switch");
  });

  $("settings-row-myfestival").addEventListener("click", () => {
    showScreen("my-festival");
  });

  $("settings-row-commandflags").addEventListener("click", () => {
    showScreen("command-flags");
  });

  async function fetchFestivalList() {
    $("festival-switch-status").textContent = T("app_loading");
    $("festival-switch-list").innerHTML = "";
    const { ok, data } = await apiPost("/api/checkin/festival/list", {});
    if (!ok || !data.available) {
      $("festival-switch-status").textContent = T("app_fest_list_failed");
      return;
    }
    state.festivals = data.festivals || [];
    state.activeFestivalKey = data.activeKey;
    renderFestivalSwitchList();
    $("festival-switch-status").textContent = "";
  }

  // Up to two "words" of the label, first letter each ("MBCC 2026" -> "M2",
  // "Тест (1928 пив)" -> "Т1") - the placeholder when a festival has no
  // picture yet.
  function festivalInitials(label) {
    const parts = String(label || "?").replace(/[^\p{L}\p{N}\s]/gu, " ").trim().split(/\s+/);
    return parts.slice(0, 2).map((w) => w.charAt(0)).join("").toUpperCase() || "?";
  }

  function renderFestivalSwitchList() {
    const el = $("festival-switch-list");
    if (!state.festivals.length) {
      el.innerHTML = `<div class="festival-switch-empty">${escapeHtml(T("app_fest_none"))}</div>`;
      return;
    }
    el.innerHTML = state.festivals.map((f) => {
      const active = f.key === state.activeFestivalKey;
      // A festival with no picture in webapp/festivals/ (see
      // webapp_server.py's _festival_image_url) falls back to a plain square
      // with its initials, so the grid stays even.
      const art = f.imageUrl
        ? `<img src="${escapeHtml(f.imageUrl)}" alt="" loading="lazy">`
        : `<span class="festival-tile-initials">${escapeHtml(festivalInitials(f.label))}</span>`;
      return `<button type="button" class="festival-tile${active ? " festival-tile-active" : ""}"
                      data-festival-key="${escapeHtml(f.key)}"${active ? ' aria-current="true"' : ""}>
        <span class="festival-tile-art">${art}<span class="festival-tile-check"><svg class="icon"><use href="#icon-check"/></svg></span></span>
        <span class="festival-tile-label">${escapeHtml(f.label)}</span>
      </button>`;
    }).join("");
    $("festival-switch-list").querySelectorAll("[data-festival-key]").forEach((row) => {
      row.addEventListener("click", () => {
        const key = row.dataset.festivalKey;
        if (key === state.activeFestivalKey) return;
        const label = state.festivals.find((f) => f.key === key)?.label || key;
        const doSwitch = async () => {
          $("festival-switch-status").textContent = T("app_fest_switching");
          const { ok, data } = await apiPost("/api/checkin/festival/switch", { key });
          if (!ok) {
            $("festival-switch-status").textContent = T("app_fest_switch_failed");
            return;
          }
          state.activeFestivalKey = data.activeKey;
          renderFestivalSwitchList();
          $("festival-switch-status").textContent = T("app_fest_done", { n: data.beerCount });
        };
        const msg = T("app_fest_switch_confirm", { label });
        if (tg && tg.showConfirm) {
          tg.showConfirm(msg, (confirmed) => { if (confirmed) doSwitch(); });
        } else if (confirm(msg)) {
          doSwitch();
        }
      });
    });
  }

  async function fetchMyFestivalList() {
    $("my-festival-status").textContent = T("app_loading");
    $("my-festival-list").innerHTML = "";
    const { ok, data } = await apiPost("/api/checkin/festival/my/get", {});
    if (!ok) {
      $("my-festival-status").textContent = T("app_fest_list_failed");
      return;
    }
    state.myFestivals = data.festivals || [];
    state.myPersonalKey = data.personalKey || null;
    state.myEffectiveKey = data.effectiveKey || null;
    renderMyFestivalList();
    $("my-festival-status").textContent = "";
  }

  function renderMyFestivalList() {
    const el = $("my-festival-list");
    if (!state.myFestivals.length) {
      el.innerHTML = `<div class="festival-switch-empty">${escapeHtml(T("app_fest_none"))}</div>`;
      return;
    }
    // "Автоматично" is always first - represents "no personal override",
    // falling back to the user's group binding (if any) or the shared
    // default. Having no festival of its own, its art is the app's own mark
    // (the same <symbol> the search screen's idle hero uses).
    const autoActive = !state.myPersonalKey;
    const autoTile = `<button type="button" class="festival-tile${autoActive ? " festival-tile-active" : ""}"
                    data-festival-key=""${autoActive ? ' aria-current="true"' : ""}>
      <span class="festival-tile-art"><svg class="festival-tile-auto-mark" viewBox="0 0 512 512"><use href="#app-mark"/></svg><span class="festival-tile-check"><svg class="icon"><use href="#icon-check"/></svg></span></span>
      <span class="festival-tile-label">${escapeHtml(T("app_fest_auto"))}</span>
    </button>`;
    const festivalTiles = state.myFestivals.map((f) => {
      const active = f.key === state.myPersonalKey;
      const art = f.imageUrl
        ? `<img src="${escapeHtml(f.imageUrl)}" alt="" loading="lazy">`
        : `<span class="festival-tile-initials">${escapeHtml(festivalInitials(f.label))}</span>`;
      return `<button type="button" class="festival-tile${active ? " festival-tile-active" : ""}"
                      data-festival-key="${escapeHtml(f.key)}"${active ? ' aria-current="true"' : ""}>
        <span class="festival-tile-art">${art}<span class="festival-tile-check"><svg class="icon"><use href="#icon-check"/></svg></span></span>
        <span class="festival-tile-label">${escapeHtml(f.label)}</span>
      </button>`;
    }).join("");
    el.innerHTML = autoTile + festivalTiles;
    el.querySelectorAll("[data-festival-key]").forEach((row) => {
      row.addEventListener("click", async () => {
        const key = row.dataset.festivalKey || null;
        if (key === state.myPersonalKey) return;
        $("my-festival-status").textContent = T("app_saving");
        const { ok, data } = await apiPost("/api/checkin/festival/my/set", { key });
        if (!ok) {
          $("my-festival-status").textContent = T("app_fest_save_failed");
          return;
        }
        state.myPersonalKey = data.personalKey;
        renderMyFestivalList();
        $("my-festival-status").textContent = "";
      });
    });
  }

  async function fetchCommandFlags() {
    $("command-flags-status").textContent = T("app_loading");
    $("command-flags-list").innerHTML = "";
    const { ok, data } = await apiPost("/api/checkin/command_flags/get", {});
    if (!ok || !data.available) {
      $("command-flags-status").textContent = T("app_flags_load_failed");
      return;
    }
    state.commandFlags = data.commands || [];
    state.photoRecognitionEnabled = !!data.photoRecognition;
    renderCommandFlags();
    $("command-flags-status").textContent = "";
  }

  function commandFlagRowHtml(id, title, hint, checked, command) {
    // The photo-recognition row (command=null) has no grip - it's not a
    // real command, so there's nothing to reorder it relative to; it
    // always stays pinned above the draggable command list.
    const grip = command
      ? `<span class="command-flag-grip"><svg class="icon icon-filled"><use href="#icon-grip"/></svg></span>`
      : "";
    return `<div class="settings-row command-flag-row" id="${id}-row"${command ? ` data-command="${escapeHtml(command)}"` : ""}>
      ${grip}
      <div class="settings-row-label">
        <div class="settings-row-title">${escapeHtml(title)}</div>
        <div class="settings-row-hint">${escapeHtml(hint)}</div>
      </div>
      <label class="toggle-switch">
        <input type="checkbox" id="${id}-toggle"${checked ? " checked" : ""}>
        <span class="toggle-slider"></span>
      </label>
    </div>`;
  }

  function renderCommandFlags() {
    const el = $("command-flags-list");
    const photoRow = commandFlagRowHtml(
      "command-flag-photo", T("app_flag_photo_label"),
      T("app_flag_photo_desc"),
      state.photoRecognitionEnabled, null,
    );
    const commandRows = state.commandFlags.map((c) =>
      commandFlagRowHtml(`command-flag-${c.command}`, `/${c.command} — ${c.label}`, c.description, c.enabled, c.command)
    ).join("");
    el.innerHTML = photoRow + commandRows;

    $("command-flag-photo-toggle").addEventListener("change", async (e) => {
      const enabled = e.target.checked;
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      const { ok } = await apiPost("/api/checkin/command_flags/set", { photoRecognition: enabled });
      if (ok) state.photoRecognitionEnabled = enabled;
    });
    el.querySelectorAll(".command-flag-grip").forEach((grip) => {
      grip.addEventListener("pointerdown", onCommandRowPointerDown);
    });
    state.commandFlags.forEach((c) => {
      $(`command-flag-${c.command}-toggle`).addEventListener("change", async (e) => {
        const enabled = e.target.checked;
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        const { ok } = await apiPost("/api/checkin/command_flags/set", { command: c.command, enabled });
        if (ok) c.enabled = enabled;
      });
    });
  }

  // Pointer-Events-based drag for reordering the command list, same
  // technique as the festival map's brewery-pill drag (see onMapPillPointerDown's
  // own comment for why: native HTML5 drag-and-drop never fires on touch
  // in Telegram's mobile WebView) but simplified for a single vertical
  // list - no zones/sides, just "which slot is the pointer over now."
  // Started from the row's grip handle specifically (not the whole row),
  // so a tap on the toggle switch itself never gets mistaken for a
  // drag-start.
  let commandDrag = null; // { command, row, placeholder, lastIndex }
  let commandDragActive = false;

  function cancelCommandDrag() {
    if (commandDrag) {
      commandDrag.row.remove();
      commandDrag.placeholder.remove();
    }
    document.removeEventListener("pointermove", onCommandRowPointerMove);
    document.removeEventListener("pointerup", onCommandRowPointerUp);
    document.removeEventListener("pointercancel", onCommandRowPointerUp);
    commandDrag = null;
    commandDragActive = false;
  }

  function flipReorderCommandList(mutate) {
    const rows = document.querySelectorAll("#command-flags-list .command-flag-row");
    const firstRects = new Map();
    rows.forEach((el) => firstRects.set(el, el.getBoundingClientRect()));
    mutate();
    firstRects.forEach((first, el) => {
      if (!el.isConnected) return;
      const last = el.getBoundingClientRect();
      const dy = first.top - last.top;
      if (Math.abs(dy) < 0.5) return;
      el.style.transition = "none";
      el.style.transform = `translateY(${dy}px)`;
      el.getBoundingClientRect(); // force layout so the transform above is committed before transitioning away from it
      requestAnimationFrame(() => {
        el.style.transition = "transform 0.18s ease";
        el.style.transform = "";
      });
    });
  }

  function placeCommandPlaceholderAt(index) {
    const list = $("command-flags-list");
    const { placeholder } = commandDrag;
    // The photo row has no data-command and is never part of the
    // reorderable set - excluded here the same way it's excluded from
    // state.commandFlags, so index 0 always means "first COMMAND slot",
    // never displacing the pinned photo row above it.
    const siblings = [...list.querySelectorAll(".command-flag-row[data-command]")].filter((el) => el !== placeholder);
    const refNode = siblings[index] || null;
    if (refNode) list.insertBefore(placeholder, refNode);
    else list.appendChild(placeholder);
  }

  function onCommandRowPointerDown(e) {
    if (commandDrag) cancelCommandDrag();
    e.preventDefault();
    const row = e.currentTarget.closest(".command-flag-row");
    const rect = row.getBoundingClientRect();
    const list = row.parentElement;
    const originalIndex = [...list.querySelectorAll(".command-flag-row[data-command]")].indexOf(row);

    const placeholder = document.createElement("div");
    placeholder.className = "command-flag-row-placeholder";
    placeholder.style.height = `${rect.height}px`;
    list.insertBefore(placeholder, row);

    try { row.setPointerCapture(e.pointerId); } catch { /* ignore, see onMapPillPointerDown's own note */ }
    commandDragActive = true;
    row.classList.add("command-flag-row-floating");
    row.style.width = `${rect.width}px`;
    row.style.left = `${rect.left}px`;
    row.style.top = `${rect.top}px`;
    document.body.appendChild(row);

    commandDrag = {
      command: row.dataset.command,
      row, placeholder,
      offsetY: e.clientY - rect.top,
      lastIndex: originalIndex,
    };
    document.addEventListener("pointermove", onCommandRowPointerMove);
    document.addEventListener("pointerup", onCommandRowPointerUp);
    document.addEventListener("pointercancel", onCommandRowPointerUp);
  }

  function onCommandRowPointerMove(e) {
    if (!commandDrag) return;
    const { row, offsetY, placeholder } = commandDrag;
    row.style.top = `${e.clientY - offsetY}px`;

    const siblings = [...$("command-flags-list").querySelectorAll(".command-flag-row[data-command]")]
      .filter((el) => el !== placeholder);
    let index = siblings.length;
    for (let i = 0; i < siblings.length; i++) {
      const r = siblings[i].getBoundingClientRect();
      if (e.clientY < r.top + r.height / 2) { index = i; break; }
    }
    if (index !== commandDrag.lastIndex) {
      flipReorderCommandList(() => placeCommandPlaceholderAt(index));
      commandDrag.lastIndex = index;
    }
  }

  async function onCommandRowPointerUp() {
    if (!commandDrag) return;
    const { row, placeholder } = commandDrag;
    const list = $("command-flags-list");
    flipReorderCommandList(() => {
      list.insertBefore(row, placeholder);
      placeholder.remove();
    });
    row.classList.remove("command-flag-row-floating");
    row.style.width = "";
    row.style.left = "";
    row.style.top = "";
    document.removeEventListener("pointermove", onCommandRowPointerMove);
    document.removeEventListener("pointerup", onCommandRowPointerUp);
    document.removeEventListener("pointercancel", onCommandRowPointerUp);
    commandDragActive = false;
    commandDrag = null;

    const newOrder = [...list.querySelectorAll(".command-flag-row[data-command]")].map((el) => el.dataset.command);
    state.commandFlags.sort((a, b) => newOrder.indexOf(a.command) - newOrder.indexOf(b.command));
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
    await apiPost("/api/checkin/command_flags/reorder", { order: newOrder });
  }

  async function fetchFestivalMap() {
    if (mapDragActive) return; // don't yank a pill mid-gesture on an incoming poll tick
    const { ok, data } = await apiPost("/api/checkin/festival_map/get", {});
    if (!ok) {
      $("festival-map-status").textContent = T("app_map_load_failed");
      return;
    }
    $("festival-map-status").textContent = "";
    MAP_ZONES = data.zoneOrder || [];
    state.festivalMap = {
      zones: data.zones || {}, bonusCategories: data.bonusCategories || {},
      zoneLabels: data.zoneLabels || {}, breweryAliases: data.breweryAliases || {},
      waterStands: new Set(data.waterStands || []),
      plannedStands: new Set(data.plannedStands || []),
    };
    $("festival-map-water-legend").hidden = state.festivalMap.waterStands.size === 0;
    renderFestivalMap();
  }

  // A zone's real identity is always its raw key ("Area 1" - used for
  // drag/drop, festival_map.py's persistence, and _ZONE_NAME_RE matching
  // server-side) - this is ONLY what's shown to the viewer, per-festival
  // (see festivals.json's optional "zoneLabels", e.g. WFP calls its zones
  // floors, not "Area 1"). Falls back to the raw key when the current
  // festival has no custom labels.
  function zoneDisplayLabel(zone) {
    return (state.festivalMap.zoneLabels && state.festivalMap.zoneLabels[zone]) || zone;
  }

  // The .map-zone card structure (header, dot-preview, perimeter-grid) used
  // to be 4 copies of static HTML - now built once per zone here, since the
  // zone list isn't known until the server reports it.
  function buildZoneCard(zone) {
    const card = document.createElement("div");
    card.className = "map-zone";
    card.dataset.zone = zone;
    card.innerHTML = `
      <div class="map-zone-header">
        <span class="map-zone-label">${escapeHtml(zoneDisplayLabel(zone))}</span>
        <span class="map-zone-count"></span>
      </div>
      <div class="map-zone-pills preview" data-zone="${escapeHtml(zone)}"></div>
      <div class="perimeter-grid">
        <div class="perimeter-top"></div>
        <div class="perimeter-left"></div>
        <div class="perimeter-mid">
          <div class="map-islands"></div>
          <button type="button" class="map-island-add-btn">${escapeHtml(T("app_map_add_island"))}</button>
          <div class="map-gap-source">${escapeHtml(T("app_map_add_gap"))}</div>
          ${ICON_BEER}
        </div>
        <div class="perimeter-right"></div>
        <div class="perimeter-bottom"></div>
      </div>`;
    card.querySelector(".map-gap-source").addEventListener("pointerdown", onGapSourcePointerDown);
    return card;
  }

  // Some breweries' real Untappd/festival names are too long to read as a
  // single map pill (the perimeter grid gives each pill only a narrow
  // column's width) - a small hand-maintained shortened DISPLAY label,
  // same spirit as beer_match.py's own hand-maintained substitution
  // tables. Only ever affects what's shown - the pill's `dataset.brewery`
  // (used for search highlighting, drag/drop, and the click-through to
  // openBreweryBeers) always stays the real, full name, so this can't
  // silently break matching the way a shortened name baked into the data
  // itself would.
  const BREWERY_DISPLAY_ALIASES = {
    "Wunderkammer Biermanufaktur": "Wunderkammer",
    "Brasserie du Bas-Canada": "du Bas-Canada",
    "Kemker Kultuur (Brauerei J. Kemker)": "Kemker Kultuur",
    "Frequentem Brewing Co.": "Frequentem",
    "DEYA Brewing Company": "DEYA",
    "Goose Island Beer Co.": "Goose Island",
    "Duckpond Brewing": "Duckpond",
    "Factory Brewing": "Factory",
    "Browar Artezan": "Artezan",
    "Browar Birbant": "Birbant",
    "Clandestin Beer": "Clandestin",
    "is/was brewing": "is/was",
    "Lubrow Brewery": "Lubrow",
    "Neon Raptor Brewing Co.": "Neon Raptor",
    "Other Half Brewing Co.": "Other Half",
    "Sante Adairius Rustic Ales": "SARA",
    "Browar Spółdzielczy": "Spółdzielczy",
    "Browar Stu Mostów": "Stu Mostów",
    "Trillium Brewing Company": "Trillium",
    "Varietal Beer Company": "Varietal",
    "Underwood Brewery": "Underwood",
    "The Attic": "Attic Meadery",
    "Rodinný pivovar Zichovec": "Zichovec",
    "Augustowska Miodosytnia": "Augustowska",
    "Ārpus Brewing Co.": "Ārpus",
    "Apex Brewing Company": "Apex",
    "Blackout Brewing": "Blackout",
    "Cydr Chyliczki": "Chyliczki",
    "Browar Cztery Ściany / Four Walls Brewery": "Cztery Ściany",
    "Duality Brewing": "Duality",
    "Harpagan Craft Beer": "Harpagan",
    "Brasserie La Malpolon": "La Malpolon",
    "Lubrow Brett & Barrel": "Lubrow",
    "Moon Lark Brewery": "Moon Lark",
    "Nepo Brewing": "Nepo",
    "Piwne Podziemie / Beer Underground": "Piwne Podziemie",
    "LOKO* by Sáez & Son": "LOKO*",
    "SOMA Beer": "SOMA",
    "Spyglass Brewing Company": "Spyglass",
    "TankBusters.Co": "TankBusters",
    "Calderona Lagers by Sáez & Son": "Sáez & Son",
    "Browar Bednary": "Bednary",
    "Browar Sulewski": "Sulewski",
    "Browar Wielka Sowa": "Wielka Sowa",
    "Browar Brokreacja": "Brokreacja",
    "Browar Nieczajna": "Nieczajna",
    "Browar Warszawski": "Warszawski",
    "Browar Bałtów": "Bałtów",
    "Browar Kingpin": "Kingpin",
    "Browar Monsters": "Monsters",
    "Sick Boy Brewing": "Sick Boy",
    "Browar Monsters / Sick Boy Brewing": "Monsters/Sick Boy",
  };

  function makeBreweryPill(brewery, draggable) {
    const pill = document.createElement("div");
    pill.className = "brewery-pill";
    pill.textContent = BREWERY_DISPLAY_ALIASES[brewery] || brewery;
    pill.title = brewery;
    pill.dataset.brewery = brewery;
    if (state.festivalMap && state.festivalMap.waterStands && state.festivalMap.waterStands.has(brewery)) {
      const drop = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      drop.setAttribute("class", "icon icon-filled pill-water-icon");
      drop.setAttribute("aria-label", T("app_map_water"));
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#icon-droplet");
      drop.appendChild(use);
      pill.prepend(drop);
      pill.title = brewery + " · " + T("app_map_water");
    }
    // A plan stand whose menu isn't in the data yet: shown (and movable by
    // editors) but there are no beers to open.
    const planned = !!(state.festivalMap && state.festivalMap.plannedStands && state.festivalMap.plannedStands.has(brewery));
    if (planned) {
      pill.classList.add("brewery-pill-planned");
      pill.title = brewery + " · " + T("app_map_planned");
    }
    if (draggable) {
      pill.addEventListener("pointerdown", onMapPillPointerDown);
    } else if (!planned) {
      // Non-draggable pills only ever show up in read-only contexts (the
      // zone detail view, Lagerland) - editing has its own drag gesture and
      // deliberately doesn't also open this on a stray tap.
      pill.classList.add("brewery-pill-clickable");
      pill.addEventListener("click", () => openBreweryBeers(brewery));
    }
    return pill;
  }

  const MAP_SIDES = ["top", "left", "right", "bottom"];

  function zoneTotal(zoneSides) {
    // .filter(Boolean) drops empty-slot `null` entries (see
    // festival_map.py's own docstring) - they're placeholders, not real
    // breweries, and shouldn't inflate the zone's own beer count.
    const perimeter = MAP_SIDES.reduce((sum, side) => sum + (zoneSides[side] || []).filter(Boolean).length, 0);
    const islands = Object.values(zoneSides.islands || {})
      .reduce((sum, island) => sum + (island.breweries || []).filter(Boolean).length, 0);
    return perimeter + islands;
  }

  // Overview cards are too small to show 20-28 readable pills, so outside
  // edit mode they show a dense grid of blank dots (just a glanceable
  // "how full is this zone") - tapping the card opens the full-screen
  // detail view instead, which is where the real pill list lives.
  function renderZonePreviewDots(container, count) {
    for (let i = 0; i < count; i++) {
      const dot = document.createElement("div");
      dot.className = "brewery-pill-preview";
      container.appendChild(dot);
    }
  }

  function perimeterSections(rootEl) {
    return {
      top: rootEl.querySelector(".perimeter-top"),
      left: rootEl.querySelector(".perimeter-left"),
      right: rootEl.querySelector(".perimeter-right"),
      bottom: rootEl.querySelector(".perimeter-bottom"),
    };
  }

  // The real venue layout runs breweries along the whole perimeter of a
  // rectangle - a short top row, tall left/right columns, a short bottom
  // row - not a flat grid. Each side is stored and edited as its own
  // independent list (see festival_map.py's module docstring for why: a
  // flat list re-split by position parity on every render meant dragging
  // one brewery a few slots could silently flip unrelated breweries into
  // the other column), so rendering is a direct 1:1 pass, no splitting.
  //
  // Left/right specifically render as a shared virtual row grid, in edit
  // mode only: the shorter side pads out (with .perimeter-row-slot
  // placeholders, not real data) to match however many rows the OTHER
  // side has, so a lone brewery on one side can still be dropped into any
  // one of, say, 5 positions instead of only "before/after" the one thing
  // already there (a `None` a side's OWN list might already hold - see
  // festival_map.py - renders as this same kind of slot, at its own
  // index, independent of this cross-side padding). The read-only detail
  // view has no dragging to support, so it skips all of this and just
  // shows whatever's really there, tightly packed.
  //
  // The empty slots (`null`s, i.e. the gaps / walkways the editors left
  // between stands) are kept in the read-only view too, as invisible
  // spacers, and left/right always share one row grid - otherwise the
  // saved layout would collapse the moment edit mode is left (positions
  // only lined up while editing).
  function makeGapSlot(zone, side, islandId, index, removable) {
    const slot = document.createElement("div");
    slot.className = "perimeter-row-slot" + (side === "left" || side === "right" ? "" : " perimeter-gap-h");
    if (removable && festivalMapEditMode) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "perimeter-gap-remove";
      btn.setAttribute("aria-label", T("app_map_remove_gap"));
      btn.textContent = "×";
      btn.addEventListener("click", () => removeMapGap(zone, side, islandId, index));
      slot.appendChild(btn);
    }
    return slot;
  }

  function renderPerimeterPills(rootEl, zoneSides, draggable) {
    const sections = perimeterSections(rootEl);
    const zoneCard = rootEl.closest(".map-zone");
    const zone = zoneCard ? zoneCard.dataset.zone : null;
    ["top", "bottom"].forEach((side) => {
      const container = sections[side];
      container.innerHTML = "";
      (zoneSides[side] || []).forEach((brewery, i) => {
        container.appendChild(brewery ? makeBreweryPill(brewery, draggable) : makeGapSlot(zone, side, null, i, true));
      });
    });
    const leftList = zoneSides.left || [];
    const rightList = zoneSides.right || [];
    const rowCount = Math.max(leftList.length, rightList.length, draggable ? 1 : 0);
    ["left", "right"].forEach((side) => {
      const container = sections[side];
      container.innerHTML = "";
      const list = side === "left" ? leftList : rightList;
      for (let i = 0; i < rowCount; i++) {
        const brewery = list[i];
        // `null` is a real, stored gap (removable); undefined is just this
        // side padding out to the other side's row count.
        container.appendChild(brewery ? makeBreweryPill(brewery, draggable) : makeGapSlot(zone, side, null, i, brewery === null));
      }
    });
  }

  // Removes a stored gap (collapsing what follows it) - optimistic locally,
  // then confirmed by the server like every other map edit.
  async function removeMapGap(zone, side, islandId, index) {
    const sides = state.festivalMap.zones[zone];
    if (!sides) return;
    const list = side === "island"
      ? (sides.islands[islandId] || {}).breweries
      : sides[side];
    if (!list || list[index] !== null) return;
    list.splice(index, 1);
    renderFestivalMap();
    await apiPost("/api/checkin/festival_map/gap_remove", { zone, side, index, islandId: side === "island" ? islandId : null });
  }

  // Interior clusters (see festival_map.py's own docstring) - a handful of
  // small brewery groups floating in a zone's decorative middle, for
  // venues whose real layout isn't just a perimeter (WFP's 2nd floor, for
  // one). Rendered inside the same .perimeter-mid the beer-icon watermark
  // already lives in; the icon only shows while there are no islands yet
  // (see style.css's .has-islands rule). Unlike renderPerimeterPills,
  // islands are never auto-seeded/padded - they only exist once an editor
  // explicitly creates one (the "+ Острівець" button), so there's no
  // empty-state placeholder grid to build here.
  function renderIslands(midEl, zone, islands, draggable) {
    const wrap = midEl.querySelector(".map-islands");
    wrap.innerHTML = "";
    // An island whose stands were all removed (e.g. dropped from the plan)
    // would be an empty dashed box - only editors still see it, to delete it.
    const entries = Object.entries(islands || {}).filter(
      ([, island]) => draggable || (island.breweries || []).some(Boolean)
    );
    midEl.classList.toggle("has-islands", entries.length > 0);
    entries.forEach(([islandId, island]) => {
      const box = document.createElement("div");
      box.className = "map-island";
      box.dataset.islandId = islandId;
      const pills = document.createElement("div");
      pills.className = "map-island-pills";
      (island.breweries || []).forEach((brewery, i) => {
        pills.appendChild(brewery ? makeBreweryPill(brewery, draggable) : makeGapSlot(zone, "island", islandId, i, true));
      });
      box.appendChild(pills);
      if (draggable) {
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "map-island-remove";
        removeBtn.textContent = "×";
        removeBtn.addEventListener("click", () => deleteMapIsland(zone, islandId));
        box.appendChild(removeBtn);
      }
      wrap.appendChild(box);
    });
    // Only the edit-capable render path (buildZoneCard) has this button at
    // all - the read-only detail view's static markup doesn't include one.
    const addBtn = midEl.querySelector(".map-island-add-btn");
    if (addBtn) addBtn.onclick = () => addMapIsland(zone);
  }

  async function addMapIsland(zone) {
    const { ok, data } = await apiPost("/api/checkin/festival_map/island_create", { zone });
    if (!ok || !data.islandId) return;
    const zoneSides = state.festivalMap.zones[zone] || (state.festivalMap.zones[zone] = {});
    zoneSides.islands = zoneSides.islands || {};
    zoneSides.islands[data.islandId] = { label: "", breweries: [] };
    renderFestivalMap();
  }

  // Optimistically returns the island's breweries to `top` locally too
  // (mirrors festival_map.py's delete_island) so they don't visibly
  // vanish until the next poll reconciles - same "match what the server
  // will confirm" instinct as onMapPillPointerUp's own optimistic move.
  async function deleteMapIsland(zone, islandId) {
    const zoneSides = state.festivalMap.zones[zone];
    const island = zoneSides && zoneSides.islands && zoneSides.islands[islandId];
    if (island) {
      delete zoneSides.islands[islandId];
      zoneSides.top = (zoneSides.top || []).concat((island.breweries || []).filter(Boolean));
    }
    renderFestivalMap();
    await apiPost("/api/checkin/festival_map/island_delete", { zone, islandId });
  }

  let festivalMapDetailZone = null; // which zone (if any) the full-screen detail view is showing

  function renderFestivalMap() {
    // Wipe-and-rebuild, same philosophy as the pill contents inside each
    // zone already use - this data changes rarely enough that rebuilding a
    // handful of zone cards per poll tick is cheap, and it's the only way
    // to handle a zone count that isn't known until the server replies.
    const zonesContainer = $("festival-map-zones");
    zonesContainer.innerHTML = "";
    // Edit mode's 2-per-row column count depends on how many zones this
    // festival has (ceil(N/2) - 1 column for a single zone, 2 for 3-4,
    // etc.) - see style.css's own comment on why this is set here instead
    // of as a fixed rule. Cleared outside edit mode so the overview's own
    // auto-fit rule (a plain CSS class, lower priority than any inline
    // style) actually takes effect instead of being overridden by this.
    if (festivalMapEditMode) {
      const editColumns = Math.max(1, Math.ceil(MAP_ZONES.length / 2));
      zonesContainer.style.gridTemplateColumns = `repeat(${editColumns}, calc(100vw - 30px))`;
    } else {
      zonesContainer.style.gridTemplateColumns = "";
    }
    MAP_ZONES.forEach((zone) => {
      const zoneSides = state.festivalMap.zones[zone] || {};
      const total = zoneTotal(zoneSides);
      const zoneEl = buildZoneCard(zone);
      zonesContainer.appendChild(zoneEl);
      const dotsContainer = zoneEl.querySelector(".map-zone-pills.preview");
      renderZonePreviewDots(dotsContainer, total);
      renderPerimeterPills(zoneEl.querySelector(".perimeter-grid"), zoneSides, true);
      renderIslands(zoneEl.querySelector(".perimeter-mid"), zone, zoneSides.islands, true);
      zoneEl.querySelector(".map-zone-count").textContent = total;
      zoneEl.addEventListener("click", () => {
        if (festivalMapEditMode) return;
        openFestivalMapDetail(zone);
      });
    });
    const bonusContainer = $("festival-map-bonus-categories");
    bonusContainer.innerHTML = "";
    Object.entries(state.festivalMap.bonusCategories).forEach(([name, breweries]) => {
      const section = document.createElement("div");
      section.className = "festival-map-lagerland";
      section.innerHTML = `<div class="map-zone-label">${ICON_BEER} ${escapeHtml(name)} <span class="hint">${escapeHtml(T("app_map_zone_readonly"))}</span></div>`;
      const pillsEl = document.createElement("div");
      pillsEl.className = "lagerland-pills";
      breweries.forEach((brewery) => pillsEl.appendChild(makeBreweryPill(brewery, false)));
      section.appendChild(pillsEl);
      bonusContainer.appendChild(section);
    });
    if (festivalMapDetailZone) renderFestivalMapDetail();
    applyActiveSearchHighlight(); // re-apply after a poll/mode-switch rebuild - see its own docstring
  }

  function renderFestivalMapDetail() {
    const zoneSides = state.festivalMap.zones[festivalMapDetailZone] || {};
    $("festival-map-detail-label").textContent = zoneDisplayLabel(festivalMapDetailZone);
    $("festival-map-detail-count").textContent = zoneTotal(zoneSides);
    renderPerimeterPills($("festival-map-detail-pills"), zoneSides, false);
    renderIslands($("festival-map-detail-pills").querySelector(".perimeter-mid"), festivalMapDetailZone, zoneSides.islands, false);
    applyActiveSearchHighlight();
  }

  function openFestivalMapDetail(zone, highlightBrewery) {
    festivalMapDetailZone = zone;
    $("festival-map-overview").classList.add("hidden");
    $("festival-map-edit-btn").classList.add("hidden");
    $("festival-map-detail").classList.remove("hidden");
    renderFestivalMapDetail();
    if (highlightBrewery) {
      highlightBreweryPill(highlightBrewery);
      flashZoneLabel();
    }
  }

  function closeFestivalMapDetail() {
    festivalMapDetailZone = null;
    $("festival-map-detail").classList.add("hidden");
    $("festival-map-overview").classList.remove("hidden");
    $("festival-map-edit-btn").classList.remove("hidden");
  }

  $("festival-map-detail-back").addEventListener("click", closeFestivalMapDetail);

  // A search result stays marked as the "active" pick (a persistent ring,
  // not the fading flash below) until a different brewery is searched or
  // the map screen is left entirely (see showScreen's teardown). Re-applied
  // after every render since polling/mode switches rebuild the pills fresh.
  let activeSearchBrewery = null;

  function clearActiveSearchHighlight() {
    activeSearchBrewery = null;
    document.querySelectorAll(".brewery-pill-active").forEach((el) => el.classList.remove("brewery-pill-active"));
  }

  function applyActiveSearchHighlight() {
    if (!activeSearchBrewery) return;
    const pill = [...document.querySelectorAll(".brewery-pill")]
      .find((el) => el.dataset.brewery === activeSearchBrewery && el.offsetParent !== null);
    if (pill) pill.classList.add("brewery-pill-active");
  }

  // Brief, non-persistent flash on the open zone's "Area N" label - just a
  // visual cue that this is the zone search jumped to, unlike the pill's
  // active marker which sticks around.
  function flashZoneLabel() {
    const label = $("festival-map-detail-label");
    label.classList.remove("map-zone-label-flash");
    void label.offsetWidth;
    label.classList.add("map-zone-label-flash");
    setTimeout(() => label.classList.remove("map-zone-label-flash"), 1600);
  }

  // Flashes and scrolls to a brewery's pill wherever it's currently
  // visible (the detail view's own list, or the Lagerland row - overview
  // cards only show dots, never a real named pill, so this naturally never
  // matches one of those), and marks it as the active search result.
  function highlightBreweryPill(brewery) {
    clearActiveSearchHighlight();
    activeSearchBrewery = brewery;
    const pill = [...document.querySelectorAll(".brewery-pill")]
      .find((el) => el.dataset.brewery === brewery && el.offsetParent !== null);
    if (!pill) return;
    pill.scrollIntoView({ behavior: "smooth", block: "center" });
    pill.classList.add("brewery-pill-active");
    pill.classList.remove("brewery-pill-highlight");
    // Force a reflow so re-adding the class restarts the CSS animation even
    // if the same brewery was just highlighted a moment ago.
    void pill.offsetWidth;
    pill.classList.add("brewery-pill-highlight");
    setTimeout(() => pill.classList.remove("brewery-pill-highlight"), 1600);
  }

  // ---- Festival map search (finds a brewery across all 4 zones + Lagerland,
  // jumping straight to it) ----
  function allMapBreweries() {
    const list = [];
    MAP_ZONES.forEach((zone) => {
      const sides = state.festivalMap.zones[zone] || {};
      MAP_SIDES.forEach((side) => {
        (sides[side] || []).forEach((brewery) => { if (brewery) list.push({ brewery, zone }); });
      });
      // Islands (see renderIslands) are a separate list the perimeter walk
      // above never touches - a stand moved into one would otherwise
      // silently drop out of search entirely, taking every collab alias
      // resolving to it down with it (confirmed live: dragging "OneMoreBeer"
      // into an island made "Brouwerij Lindemans" unsearchable too, since
      // the alias lookup below only ever finds a REAL pill in this list).
      Object.values(sides.islands || {}).forEach((island) => {
        (island.breweries || []).forEach((brewery) => { if (brewery) list.push({ brewery, zone }); });
      });
    });
    Object.entries(state.festivalMap.bonusCategories).forEach(([category, breweries]) => {
      breweries.forEach((brewery) => list.push({ brewery, zone: null, category }));
    });
    // A collab beer's OTHER named brewery (e.g. "Verdant Brewing Co" on a
    // beer poured at PINTA's stand - see webapp_server.py's
    // _festival_brewery_aliases) has no stand/pill of its own, so it'd
    // otherwise never show up here at all. Search still needs to find it
    // under its own credited name - just resolving to whichever REAL
    // stand it's actually at (selectFestivalMapSearchResult opens/
    // highlights `realBrewery`, never a phantom entry for the alias
    // itself).
    Object.entries(state.festivalMap.breweryAliases || {}).forEach(([alias, realBrewery]) => {
      const real = list.find((it) => it.brewery === realBrewery);
      if (real) list.push({ brewery: alias, zone: real.zone, category: real.category, realBrewery });
    });
    return list;
  }

  function selectFestivalMapSearchResult(match) {
    const input = $("festival-map-search-input");
    input.value = "";
    // A real "input" event (not just setting .value) - addSearchClearButton's
    // own listener is what actually hides the "x" clear button; skipping
    // this left it visibly stuck showing after picking a result.
    input.dispatchEvent(new Event("input", { bubbles: true }));
    $("festival-map-search-results").classList.add("hidden");
    $("festival-map-search-results").innerHTML = "";
    // A collab beer's OTHER named brewery (match.realBrewery set - see
    // allMapBreweries' own note) has no stand/pill of its own - open or
    // highlight whichever REAL stand it's actually poured at instead.
    const targetBrewery = match.realBrewery || match.brewery;
    if (match.zone) {
      openFestivalMapDetail(match.zone, targetBrewery);
    } else {
      highlightBreweryPill(targetBrewery); // a bonus category - already visible on the overview
    }
  }

  // Public page only: beers of the festival database matching the typed
  // text (server-side local search - see handle_public_search), appended
  // under the brewery matches. A result opens its stand on the map; the
  // link button goes to the beer on Untappd.
  let publicSearchTimer = null;
  let publicSearchSeq = 0;
  function schedulePublicBeerSearch(rawQuery) {
    if (!PUBLIC_MODE) return;
    clearTimeout(publicSearchTimer);
    const seq = ++publicSearchSeq;
    const query = rawQuery.trim();
    if (query.length < 2) return;
    publicSearchTimer = setTimeout(async () => {
      const { ok, data } = await apiPost("/api/public/search", { query });
      if (!ok || seq !== publicSearchSeq) return;
      const resultsEl = $("festival-map-search-results");
      (data.results || []).forEach((r) => {
        const item = document.createElement("div");
        item.className = "venue-item public-beer-result";
        const place = r.zone ? zoneDisplayLabel(r.zone) : "";
        item.innerHTML = `<span class="public-beer-result-text">${escapeHtml(r.name || "")} — ${escapeHtml(r.brewery || "")}${place ? ` · ${escapeHtml(place)}` : ""}</span>
          <button class="untappd-link-btn" title="${escapeHtml(T("app_open_in_untappd"))}">${ICON_LINK}</button>`;
        item.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(r.beerId, e));
        item.addEventListener("click", () => selectFestivalMapSearchResult({ brewery: r.stand, zone: r.zone }));
        resultsEl.appendChild(item);
      });
      resultsEl.classList.toggle("hidden", resultsEl.children.length === 0);
    }, 300);
  }

  $("festival-map-search-input").addEventListener("input", (e) => {
    // Folded (diacritic-stripped, lowercased) so "Stu Mostow" finds the
    // real "Browar Stu Mostów" and vice versa, regardless of which form
    // (if either) the typed query or the catalog name happens to use -
    // see foldDiacritics's own docstring.
    const q = foldDiacritics(e.target.value.trim().toLowerCase());
    const resultsEl = $("festival-map-search-results");
    resultsEl.innerHTML = "";
    schedulePublicBeerSearch(e.target.value);
    if (!q) {
      resultsEl.classList.add("hidden");
      return;
    }
    const rawMatches = allMapBreweries().filter((it) => {
      const alias = BREWERY_DISPLAY_ALIASES[it.brewery];
      return foldDiacritics(it.brewery.toLowerCase()).includes(q)
        || (alias && foldDiacritics(alias.toLowerCase()).includes(q));
    });
    // A collab-credit alias (realBrewery set - see allMapBreweries) whose
    // real stand already matched on its own would just list the same stand
    // twice ("Browar Monsters" next to "Browar Monsters / Sick Boy Brewing"),
    // so only keep aliases that reach a stand that isn't already a result.
    const matchedStands = new Set(rawMatches.filter((it) => !it.realBrewery).map((it) => it.brewery));
    const matches = rawMatches
      .filter((it) => !it.realBrewery || !matchedStands.has(it.realBrewery))
      .slice(0, 20);
    matches.forEach((match) => {
      const item = document.createElement("div");
      item.className = "venue-item";
      const place = match.zone ? zoneDisplayLabel(match.zone) : match.category;
      item.textContent = match.realBrewery
        ? T("app_map_match_at_stand", { brewery: match.brewery, place, stand: match.realBrewery })
        : `${match.brewery} — ${place}`;
      item.addEventListener("click", () => selectFestivalMapSearchResult(match));
      resultsEl.appendChild(item);
    });
    resultsEl.classList.toggle("hidden", matches.length === 0);
  });

  // Tapping anywhere on a collapsed overview card opens its full-screen
  // detail view (wired per-card in renderFestivalMap, since cards are
  // built dynamically now); in edit mode the card is a drag surface
  // instead, so the tap-to-open behavior is disabled there.

  function updateFestivalMapEditIcon() {
    const btn = $("festival-map-edit-btn");
    btn.innerHTML = festivalMapEditMode
      ? '<svg class="icon"><use href="#icon-check"/></svg>'
      : '<svg class="icon"><use href="#icon-edit"/></svg>';
    btn.setAttribute("aria-label", T(festivalMapEditMode ? "app_save" : "app_edit"));
  }

  $("festival-map-edit-btn").addEventListener("click", () => {
    cancelMapDrag();
    closeFestivalMapDetail(); // edit mode uses its own scrollable overview layout, not the single-zone detail view
    festivalMapEditMode = !festivalMapEditMode;
    document.body.classList.toggle("map-edit-mode", festivalMapEditMode);
    updateFestivalMapEditIcon();
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
    renderFestivalMap();
  });

  // Pointer-Events-based drag (not native HTML5 drag-and-drop, which never
  // fires on touch in Telegram's mobile WebView - the primary target here).
  // The dragged pill itself becomes a `position:fixed` element that follows
  // the pointer, and a placeholder slot takes its place in the list -
  // dragging the placeholder between containers (via plain DOM insertBefore)
  // is what makes the other pills visibly shift out of the way as you move,
  // Telegram-chat-reorder style, animated with a FLIP transform pass.
  let mapDrag = null; // { brewery, pill, placeholder, lastZone, lastSide, lastContainer, lastIndex }
  let mapDragTimeoutHandle = null;

  // Defensive cleanup for a drag session that never got a matching
  // pointerup/pointercancel (seen in practice - a stray extra pointerdown
  // before the previous drag finished used to overwrite `mapDrag`, leaking
  // the old floating pill/placeholder on screen forever and leaving its
  // listeners attached).
  function cancelMapDrag() {
    document.querySelectorAll(".map-zone.drag-over").forEach((el) => el.classList.remove("drag-over"));
    if (mapDrag) {
      mapDrag.pill.remove();
      mapDrag.placeholder.remove();
    }
    document.removeEventListener("pointermove", onMapPillPointerMove);
    document.removeEventListener("pointerup", onMapPillPointerUp);
    document.removeEventListener("pointercancel", onMapPillPointerUp);
    clearTimeout(mapDragTimeoutHandle);
    mapDragTimeoutHandle = null;
    mapDrag = null;
    mapDragActive = false;
  }

  // Animates the pills inside #festival-map-zones sliding into their new
  // positions after `mutate` reorders the DOM (FLIP: record rects, mutate,
  // measure the delta, animate away from it). Scoped to all 4 zones so
  // cross-zone moves animate correctly too.
  function flipReorder(mutate) {
    const pills = document.querySelectorAll("#festival-map-zones .brewery-pill");
    const firstRects = new Map();
    pills.forEach((el) => firstRects.set(el, el.getBoundingClientRect()));
    mutate();
    firstRects.forEach((first, el) => {
      if (!el.isConnected) return;
      const last = el.getBoundingClientRect();
      const dx = first.left - last.left;
      const dy = first.top - last.top;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
      el.style.transition = "none";
      el.style.transform = `translate(${dx}px, ${dy}px)`;
      el.getBoundingClientRect(); // force layout so the transform above is committed before transitioning away from it
      requestAnimationFrame(() => {
        el.style.transition = "transform 0.18s ease";
        el.style.transform = "";
      });
    });
  }

  // Moves the placeholder to sit after `index` real pills within
  // `container` (native insertBefore relocates it from wherever it
  // currently is, so this works the same whether it's already in this
  // container or arriving from a different one).
  function placePlaceholderAt(container, index) {
    const { placeholder } = mapDrag;
    const siblings = [...container.children].filter((el) => el !== placeholder);
    const refNode = siblings[index] || null;
    if (refNode) container.insertBefore(placeholder, refNode);
    else container.appendChild(placeholder);
  }

  // Where a pill's own container currently is - one of the 4 perimeter
  // sides, or (if it's sitting inside a .map-island-pills box) an island,
  // identified by its parent .map-island's data-island-id.
  function containerLocation(container) {
    const islandBox = container.closest(".map-island-pills");
    if (islandBox) return { side: "island", islandId: islandBox.parentElement.dataset.islandId };
    return { side: MAP_SIDES.find((s) => container.classList.contains(`perimeter-${s}`)), islandId: null };
  }

  function onMapPillPointerDown(e) {
    if (!festivalMapEditMode) return;
    // A previous drag that never got a matching pointerup/pointercancel
    // (happens in practice - e.g. the WebView swallows it) used to just
    // block every future pointerdown forever once `mapDrag` was left
    // non-null. Self-heal instead: treat a fresh pointerdown as proof the
    // old gesture is over and clean it up before starting the new one.
    if (mapDrag) cancelMapDrag();
    e.preventDefault();
    const pill = e.currentTarget;
    const rect = pill.getBoundingClientRect();
    const originalContainer = pill.parentElement;
    const originalZone = originalContainer.closest(".map-zone").dataset.zone;
    const originalIndex = [...originalContainer.children].indexOf(pill);

    const placeholder = document.createElement("div");
    placeholder.className = "brewery-pill-placeholder";
    originalContainer.insertBefore(placeholder, pill);

    // Best-effort only: pointer capture keeping events targeted at `pill`
    // is what dragging into a *different* zone used to rely on, and a
    // capture failure (seen in practice in Telegram's WebView) silently
    // broke cross-zone drags entirely, since a pill-scoped listener stops
    // getting events once the pointer leaves it. Listening on `document`
    // below is the real fix - capture is now just a minor assist, so a
    // failure here is harmless.
    try { pill.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    mapDragActive = true;
    pill.classList.add("map-pill-floating");
    pill.style.width = `${rect.width}px`;
    pill.style.left = `${e.clientX}px`;
    pill.style.top = `${e.clientY}px`;
    document.body.appendChild(pill);

    const originalLoc = containerLocation(originalContainer);
    mapDrag = {
      brewery: pill.dataset.brewery,
      pill,
      placeholder,
      lastZone: originalZone,
      lastSide: originalLoc.side,
      lastIslandId: originalLoc.islandId,
      lastContainer: originalContainer,
      lastIndex: originalIndex,
    };
    document.addEventListener("pointermove", onMapPillPointerMove);
    document.addEventListener("pointerup", onMapPillPointerUp);
    document.addEventListener("pointercancel", onMapPillPointerUp);
    // Safety net for the exact "WebView swallows it" case the comment
    // above already works around on the NEXT pointerdown - but until that
    // happens, mapDragActive stays stuck true forever, which blocks
    // fetchFestivalMap's own poll-driven refresh (`if (mapDragActive)
    // return;`) from ever running again. Confirmed live: an interrupted
    // drag left a stray .brewery-pill-placeholder sitting in the pill's
    // old spot AND the real pill floating, detached, invisible in
    // document.body - permanently, since nothing ever polled fresh data
    // to overwrite it, until the next manual drag attempt happened to
    // self-heal it. This timeout guarantees that happens within seconds
    // instead of depending on the viewer trying to drag something else.
    clearTimeout(mapDragTimeoutHandle);
    mapDragTimeoutHandle = setTimeout(cancelMapDrag, 8000);
  }

  // Finds which zone the pointer is over, then either a precise hit on one
  // of that zone's island boxes, or - failing that - which of the 4
  // independent perimeter side-lists (top/left/right/bottom) is closest by
  // rect distance, so dropping in the empty decorative middle still
  // resolves to whichever side is nearest instead of missing entirely.
  // Island hit-testing runs first and is exact (not nearest-by-distance)
  // so dropping into a small island box next to the middle never gets
  // stolen by a "nearer" perimeter side.
  function findDropTarget(x, y) {
    for (const zone of MAP_ZONES) {
      const zoneEl = findZoneEl(zone);
      const rect = zoneEl.getBoundingClientRect();
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        for (const box of zoneEl.querySelectorAll(".map-island-pills")) {
          const bRect = box.getBoundingClientRect();
          if (x >= bRect.left && x <= bRect.right && y >= bRect.top && y <= bRect.bottom) {
            return { zone, zoneEl, side: "island", islandId: box.parentElement.dataset.islandId, sideContainer: box };
          }
        }
        const sections = perimeterSections(zoneEl);
        let side = null;
        let sideContainer = null;
        let bestDist = Infinity;
        MAP_SIDES.forEach((s) => {
          const sRect = sections[s].getBoundingClientRect();
          const dx = Math.max(sRect.left - x, 0, x - sRect.right);
          const dy = Math.max(sRect.top - y, 0, y - sRect.bottom);
          const dist = Math.hypot(dx, dy);
          if (dist < bestDist) {
            bestDist = dist;
            side = s;
            sideContainer = sections[s];
          }
        });
        return { zone, zoneEl, side, islandId: null, sideContainer };
      }
    }
    return null;
  }

  // Each side is its own independent, directly-rendered list (see
  // renderPerimeterPills), so this only ever needs to look at that one
  // container's children - no cross-section index bookkeeping. Top/bottom
  // read left-to-right so "before/after" compares x; left/right are
  // vertical columns so it compares y.
  function insertionIndex(container, x, y, excludeEl, horizontal) {
    const pills = [...container.children].filter((el) => el !== excludeEl);
    let closestIndex = pills.length;
    let closestDist = Infinity;
    pills.forEach((el, i) => {
      const rect = el.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const dist = Math.hypot(x - cx, y - cy);
      if (dist < closestDist) {
        closestDist = dist;
        closestIndex = (horizontal ? x < cx : y < cy) ? i : i + 1;
      }
    });
    return closestIndex;
  }

  function onMapPillPointerMove(e) {
    if (!mapDrag) return;
    mapDrag.pill.style.left = `${e.clientX}px`;
    mapDrag.pill.style.top = `${e.clientY}px`;
    document.querySelectorAll(".map-zone.drag-over").forEach((el) => el.classList.remove("drag-over"));
    const target = findDropTarget(e.clientX, e.clientY);
    if (!target) return; // hovering outside any zone - leave the placeholder at its last valid slot
    target.zoneEl.classList.add("drag-over");
    const horizontal = target.side === "top" || target.side === "bottom" || target.side === "island";
    const index = insertionIndex(target.sideContainer, e.clientX, e.clientY, mapDrag.placeholder, horizontal);
    if (target.sideContainer === mapDrag.lastContainer && index === mapDrag.lastIndex) return;
    mapDrag.lastZone = target.zone;
    mapDrag.lastSide = target.side;
    mapDrag.lastIslandId = target.islandId;
    mapDrag.lastContainer = target.sideContainer;
    mapDrag.lastIndex = index;
    flipReorder(() => placePlaceholderAt(target.sideContainer, index));
  }

  // Dragging the "+ Прохід" chip onto a side/island inserts an empty slot (a
  // walkway) there - same drag machinery as a stand, minus the stand: the
  // floating chip is a throwaway and nothing leaves its original place.
  function onGapSourcePointerDown(e) {
    if (!festivalMapEditMode) return;
    if (mapDrag) cancelMapDrag();
    e.preventDefault();
    const chip = document.createElement("div");
    chip.className = "brewery-pill map-pill-floating";
    chip.textContent = T("app_map_gap");
    chip.style.width = "96px";
    chip.style.left = `${e.clientX}px`;
    chip.style.top = `${e.clientY}px`;
    document.body.appendChild(chip);
    const placeholder = document.createElement("div");
    placeholder.className = "brewery-pill-placeholder";
    mapDragActive = true;
    mapDrag = {
      gap: true, brewery: null, pill: chip, placeholder,
      lastZone: null, lastSide: null, lastIslandId: null, lastContainer: null, lastIndex: 0,
    };
    document.addEventListener("pointermove", onMapPillPointerMove);
    document.addEventListener("pointerup", onMapPillPointerUp);
    document.addEventListener("pointercancel", onMapPillPointerUp);
    clearTimeout(mapDragTimeoutHandle);
    mapDragTimeoutHandle = setTimeout(cancelMapDrag, 8000);
  }

  async function dropMapGap({ lastZone, lastSide, lastIslandId, lastIndex }) {
    if (!lastZone) return;
    const sides = state.festivalMap.zones[lastZone];
    const target = lastSide === "island"
      ? (sides.islands[lastIslandId] || {}).breweries
      : sides[lastSide];
    if (!target || lastIndex >= target.length) {
      renderFestivalMap(); // a gap past the last stand separates nothing
      return;
    }
    target.splice(lastIndex, 0, null);
    renderFestivalMap();
    await apiPost("/api/checkin/festival_map/gap_insert", {
      zone: lastZone, side: lastSide, index: lastIndex, islandId: lastSide === "island" ? lastIslandId : null,
    });
  }

  async function onMapPillPointerUp() {
    if (!mapDrag) return;
    if (mapDrag.gap) {
      const drop = mapDrag;
      cancelMapDrag();
      await dropMapGap(drop);
      return;
    }
    const { brewery, lastZone, lastSide, lastIslandId, lastIndex } = mapDrag;
    cancelMapDrag();

    // Optimistic local move so nothing snaps back while the request is in
    // flight, then let the next poll reconcile with the server. Mirrors
    // festival_map.py's own move_brewery: dropping onto an already-empty
    // slot (or past the current end) CLAIMS that exact position - nothing
    // shifts, and wherever the brewery used to be becomes an empty slot
    // (`null`) rather than being spliced away, so no OTHER row's
    // alignment changes just because this one moved. Dropping onto a
    // REAL, occupied position still reorders normally (shifts, and fully
    // removes the old spot) - same as this always did before slots
    // existed. Keeping this decision in sync with the backend means the
    // optimistic render already matches what the next poll will confirm,
    // instead of visibly "jumping" once the server's real state arrives.
    // An "island" target is just a second kind of list this can point at
    // (targetSides.islands[id].breweries instead of targetSides[side]) -
    // same claim-vs-reorder logic either way.
    const targetSides = state.festivalMap.zones[lastZone];
    const target = lastSide === "island"
      ? (targetSides.islands[lastIslandId] || (targetSides.islands[lastIslandId] = { label: "", breweries: [] })).breweries
      : (targetSides[lastSide] || (targetSides[lastSide] = []));
    const claimSlot = lastIndex >= target.length || !target[lastIndex];

    MAP_ZONES.forEach((zone) => {
      const zoneSides = state.festivalMap.zones[zone];
      if (!zoneSides) return;
      MAP_SIDES.forEach((side) => {
        const list = zoneSides[side];
        if (!list) return;
        const idx = list.indexOf(brewery);
        if (idx === -1) return;
        if (claimSlot) list[idx] = null;
        else list.splice(idx, 1);
      });
      Object.values(zoneSides.islands || {}).forEach((island) => {
        const idx = island.breweries.indexOf(brewery);
        if (idx === -1) return;
        if (claimSlot) island.breweries[idx] = null;
        else island.breweries.splice(idx, 1);
      });
    });
    if (claimSlot) {
      while (target.length <= lastIndex) target.push(null);
      target[lastIndex] = brewery;
    } else {
      target.splice(lastIndex, 0, brewery);
    }
    renderFestivalMap();

    await apiPost("/api/checkin/festival_map/move", {
      brewery, zone: lastZone, side: lastSide, index: lastIndex,
      islandId: lastSide === "island" ? lastIslandId : null,
    });
  }

  // ---- Settings screen (⚙️) - quick on/off for the three watch features ----
  // Each toggle reads/writes that feature's own existing config directly;
  // this screen doesn't own any state itself, just surfaces the three
  // already-existing on/off switches in one place.

  $("settings-bar-btn").addEventListener("click", () => showScreen("settings"));

  async function fetchSettingsStatus() {
    $("settings-status").textContent = T("app_loading");
    const [autotoast, festivalWatch, commentWatch, festivalMode, festivalList, commandFlags] = await Promise.all([
      apiPost("/api/checkin/autotoast/status", {}),
      apiPost("/api/checkin/festival_watch/get", {}),
      apiPost("/api/checkin/comment_watch/get", {}),
      apiPost("/api/checkin/festival_mode/get", {}),
      apiPost("/api/checkin/festival/list", {}),
      apiPost("/api/checkin/command_flags/get", {}),
    ]);
    state.autoToastAvailable = !!(autotoast.ok && autotoast.data.available);
    updateAutoToastRowVisibility();
    state.commandFlagsAvailable = !!(commandFlags.ok && commandFlags.data.available);
    updateCommandFlagsRowVisibility();
    state.festivalSwitchAvailable = !!(festivalList.ok && festivalList.data.available);
    updateFestivalSwitchRowVisibility();
    $("settings-autotoast-toggle").checked = !!(autotoast.ok && autotoast.data.enabled);
    $("settings-festivalwatch-toggle").checked = !!(festivalWatch.ok && festivalWatch.data.enabled);
    $("settings-commentwatch-toggle").checked = !!(commentWatch.ok && commentWatch.data.enabled);
    $("settings-festivalmode-toggle").checked = !!(festivalMode.ok && festivalMode.data.enabled);
    $("settings-status").textContent = "";
  }

  $("settings-autotoast-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await apiPost("/api/checkin/autotoast/toggle", { enabled: e.target.checked });
  });
  $("settings-festivalwatch-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await apiPost("/api/checkin/festival_watch/toggle", { enabled: e.target.checked });
  });
  $("settings-commentwatch-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await apiPost("/api/checkin/comment_watch/toggle", { enabled: e.target.checked });
  });
  $("settings-festivalmode-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    state.festivalMode = e.target.checked;
    applyFestivalModeUI();
    await apiPost("/api/checkin/festival_mode/toggle", { enabled: e.target.checked });
  });

  // Settings-screen "forget my test check-ins" - clears this user's own
  // completedBy markers (see checkin_queue.reset_user), so a pre-festival
  // test check-in through the queue stops being reported as "already had
  // this at the festival" the next time they search for that beer.
  // Deliberately does NOT bring anything back into the active queue view
  // (reset_user moves it to hiddenBy instead of just clearing it) - beers
  // hidden on purpose are left untouched either way.
  $("settings-row-queue-reset").addEventListener("click", () => {
    const doReset = async () => {
      const { ok, data } = await apiPost("/api/checkin/queue/reset_personal", {});
      if (!ok || !data.ok) {
        notify(T("app_reset_failed"));
        return;
      }
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      notify(data.reset > 0
        ? T("app_reset_done", { n: data.reset })
        : T("app_reset_none"));
    };
    if (tg && tg.showConfirm) {
      tg.showConfirm(T("app_reset_confirm"), (confirmed) => { if (confirmed) doReset(); });
    } else if (confirm(T("app_reset_confirm"))) {
      doReset();
    }
  });

  // ---- Events screen (🔔) - recent auto-toast/comment/novelty activity ----
  // Purely a read-only glance-back over event_log.py's rolling per-owner
  // log - no Untappd/quota cost, just a local file read.

  $("events-bar-btn").addEventListener("click", () => showScreen("events"));

  function timeAgo(unixSeconds) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - unixSeconds) / 60));
    if (mins < 1) return T("app_time_just_now");
    if (mins < 60) return T("app_time_min_ago", { n: mins });
    const hours = Math.round(mins / 60);
    if (hours < 24) return T("app_time_hours_ago", { n: hours });
    return T("app_time_days_ago", { n: Math.round(hours / 24) });
  }

  // Event rows get an SVG icon per kind (see event_log.py's add_event). Older
  // stored events still carry an emoji prefix baked into their text (🆕/💬/
  // 🍻 etc.) - stripLeadingEmoji drops it so the icon isn't doubled.
  const EVENT_KIND_ICON = {
    toast: `<svg class="icon event-kind-icon"><use href="#icon-cheers"/></svg>`,
    comment: `<svg class="icon event-kind-icon"><use href="#icon-chat"/></svg>`,
    novelty: `<svg class="icon icon-filled event-kind-icon"><use href="#icon-sparkle"/></svg>`,
  };
  function stripLeadingEmoji(text) {
    return text.replace(/^(?:\p{Extended_Pictographic}|\u{1F195}|\u{FE0F}|\u{200D}|\s)+/u, "");
  }

  async function fetchEvents() {
    $("events-status").textContent = T("app_loading");
    $("events-list").innerHTML = "";
    const { ok, data } = await apiPost("/api/checkin/events/get", {});
    if (!ok) {
      $("events-status").textContent = T("app_load_failed");
      return;
    }
    const events = data.events || [];
    $("events-status").textContent = events.length ? "" : T("app_events_empty");
    const listEl = $("events-list");
    events.forEach((ev) => {
      const row = document.createElement("div");
      row.className = "event-row-wrap";
      const mainRow = document.createElement("div");
      mainRow.className = "result-row event-row";
      mainRow.innerHTML = `
        <div class="event-row-main">
          <div class="event-row-text">${EVENT_KIND_ICON[ev.kind] || ""}${escapeHtml(stripLeadingEmoji(ev.text || ""))}</div>
          <div class="event-row-time">${timeAgo(ev.at)}</div>
        </div>
        <div class="row-actions">
          ${ev.beerId ? `<button class="untappd-link-btn" title="${escapeHtml(T("app_open_in_untappd"))}">${ICON_LINK}</button>` : ""}
          ${ev.kind === "comment" && ev.checkinId ? `<button class="event-reply-toggle-btn" title="${escapeHtml(T("app_reply"))}">${ICON_CHAT}</button>` : ""}
        </div>
      `;
      if (ev.beerId) {
        mainRow.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(ev.beerId, e));
      }
      row.appendChild(mainRow);

      if (ev.kind === "comment" && ev.checkinId) {
        const replyBox = document.createElement("div");
        replyBox.className = "event-reply-box hidden";
        replyBox.innerHTML = `
          <input type="text" class="event-reply-input" placeholder="@${escapeHtml(ev.username || "")}, …" maxlength="140">
          <button class="primary-btn event-reply-send-btn">${escapeHtml(T("app_send"))}</button>
        `;
        row.appendChild(replyBox);

        mainRow.querySelector(".event-reply-toggle-btn").addEventListener("click", () => {
          replyBox.classList.toggle("hidden");
          if (!replyBox.classList.contains("hidden")) {
            replyBox.querySelector(".event-reply-input").focus();
          }
        });

        const sendReply = async () => {
          const input = replyBox.querySelector(".event-reply-input");
          const text = input.value.trim();
          if (!text) return;
          const btn = replyBox.querySelector(".event-reply-send-btn");
          btn.disabled = true;
          const { ok: replyOk, data: replyData } = await apiPost("/api/checkin/events/reply", {
            checkinId: ev.checkinId, username: ev.username, text,
          });
          btn.disabled = false;
          if (replyOk && replyData.ok) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
            input.value = "";
            replyBox.classList.add("hidden");
          } else {
            alert(replyData && replyData.error === "not_connected" ? T("app_not_connected") : T("app_reply_failed"));
          }
        };
        replyBox.querySelector(".event-reply-send-btn").addEventListener("click", sendReply);
        replyBox.querySelector(".event-reply-input").addEventListener("keydown", (e) => {
          if (e.key === "Enter") sendReply();
        });
      }

      listEl.appendChild(row);
    });

    markEventsSeen(events);
  }

  // Unread indicator on the "🔔" button - a small per-viewer convenience,
  // so localStorage is fine here (unlike everything else this app stores
  // server-side): worst case it comes back empty on a new device/browser
  // and the dot just doesn't show until the next fetch, no data is lost.
  const LAST_SEEN_EVENTS_KEY = "checkinHelper.lastSeenEventsAt";

  function markEventsSeen(events) {
    const newest = events.length ? Math.max(...events.map((e) => e.at || 0)) : Date.now() / 1000;
    try { localStorage.setItem(LAST_SEEN_EVENTS_KEY, String(newest)); } catch (e) { /* private mode etc - ignore */ }
    $("events-bar-btn").classList.remove("has-unread");
  }

  async function checkForUnreadEvents() {
    const { ok, data } = await apiPost("/api/checkin/events/get", {});
    if (!ok) return;
    const events = data.events || [];
    if (!events.length) return;
    let lastSeen = 0;
    try { lastSeen = parseFloat(localStorage.getItem(LAST_SEEN_EVENTS_KEY)) || 0; } catch (e) { /* ignore */ }
    const newest = Math.max(...events.map((e) => e.at || 0));
    if (newest > lastSeen) {
      $("events-bar-btn").classList.add("has-unread");
    }
  }

  // ---- Search field clear buttons (loaded once on start) ----

  [
    "search-input",
    "wishlist-search-input",
    "session-search-input",
    "autotoast-search-input",
    "festival-watch-search-input",
    "festival-watch-extra-search-input",
    "festival-map-search-input",
    "badges-search-input",
    "venue-search-input",
  ].forEach(addSearchClearButton);

  // ---- Maintenance splash (loaded once on start, checked first) ----
  // No initData/auth needed (see handle_maintenance_get) - deliberately
  // fires before every other "loaded once on start" call below, so the
  // splash covers the screen as early as possible if maintenance is on.
  // Those other calls still fire and populate their own state underneath
  // regardless (harmless - registering listeners/fetching data is not
  // user-visible on its own), the overlay just visually blocks reaching
  // any of it while shown.
  apiPost("/api/checkin/maintenance/get", {}).then(({ ok, data }) => {
    if (ok && data && data.enabled) {
      if (data.message) $("maintenance-message").textContent = data.message;
      $("maintenance-overlay").classList.remove("hidden");
    }
  });

  // ---- Usage badge (loaded once on start) ----

  // Shared by both the MCP quota badge and the owner-only direct-API one
  // below - same ring math, different element ids and label.
  function renderUsageBadge(wrapId, badgeId, ringId, remaining, limit, label) {
    $(badgeId).textContent = `${remaining}/${limit} ${label}`;
    // Ring shows the share of quota still left (r=9 in the 24-unit box).
    const circ = 2 * Math.PI * 9;
    const left = limit ? Math.max(0, Math.min(1, remaining / limit)) : 0;
    $(ringId).setAttribute("stroke-dasharray", `${(circ * left).toFixed(1)} ${circ.toFixed(1)}`);
    $(wrapId).classList.remove("hidden");
  }

  Promise.all([apiPost("/api/checkin/usage", {}), i18nReady]).then(([{ ok, data }]) => {
    if (ok && data.remaining != null) {
      renderUsageBadge("usage-wrap", "usage-badge", "usage-ring-fill", data.remaining, data.limit, T("app_usage_api"));
    }
    // Owner-only (see handle_usage) - the separate direct-Untappd-API quota
    // background sync loops fall back to, absent for every other viewer.
    if (ok && data.directUsage && data.directUsage.remaining != null) {
      renderUsageBadge(
        "direct-usage-wrap", "direct-usage-badge", "direct-usage-ring-fill",
        data.directUsage.remaining, data.directUsage.limit, T("app_usage_direct"),
      );
    }
    if (ok && data.lastVenue) {
      state.lastVenue = data.lastVenue;
    }
  });

  // ---- Festival mode (loaded once on start) - see applyFestivalModeUI ----
  apiPost("/api/checkin/festival_mode/get", {}).then(({ ok, data }) => {
    state.festivalMode = !!(ok && data.enabled);
    applyFestivalModeUI();
  });

  // ---- Session color map (loaded once on start) ----
  // Needed so sourceBadge() can pick the right dot for a session whose raw
  // name isn't literally "yellow"/"blue"/etc (e.g. "friday"/"saturday") -
  // without this, every such beer would fall back to the generic 🎪 and all
  // sessions would look the same in search results.
  apiPost("/api/checkin/festival/meta", {}).then(({ ok, data }) => {
    if (ok) {
      (data.sessions || []).forEach((s) => { sessionColorMap[s.session] = s.color; });
    }
  });

  // ---- Unread events indicator (loaded once on start) ----
  checkForUnreadEvents();

  // ---- Deep link into the map (loaded once on start) ----
  // A festival-novelty Telegram notification's "Відкрити на карті" button
  // (see webapp_server.py's _notify_festival_novelty) opens the Mini App
  // with these two query params instead of a bare /checkin - jump straight
  // to that brewery's stand the same way an in-app map search result does
  // (openFestivalMapDetail's own highlight), skipping the normal idle
  // search screen entirely for this one launch.
  const deepLinkParams = new URLSearchParams(window.location.search);
  const deepLinkZone = deepLinkParams.get("mapZone");
  const deepLinkBrewery = deepLinkParams.get("mapBrewery");
  if (deepLinkZone && deepLinkBrewery) {
    showScreen("festival-map");
    fetchFestivalMap().then(() => openFestivalMapDetail(deepLinkZone, deepLinkBrewery));
  } else if (PUBLIC_MODE) {
    showScreen("festival-map");
  }
})();
