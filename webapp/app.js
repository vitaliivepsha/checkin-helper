(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }
  const initData = tg ? tg.initData : "";

  const NOT_CONNECTED_MSG = "Спершу підключіть свій Untappd — напишіть /connect_untappd боту в приваті.";
  const DEFAULT_LABEL_URL = "https://assets.untappd.com/site/assets/images/temp/badge-beer-default.png";

  // Escape valve for the Untappd MCP quota (100/rolling-hour per access
  // token, confirmed live via get_untappd_api_usage) - opening the beer's
  // real Untappd page costs nothing on our side, so it's always available
  // even when the quota's tight or check_in itself is failing.
  function openUntappdBeer(beerId, e) {
    if (e) e.stopPropagation();
    const url = `https://untappd.com/beer/${beerId}`;
    if (tg && tg.openLink) {
      tg.openLink(url);
    } else {
      window.open(url, "_blank");
    }
  }

  const state = {
    selectedBeer: null,
    rating: 4,
    venues: null,
    selectedVenue: null,
    lastVenue: null,     // remembered from the previous check-in - festival venue doesn't change mid-day
    lastKnownLocation: null, // cached after a successful "Локації поруч" tap - reused to geo-bias text search
    lastVenueSearch: null,   // {type:"nearby",lat,lng} | {type:"query",query} - replayed when a filter checkbox toggles
    queue: [],          // shared, server-backed - everyone in the group sees the same list
    currentSession: null, // which session's drill-down list is currently open
    origin: "search",    // where to return after rate/confirm: "search", "queue" or "session-beers"
    queueItemId: null,   // the server's item id, not an array index (another phone can remove items)
    badgesRaw: [],       // last /api/checkin/badges/get fetch - re-filtered/sorted client-side, no re-fetch needed
    badgesSort: "level_desc",
    selectedBadge: null, // drill-down target for screen-badge-detail
  };

  function $(id) { return document.getElementById(id); }

  let queuePollHandle = null;
  let statsPollHandle = null;
  function showScreen(name) {
    document.querySelectorAll(".screen").forEach((el) => el.classList.remove("active"));
    $("screen-" + name).classList.add("active");
    if (name === "queue") {
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
      fetchBadgeStats();
    }
    if (name === "badge-detail") {
      renderBadgeDetail();
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
    const resp = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initData,
      },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json().catch(() => ({}));
    return { ok: resp.ok, status: resp.status, data };
  }

  // ---- Shared queue (solves "which beer is in which glass / who brought what") ----
  // Server-backed so the whole group sees the same list on every phone.

  async function fetchQueue() {
    const { ok, data } = await apiPost("/api/checkin/queue/list", {});
    state.queue = ok ? (data.items || []) : [];
    state.queueTotal = ok ? (data.total || 0) : 0;
    renderQueueList();
  }

  async function addToQueue(beer, btn) {
    const { ok, data } = await apiPost("/api/checkin/queue/add", beer);
    if (ok && data.ok) {
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      if (btn) {
        const original = btn.textContent;
        btn.textContent = "✓";
        setTimeout(() => { btn.textContent = original; }, 1000);
      }
      updateQueueCountOnly();
    } else {
      alert("Не вдалося додати у чергу. Спробуйте ще раз.");
    }
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

  function renderQueueList() {
    const listEl = $("queue-list");
    listEl.innerHTML = "";
    $("queue-count").textContent = String(state.queue.length);
    $("queue-bar-btn").classList.toggle("hidden", (state.queueTotal || 0) === 0);
    $("queue-status").textContent = state.queue.length
      ? ""
      : (state.queueTotal
        ? "Усе, що зараз у черзі, ви вже відмітили як випите."
        : "Черга порожня — додайте пиво кнопкою «+» у результатах пошуку.");
    state.queue.forEach((beer, idx) => {
      const row = document.createElement("div");
      row.className = "result-row queue-row" + (beer.hadIt ? " had-it" : "");
      const addedBy = beer.addedBy && beer.addedBy.name ? beer.addedBy.name : "?";
      row.innerHTML = `
        <div class="queue-number">${idx + 1}</div>
        <div class="result-main">
          <div class="result-name">${beer.hadIt ? '<span class="badge">✅</span>' : ""}<span class="result-name-text">${escapeHtml(beer.name || "")}</span></div>
          ${metaLine(beer.brewery)}
          ${metaLine(beer.style)}
          <div class="result-meta">додав(-ла) ${escapeHtml(addedBy)}</div>
        </div>
        <div class="row-actions">
          <button class="queue-remove-btn" data-id="${beer.id}">✕</button>
          <button class="untappd-link-btn" title="Відкрити в Untappd">🔗</button>
        </div>`;
      row.addEventListener("click", (e) => {
        if (e.target.closest(".queue-remove-btn") || e.target.closest(".untappd-link-btn")) return;
        selectBeer(beer, { origin: "queue", queueItemId: beer.id });
      });
      row.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(beer.beerId, e));
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

  $("queue-bar-btn").addEventListener("click", () => showScreen("queue"));

  $("queue-clear-btn").addEventListener("click", async () => {
    const { ok, data } = await apiPost("/api/checkin/queue/clear", {});
    if (ok && data.ok && tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    fetchQueue();
  });

  updateQueueCountOnly();

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
  const SESSION_NAMES = { yellow: "Жовта", blue: "Синя", red: "Червона", green: "Зелена" };

  function sessionLabel(session) {
    if (SESSION_NAMES[session]) return `${SESSION_NAMES[session]} сесія`;
    const name = String(session || "");
    return `Сесія «${name.charAt(0).toUpperCase()}${name.slice(1)}»`;
  }

  $("stats-bar-btn").addEventListener("click", () => showScreen("stats"));

  async function fetchFestivalStats() {
    const { ok, data } = await apiPost("/api/checkin/festival/stats", {});
    if (!ok) {
      $("stats-summary").textContent = "Не вдалося завантажити прогрес.";
      return;
    }
    const pct = data.total ? Math.round((100 * data.checked) / data.total) : 0;
    $("stats-summary").textContent = `${data.checked} / ${data.total} спробувано (${pct}%)`;
    const listEl = $("stats-sessions");
    listEl.innerHTML = "";
    (data.sessions || []).forEach((s) => {
      const row = document.createElement("div");
      row.className = "venue-item";
      const sPct = s.total ? Math.round((100 * s.checked) / s.total) : 0;
      row.innerHTML = `
        <div>${SESSION_EMOJI[s.color] || "🎪"} ${sessionLabel(s.session)} — ${s.checked} / ${s.total} (${sPct}%)</div>
        <div class="stats-bar"><div class="stats-bar-fill" style="width:${sPct}%"></div></div>`;
      row.addEventListener("click", () => openSessionBeers(s.session, s.color));
      listEl.appendChild(row);
    });
  }

  // ---- Badge progress (real Untappd style/country badges, computed from
  // had_it_index's already-synced beer/style/country history - see
  // badge_stats.py) ----

  $("badges-bar-btn").addEventListener("click", () => showScreen("badges"));

  async function fetchBadgeStats() {
    $("badges-status").textContent = "Завантажую…";
    const { ok, data } = await apiPost("/api/checkin/badges/get", {});
    if (!ok) {
      $("badges-status").textContent = "Не вдалося завантажити прогрес бейджів.";
      return;
    }
    state.badgesRaw = data.badges || [];
    renderBadgesList();
  }

  // Sorts/filters the already-fetched list client-side - a fresh fetch per
  // toggle would be pointless round-tripping for a fixed ~170-row list that
  // doesn't change mid-session.
  function sortedBadges() {
    const list = state.badgesRaw.slice();
    if (state.badgesSort === "level_asc") {
      list.sort((a, b) => a.level - b.level || a.pct - b.pct);
    } else if (state.badgesSort === "alpha") {
      list.sort((a, b) => a.name.localeCompare(b.name));
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

  function renderBadgesList() {
    const query = $("badges-search-input").value.trim().toLowerCase();
    const badges = sortedBadges().filter((b) => badgeMatchesQuery(b, query));
    if (!state.badgesRaw.length) {
      $("badges-status").textContent = "Дані ще накопичуються — почни випивати щось нове 🙂";
    } else {
      $("badges-status").textContent = badges.length ? "" : "Нічого не знайдено.";
    }
    const listEl = $("badges-list");
    listEl.innerHTML = "";
    badges.forEach((b) => {
      const target = b.nextThreshold ?? b.current;
      const pct = Math.max(0, Math.min(100, b.pct));
      const row = document.createElement("div");
      row.className = "venue-item badge-row" + (b.done ? " done" : "");
      row.innerHTML = `
        <img class="badge-row-icon" src="${b.icon || DEFAULT_LABEL_URL}" alt="">
        <div class="badge-row-main">
          <div class="badge-row-title">${b.done ? "🏆 " : ""}${escapeHtml(b.name || "")}</div>
          <div class="badge-row-progress">${b.current} / ${target}${b.levelLabel ? " · " + escapeHtml(b.levelLabel) : ""}</div>
          <div class="stats-bar"><div class="stats-bar-fill" style="width:${pct}%"></div></div>
        </div>`;
      row.addEventListener("click", () => openBadgeDetail(b));
      listEl.appendChild(row);
    });
  }

  $("badges-search-input").addEventListener("input", renderBadgesList);

  document.querySelectorAll("#badges-sort-pills .pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.badgesSort = btn.dataset.sort;
      document.querySelectorAll("#badges-sort-pills .pill").forEach((p) => p.classList.toggle("active", p === btn));
      renderBadgesList();
    });
  });

  // ---- Badge detail drill-down ----

  const BADGE_KIND_LABEL = { style: "стилю", country: "країни", venue: "категорії локації" };

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
    $("badge-detail-name").textContent = (b.done ? "🏆 " : "") + (b.name || "");
    $("badge-detail-progress").textContent =
      `${b.current} / ${target}${b.levelLabel ? " · " + b.levelLabel : ""}`;
    $("badge-detail-bar").style.width = pct + "%";
    const kindLabel = BADGE_KIND_LABEL[b.kind] || "тегу";
    $("badge-detail-howto").textContent = b.done
      ? `Максимальний рівень досягнуто (${b.current} кваліфікуючих позицій).`
      : `Щоб піднятись на рівень: ще ${target - b.current} із ${b.countPerLevel} за ${kindLabel}, перелічені нижче.`;
    const tagsEl = $("badge-detail-tags");
    tagsEl.innerHTML = "";
    (b.tags || []).forEach((tag) => {
      const el = document.createElement("span");
      el.className = "badge-tag";
      el.textContent = tag;
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
      untappdBtn.onclick = () => {
        if (tg && tg.openLink) tg.openLink(openUrl);
        else window.open(openUrl, "_blank");
      };
    } else {
      untappdBtn.classList.add("hidden");
    }
  }

  // ---- Session drill-down (list of beers in one session, with search) ----

  function openSessionBeers(session, color) {
    state.currentSession = session;
    $("session-beers-title").textContent = `${SESSION_EMOJI[color] || "🎪"} ${sessionLabel(session)}`;
    $("session-search-input").value = "";
    showScreen("session-beers");
  }

  async function fetchSessionBeers() {
    if (!state.currentSession) return;
    const query = $("session-search-input").value.trim();
    $("session-beers-status").textContent = "Завантажую…";
    const { ok, data } = await apiPost("/api/checkin/festival/session", {
      session: state.currentSession, query,
    });
    if (!ok) {
      $("session-beers-status").textContent = "Не вдалося завантажити список.";
      return;
    }
    const beers = data.beers || [];
    $("session-beers-status").textContent = beers.length ? "" : "Нічого не знайдено.";
    const listEl = $("session-beers-list");
    listEl.innerHTML = "";
    beers.forEach((b) => {
      const row = document.createElement("div");
      row.className = "result-row" + (b.hadIt ? " had-it" : "");
      row.innerHTML = `
        <div class="thumb">
          <img src="${DEFAULT_LABEL_URL}" alt="">
          ${b.hadIt ? '<span class="had-it-corner">✅</span>' : ""}
        </div>
        <div class="result-main">
          <div class="result-name"><span class="result-name-text">${escapeHtml(b.name || "")}</span>${ratingBadge(b)}</div>
          ${metaLine(b.brewery)}
          ${metaLine(b.style)}
        </div>
        <div class="row-actions">
          <button class="untappd-link-btn" title="Відкрити в Untappd">🔗</button>
        </div>`;
      row.addEventListener("click", (e) => {
        if (e.target.closest(".untappd-link-btn")) return;
        selectBeer(b, { origin: "session-beers" });
      });
      row.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(b.beerId, e));
      listEl.appendChild(row);
    });
  }

  let sessionSearchDebounce = null;
  $("session-search-input").addEventListener("input", () => {
    clearTimeout(sessionSearchDebounce);
    sessionSearchDebounce = setTimeout(fetchSessionBeers, 350);
  });

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

  let searchDebounce = null;
  $("search-input").addEventListener("input", (e) => {
    const q = e.target.value.trim();
    clearTimeout(searchDebounce);
    if (q.length < 2) {
      $("results").innerHTML = "";
      $("search-status").textContent = "";
      return;
    }
    $("search-status").textContent = "Шукаю…";
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
      $("search-status").textContent = status === 429
        ? "Untappd тимчасово обмежив запити — спробуйте за хвилину."
        : "Помилка пошуку.";
      return;
    }
    const beers = data.beers || [];
    $("search-status").textContent = beers.length ? "" : "Нічого не знайдено.";
    $("results").innerHTML = "";
    beers.forEach((b) => {
      const row = document.createElement("div");
      row.className = "result-row" + (b.hadIt ? " had-it" : "");
      row.innerHTML = `
        <div class="thumb">
          <img src="${b.labelUrl || DEFAULT_LABEL_URL}" alt="">
          ${b.hadIt ? '<span class="had-it-corner">✅</span>' : ""}
        </div>
        <div class="result-main">
          <div class="result-name">${sourceBadge(b)}<span class="result-name-text">${escapeHtml(b.name || "")}</span>${ratingBadge(b)}</div>
          ${metaLine(b.brewery)}
          ${metaLine(b.style, b.abv != null ? b.abv + "%" : null)}
        </div>
        <div class="row-actions">
          <button class="add-queue-btn" title="Додати у чергу">+</button>
          <button class="untappd-link-btn" title="Відкрити в Untappd">🔗</button>
        </div>`;
      row.addEventListener("click", (e) => {
        if (e.target.closest(".add-queue-btn") || e.target.closest(".untappd-link-btn")) return;
        selectBeer(b, { origin: "search" });
      });
      row.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(b.beerId, e));
      const addBtn = row.querySelector(".add-queue-btn");
      addBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        addToQueue(b, addBtn);
      });
      $("results").appendChild(row);
    });
  }

  function ratingBadge(b) {
    if (!b.hadIt || typeof b.userRating !== "number") return "";
    return ` <span class="badge had-it-badge">${b.userRating.toFixed(2)}⭐</span>`;
  }

  const SESSION_EMOJI = { yellow: "🟡", blue: "🔵", red: "🔴", green: "🟢" };
  // raw session key -> color, filled in once from /api/checkin/festival/meta
  // (see the bottom of this file) - covers non-color session names.
  const sessionColorMap = {};

  function sourceBadge(b) {
    if (b.source === "festival") {
      const sessions = b.sessions && b.sessions.length ? b.sessions : [null];
      const emojis = sessions.map((s) => SESSION_EMOJI[sessionColorMap[s] || s] || "🎪").join("");
      return `<span class="badge">${emojis}</span> `;
    }
    if (b.source === "wishlist") {
      return `<span class="badge">❤️</span> `;
    }
    return "";
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

  // ---- Rate screen ----

  function setSelectedVenueDisplay(text) {
    const el = $("venue-selected");
    el.textContent = text || "";
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
    setSelectedVenueDisplay(state.lastVenue ? "📍 " + (state.lastVenue.name || "") : "");
    updatePillHighlight();
    showScreen("rate");
  }

  function screenForOrigin(origin) {
    if (origin === "queue") return "queue";
    if (origin === "session-beers") return "session-beers";
    return "search";
  }

  $("rate-back-btn").addEventListener("click", () => {
    showScreen(screenForOrigin(state.origin));
  });

  const PILL_VALUES = [3.75, 4, 4.25, 4.5, 4.75, 5];
  const pillsEl = $("rating-pills");
  PILL_VALUES.forEach((v) => {
    const pill = document.createElement("div");
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
          : `<span class="venue-badge">🏅 ${escapeHtml(b.name)}</span>`
        ).join("");
        html += `<div class="venue-badges">${badgesHtml}</div>`;
      }
      item.innerHTML = html;
      item.addEventListener("click", () => {
        state.selectedVenue = v;
        setSelectedVenueDisplay("📍 " + (v.name || "") + (v.category ? ` (${v.category})` : ""));
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
      $("venue-toggle-btn").textContent = "📍 Завантажую…";
      const { ok, data } = await apiPost("/api/checkin/venues", {});
      if (!ok && data.error === "not_connected") {
        alert(NOT_CONNECTED_MSG);
        $("venue-toggle-btn").textContent = "📍 Мої локації";
        return;
      }
      state.venues = ok ? (data.venues || []) : [];
      $("venue-toggle-btn").textContent = "📍 Мої локації";
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
      alert("Геолокація недоступна на цьому пристрої/версії Telegram — додай локацію зі списку своїх.");
      return;
    }
    const btn = $("venue-nearby-btn");
    btn.disabled = true;
    btn.textContent = "🧭 Шукаю…";

    // Safety net: if getLocation's callback never fires for any reason
    // (a stuck permission prompt, a Telegram client quirk), don't leave
    // the button stuck on "Шукаю…" forever - reset after a timeout.
    let settled = false;
    const resetBtn = () => { btn.disabled = false; btn.textContent = "🧭 Локації поруч"; };
    const timeoutId = setTimeout(() => {
      if (settled) return;
      settled = true;
      resetBtn();
      alert("Не вдалося отримати геолокацію (тайм-аут) — спробуйте ще раз.");
    }, 12000);

    const lm = await ensureLocationManager();
    if (!lm || !lm.isLocationAvailable) {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      resetBtn();
      alert("Геолокація недоступна на цьому пристрої/версії Telegram — додай локацію зі списку своїх.");
      return;
    }
    lm.getLocation(async (location) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      resetBtn();
      if (!location) {
        alert("Не вдалося отримати геолокацію — перевір дозволи в налаштуваннях.");
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
        ? "Foursquare тимчасово обмежив запити — спробуйте за хвилину."
        : "Не вдалося знайти локації.");
      return;
    }
    renderVenueList(data.venues || []);
  }

  $("to-confirm-btn").addEventListener("click", () => {
    const b = state.selectedBeer;
    const venue = state.selectedVenue;
    const shout = $("shout-input").value.trim();
    $("confirm-summary").innerHTML = `
      <div><b>Пиво</b> ${escapeHtml(b.name || "")}</div>
      <div><b>Броварня</b> ${escapeHtml(b.brewery || "")}</div>
      <div><b>Оцінка</b> ${state.rating.toFixed(2)} ⭐</div>
      <div><b>Локація</b> ${venue ? escapeHtml(venue.name || "") : "—"}</div>
      <div><b>Коментар</b> ${shout ? escapeHtml(shout) : "—"}</div>`;
    $("submit-status").textContent = "";
    $("submit-btn").disabled = false;
    $("submit-btn").textContent = "✅ Чекінити";
    showScreen("confirm");
  });

  // ---- Confirm / submit ----

  $("submit-btn").addEventListener("click", async () => {
    const btn = $("submit-btn");
    btn.disabled = true;
    btn.textContent = "Надсилаю…";
    $("submit-status").textContent = "";

    const venue = state.selectedVenue;
    const body = {
      beerId: state.selectedBeer.beerId,
      rating: state.rating,
      shout: $("shout-input").value.trim(),
      foursquareId: venue ? venue.foursquareId : null,
      geolat: venue ? venue.lat : null,
      geolng: venue ? venue.lng : null,
      venueName: venue ? venue.name : null,
      queueItemId: state.origin === "queue" ? state.queueItemId : null,
    };

    const { ok, status, data } = await apiPost("/api/checkin/submit", body);
    if (ok && data.ok) {
      if (venue) {
        // Keep in-memory state in sync with what the server just persisted,
        // so the next beer in this same session is pre-filled without
        // waiting for a reload/refetch of /api/checkin/usage.
        state.lastVenue = venue;
      }
      btn.textContent = data.dryRun ? "✅ (dry-run) Готово" : "✅ Зачекінено!";
      $("submit-status").textContent = data.dryRun
        ? "Тестовий режим: реальний чекін не надіслано."
        : "Готово!";

      setTimeout(() => {
        if (state.origin === "search") {
          $("search-input").value = "";
          $("results").innerHTML = "";
        }
        showScreen(screenForOrigin(state.origin));
      }, state.origin === "queue" ? 1200 : 1500);
    } else {
      btn.disabled = false;
      btn.textContent = "✅ Чекінити";
      if (data.error === "not_connected") {
        $("submit-status").textContent = NOT_CONNECTED_MSG;
      } else {
        $("submit-status").textContent = status === 429
          ? "Untappd тимчасово обмежив запити — спробуйте за хвилину."
          : "Помилка. Спробуйте ще раз.";
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

  $("autotoast-bar-btn").addEventListener("click", () => showScreen("autotoast"));

  async function fetchAutoToastFriends() {
    $("autotoast-status").textContent = "Завантажую…";
    $("autotoast-friends-list").innerHTML = "";
    const { ok, data } = await apiPost("/api/checkin/autotoast/friends", {});
    if (!ok) {
      $("autotoast-status").textContent = (data && data.error === "not_connected")
        ? NOT_CONNECTED_MSG
        : "Не вдалося завантажити список друзів.";
      autoToastFriends = [];
      return;
    }
    autoToastFriends = data.friends || [];
    $("autotoast-enabled-toggle").checked = !!data.enabled;
    $("autotoast-status").textContent = autoToastFriends.length ? "" : "Друзів не знайдено.";
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

  $("festival-watch-bar-btn").addEventListener("click", () => showScreen("festival-watch"));

  async function fetchFestivalWatch() {
    const { ok, data } = await apiPost("/api/checkin/festival_watch/get", {});
    if (!ok) {
      $("festival-watch-status").textContent = "Не вдалося завантажити.";
      return;
    }
    $("festival-watch-enabled-toggle").checked = !!data.enabled;
    $("festival-watch-radius-input").value = data.radiusMeters || 500;
    $("festival-watch-status").textContent = data.lat != null
      ? `Точка: ${data.label || `${data.lat.toFixed(5)}, ${data.lng.toFixed(5)}`}`
      : "Точку стеження ще не встановлено — обери нижче.";
  }

  $("festival-watch-enabled-toggle").addEventListener("change", async (e) => {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await apiPost("/api/checkin/festival_watch/toggle", { enabled: e.target.checked });
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

  async function setFestivalWatchLocation(lat, lng, label) {
    await apiPost("/api/checkin/festival_watch/set_location", { lat, lng, label });
    $("festival-watch-search-results").classList.add("hidden");
    $("festival-watch-search-input").value = "";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    await fetchFestivalWatch();
  }

  $("festival-watch-here-btn").addEventListener("click", async () => {
    if (!tg || !tg.LocationManager) {
      alert("Геолокація недоступна на цьому пристрої/версії Telegram.");
      return;
    }
    const btn = $("festival-watch-here-btn");
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "🧭 Шукаю…";

    let settled = false;
    const reset = () => { btn.disabled = false; btn.textContent = originalText; };
    const timeoutId = setTimeout(() => {
      if (settled) return;
      settled = true;
      reset();
      alert("Не вдалося отримати геолокацію (тайм-аут) — спробуйте ще раз.");
    }, 12000);

    const lm = await ensureLocationManager();
    if (!lm || !lm.isLocationAvailable) {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      reset();
      alert("Геолокація недоступна на цьому пристрої/версії Telegram.");
      return;
    }
    lm.getLocation(async (location) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      reset();
      if (!location) {
        alert("Не вдалося отримати геолокацію — перевір дозволи в налаштуваннях.");
        return;
      }
      await setFestivalWatchLocation(location.latitude, location.longitude, "Моя локація");
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
      const { ok, data } = await apiPost("/api/checkin/venues/nearby", { query: q });
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
      item.addEventListener("click", () => setFestivalWatchLocation(v.lat, v.lng, v.name));
      listEl.appendChild(item);
    });
    listEl.classList.remove("hidden");
  }

  // ---- Settings screen (⚙️) - quick on/off for the three watch features ----
  // Each toggle reads/writes that feature's own existing config directly;
  // this screen doesn't own any state itself, just surfaces the three
  // already-existing on/off switches in one place.

  $("settings-bar-btn").addEventListener("click", () => showScreen("settings"));

  async function fetchSettingsStatus() {
    $("settings-status").textContent = "Завантажую…";
    const [autotoast, festivalWatch, commentWatch] = await Promise.all([
      apiPost("/api/checkin/autotoast/status", {}),
      apiPost("/api/checkin/festival_watch/get", {}),
      apiPost("/api/checkin/comment_watch/get", {}),
    ]);
    $("settings-row-autotoast").classList.toggle("hidden", !(autotoast.ok && autotoast.data.available));
    $("settings-autotoast-toggle").checked = !!(autotoast.ok && autotoast.data.enabled);
    $("settings-festivalwatch-toggle").checked = !!(festivalWatch.ok && festivalWatch.data.enabled);
    $("settings-commentwatch-toggle").checked = !!(commentWatch.ok && commentWatch.data.enabled);
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

  // ---- Events screen (🔔) - recent auto-toast/comment/novelty activity ----
  // Purely a read-only glance-back over event_log.py's rolling per-owner
  // log - no Untappd/quota cost, just a local file read.

  $("events-bar-btn").addEventListener("click", () => showScreen("events"));

  function timeAgo(unixSeconds) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - unixSeconds) / 60));
    if (mins < 1) return "щойно";
    if (mins < 60) return `${mins} хв тому`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} год тому`;
    return `${Math.round(hours / 24)} дн тому`;
  }

  async function fetchEvents() {
    $("events-status").textContent = "Завантажую…";
    $("events-list").innerHTML = "";
    const { ok, data } = await apiPost("/api/checkin/events/get", {});
    if (!ok) {
      $("events-status").textContent = "Не вдалося завантажити.";
      return;
    }
    const events = data.events || [];
    $("events-status").textContent = events.length ? "" : "Поки що нічого не сталось.";
    const listEl = $("events-list");
    events.forEach((ev) => {
      const row = document.createElement("div");
      row.className = "event-row-wrap";
      const mainRow = document.createElement("div");
      mainRow.className = "result-row event-row";
      mainRow.innerHTML = `
        <div class="event-row-main">
          <div class="event-row-text">${escapeHtml(ev.text || "")}</div>
          <div class="event-row-time">${timeAgo(ev.at)}</div>
        </div>
        <div class="row-actions">
          ${ev.beerId ? '<button class="untappd-link-btn" title="Відкрити в Untappd">🔗</button>' : ""}
          ${ev.kind === "comment" && ev.checkinId ? '<button class="event-reply-toggle-btn" title="Відповісти">💬</button>' : ""}
          ${ev.kind === "toast" && ev.username ? '<button class="event-remove-target-btn" title="Прибрати з авто-тосту">✕</button>' : ""}
        </div>
      `;
      if (ev.beerId) {
        mainRow.querySelector(".untappd-link-btn").addEventListener("click", (e) => openUntappdBeer(ev.beerId, e));
      }
      if (ev.kind === "toast" && ev.username) {
        mainRow.querySelector(".event-remove-target-btn").addEventListener("click", async () => {
          const confirmed = confirm(`Прибрати ${ev.username} зі списку авто-тосту?`);
          if (!confirmed) return;
          const { ok: removeOk } = await apiPost("/api/checkin/autotoast/remove_target", { username: ev.username });
          if (removeOk) {
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
            row.remove();
          } else {
            alert("Не вдалося прибрати.");
          }
        });
      }
      row.appendChild(mainRow);

      if (ev.kind === "comment" && ev.checkinId) {
        const replyBox = document.createElement("div");
        replyBox.className = "event-reply-box hidden";
        replyBox.innerHTML = `
          <input type="text" class="event-reply-input" placeholder="@${escapeHtml(ev.username || "")}, …" maxlength="140">
          <button class="primary-btn event-reply-send-btn">Надіслати</button>
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
            alert(replyData && replyData.error === "not_connected" ? NOT_CONNECTED_MSG : "Не вдалося надіслати відповідь.");
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

  // ---- Usage badge (loaded once on start) ----

  apiPost("/api/checkin/usage", {}).then(({ ok, data }) => {
    if (ok && data.remaining != null) {
      $("usage-badge").textContent = `${data.remaining}/${data.limit}`;
    }
    if (ok && data.lastVenue) {
      state.lastVenue = data.lastVenue;
    }
    // Auto-toast is still a personal test feature (see bot.py's
    // AUTO_TOAST_OWNER_ID) - hide the tab entirely for everyone else,
    // rather than showing a screen that'll just 403 on every action.
    if (!ok || !data.isAutoToastOwner) {
      $("autotoast-bar-btn").classList.add("hidden");
    }
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
})();
