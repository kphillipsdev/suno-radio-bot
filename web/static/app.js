/* ── SubotLive Wrapped – Frontend ─────────────────────────────────────── */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const API = "";

// ── State ───────────────────────────────────────────────────────────────
let currentUser = null;
let includeAutofill = false;

// ── Boot ────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  await checkAuth();
  setupTabs();
  loadPublicStats();
  pollNowPlaying();
  setInterval(pollNowPlaying, 15000);
  $("#view-all-djs-btn")?.addEventListener("click", () => toggleAllDjs());
  $("#view-all-liked-btn")?.addEventListener("click", () => toggleAllLiked());
});

// ── Auth ────────────────────────────────────────────────────────────────
/** Discord CDN: default avatar index is (user_id >> 22) % 6 (needs BigInt — snowflakes overflow Number). */
function discordDefaultAvatarIndex(userId) {
  try {
    return Number((BigInt(userId) >> 22n) % 6n);
  } catch {
    return 0;
  }
}

/** Avatar hash `a_*` is animated (.gif); otherwise .png. */
function discordAvatarUrl(userId, avatarHash, size = 64) {
  if (!avatarHash) {
    return `https://cdn.discordapp.com/embed/avatars/${discordDefaultAvatarIndex(userId)}.png`;
  }
  const ext = String(avatarHash).startsWith("a_") ? "gif" : "png";
  return `https://cdn.discordapp.com/avatars/${userId}/${avatarHash}.${ext}?size=${size}`;
}

async function checkAuth() {
  try {
    const res = await fetch(`${API}/api/me`);
    const data = await res.json();
    if (data.logged_in) {
      currentUser = data;
      renderAuthUI(data);
    }
  } catch (e) {
    console.warn("Auth check failed:", e);
  }
}

function renderAuthUI(user) {
  const wrappedTab = $("#wrapped-tab");
  if (wrappedTab) wrappedTab.style.display = "";
  const el = $("#auth-area");
  const avatarUrl = discordAvatarUrl(user.id, user.avatar, 64);
  el.innerHTML = `
    <div class="user-info">
      <img src="${avatarUrl}" alt="" />
      <span>${escHtml(user.username)}</span>
      <a href="/auth/logout" class="auth-btn secondary">Log out</a>
    </div>`;
}

// ── Tabs ────────────────────────────────────────────────────────────────
function setupTabs() {
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.dataset.tab;
      if (target === "public") {
        $(".public-section").classList.add("active");
        $(".wrapped-section").classList.remove("active");
      } else {
        $(".public-section").classList.remove("active");
        $(".wrapped-section").classList.add("active");
        loadWrapped();
      }
    });
  });
}

// ── Public Stats ────────────────────────────────────────────────────────
async function loadPublicStats() {
  const af = `autofill=${includeAutofill}`;
  const range = (document.getElementById("range-select") || {}).value || "all";
  try {
    const [overview, topTracks, topDjs, heatmap, topLiked] = await Promise.all([
      fetchJson(`/api/stats/overview?${af}`),
      fetchJson(`/api/stats/top-tracks?range=${range}&limit=10&${af}`),
      fetchJson(`/api/stats/top-djs?range=all&limit=10&${af}`),
      fetchJson(`/api/stats/heatmap?${af}`),
      fetchJson("/api/stats/top-liked?limit=10"),
    ]);
    renderOverview(overview);
    renderTopTracks(topTracks);
    renderTopDjs(topDjs);
    renderHeatmap(heatmap);
    renderTopLiked(topLiked);
  } catch (e) {
    console.error("Failed to load public stats:", e);
  }
}

function renderOverview(d) {
  const el = $("#overview-grid");
  el.innerHTML = `
    ${statCard("Total Plays", fmtNum(d.total_plays), "accent")}
    ${statCard("Unique Tracks", fmtNum(d.unique_tracks), "teal")}
    ${statCard("Listening Hours", fmtNum(d.total_listening_hours), "pink")}
    ${statCard("DJs", fmtNum(d.unique_djs), "amber")}
    ${statCard("Total Saves", fmtNum(d.total_likes), "accent")}
    ${statCard("Since", d.first_play ? d.first_play.slice(0, 10) : "—", "teal")}`;
}

function statCard(label, value, color) {
  return `<div class="stat-card">
    <div class="label">${label}</div>
    <div class="value ${color}">${value}</div>
  </div>`;
}

function renderTopTracks(tracks) {
  const el = $("#top-tracks-list");
  if (!tracks.length) {
    el.innerHTML = '<li class="track-item"><span class="track-info">No plays yet.</span></li>';
    return;
  }
  el.innerHTML = tracks
    .map((t, i) => {
      const rankClass = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
      const coverSrc = t.cover_url || "";
      const link = t.source_url
        ? `<a href="${escAttr(t.source_url)}" target="_blank" rel="noopener">${escHtml(t.title || "Untitled")}</a>`
        : escHtml(t.title || "Untitled");
      return `<li class="track-item">
        <span class="track-rank ${rankClass}">${i + 1}</span>
        ${coverSrc ? `<img class="track-cover" src="${escAttr(coverSrc)}" alt="" loading="lazy"/>` : `<div class="track-cover"></div>`}
        <div class="track-info">
          <div class="track-title">${link}</div>
          <div class="track-artist">${escHtml(t.artist || "Unknown")}</div>
        </div>
        <span class="track-stat">${fmtNum(t.play_count)} plays</span>
      </li>`;
    })
    .join("");
}

function renderTopDjs(djs) {
  const el = $("#top-djs-list");
  if (!djs.length) return;
  const maxPlays = djs[0]?.play_count || 1;
  el.innerHTML = djs
    .map((d, i) => {
      const pct = Math.max(2, (d.play_count / maxPlays) * 100);
      const rankClass = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
      return `<li class="dj-item">
        <span class="track-rank ${rankClass}">${i + 1}</span>
        <span style="min-width:120px;font-size:0.85rem;color:var(--text)">${escHtml(d.username || "DJ " + shortId(d.user_id))}</span>
        <div class="dj-bar-container">
          <div class="dj-bar" style="width:${pct}%"></div>
          <span class="dj-bar-label">${fmtNum(d.play_count)} plays · ${d.unique_tracks} tracks</span>
        </div>
      </li>`;
    })
    .join("");
}

function renderHeatmap(data) {
  const el = $("#heatmap");
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const lookup = {};
  let max = 0;
  data.forEach((r) => {
    const key = `${r.dow}-${r.hour}`;
    lookup[key] = r.plays;
    if (r.plays > max) max = r.plays;
  });

  let html = '<div class="heatmap-label"></div>';
  for (let h = 0; h < 24; h++) {
    html += `<div class="heatmap-label hour">${h.toString().padStart(2, "0")}</div>`;
  }

  for (let d = 0; d < 7; d++) {
    html += `<div class="heatmap-label">${days[d]}</div>`;
    for (let h = 0; h < 24; h++) {
      const plays = lookup[`${d}-${h}`] || 0;
      const intensity = max > 0 ? Math.min(5, Math.ceil((plays / max) * 5)) : 0;
      html += `<div class="heatmap-cell" data-intensity="${intensity}" title="${days[d]} ${h}:00 — ${plays} plays"></div>`;
    }
  }
  el.innerHTML = html;
}

function renderTopLiked(tracks) {
  const el = $("#top-liked-list");
  if (!tracks.length) {
    el.innerHTML = '<li class="track-item"><span class="track-info">No saves yet.</span></li>';
    return;
  }
  el.innerHTML = tracks
    .map((t, i) => {
      const rankClass = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
      const coverSrc = t.cover_url || "";
      const link = t.source_url
        ? `<a href="${escAttr(t.source_url)}" target="_blank" rel="noopener">${escHtml(t.title || "Untitled")}</a>`
        : escHtml(t.title || "Untitled");
      return `<li class="track-item">
        <span class="track-rank ${rankClass}">${i + 1}</span>
        ${coverSrc ? `<img class="track-cover" src="${escAttr(coverSrc)}" alt="" loading="lazy"/>` : `<div class="track-cover"></div>`}
        <div class="track-info">
          <div class="track-title">${link}</div>
          <div class="track-artist">${escHtml(t.artist || "Unknown")}</div>
        </div>
        <span class="track-stat">${fmtNum(t.like_count)} ♥</span>
      </li>`;
    })
    .join("");
}

// ── All DJs (paginated) ─────────────────────────────────────────────────
const PER_PAGE = 20;
let allDjsVisible = false;
let allDjsPage = 1;
let allDjsTotal = 0;

async function toggleAllDjs() {
  const section = $("#all-djs-section");
  const topList = $("#top-djs-list");
  const btn = $("#view-all-djs-btn");
  allDjsVisible = !allDjsVisible;
  section.style.display = allDjsVisible ? "block" : "none";
  topList.style.display = allDjsVisible ? "none" : "";
  btn.textContent = allDjsVisible ? "Hide" : "View all DJs";
  if (allDjsVisible) await loadAllDjsPage(1);
}

async function loadAllDjsPage(page) {
  allDjsPage = page;
  const af = `autofill=${includeAutofill}`;
  const data = await fetchJson(`/api/stats/all-djs?page=${page}&per_page=${PER_PAGE}&${af}`);
  allDjsTotal = data.total;
  const list = $("#all-djs-list");
  if (!data.items.length) {
    list.innerHTML = '<li class="dj-item"><span class="track-info">No DJs.</span></li>';
  } else {
    const maxPlays = data.items[0]?.play_count || 1;
    list.innerHTML = data.items
      .map((d, i) => {
        const pct = Math.max(2, (d.play_count / maxPlays) * 100);
        const rank = (page - 1) * PER_PAGE + i + 1;
        const rankClass = rank === 1 ? "gold" : rank === 2 ? "silver" : rank === 3 ? "bronze" : "";
        return `<li class="dj-item">
          <span class="track-rank ${rankClass}">${rank}</span>
          <span style="min-width:120px;font-size:0.85rem;color:var(--text)">${escHtml(d.username || "DJ " + shortId(d.user_id))}</span>
          <div class="dj-bar-container">
            <div class="dj-bar" style="width:${pct}%"></div>
            <span class="dj-bar-label">${fmtNum(d.play_count)} plays · ${d.unique_tracks} tracks</span>
          </div>
        </li>`;
      })
      .join("");
  }
  renderPagination("all-djs-pagination", page, data.total, PER_PAGE, loadAllDjsPage);
}

// ── All liked (paginated) ────────────────────────────────────────────────
let allLikedVisible = false;
let allLikedPage = 1;

async function toggleAllLiked() {
  const section = $("#all-liked-section");
  const topList = $("#top-liked-list");
  const btn = $("#view-all-liked-btn");
  allLikedVisible = !allLikedVisible;
  section.style.display = allLikedVisible ? "block" : "none";
  topList.style.display = allLikedVisible ? "none" : "";
  btn.textContent = allLikedVisible ? "Hide" : "View all";
  if (allLikedVisible) await loadAllLikedPage(1);
}

async function loadAllLikedPage(page) {
  allLikedPage = page;
  const data = await fetchJson(`/api/stats/all-liked?page=${page}&per_page=${PER_PAGE}`);
  const list = $("#all-liked-list");
  if (!data.items.length) {
    list.innerHTML = '<li class="track-item"><span class="track-info">No saved tracks.</span></li>';
  } else {
    list.innerHTML = data.items
      .map((t, i) => {
        const rank = (page - 1) * PER_PAGE + i + 1;
        const rankClass = rank === 1 ? "gold" : rank === 2 ? "silver" : rank === 3 ? "bronze" : "";
        const coverSrc = t.cover_url || "";
        const link = t.source_url
          ? `<a href="${escAttr(t.source_url)}" target="_blank" rel="noopener">${escHtml(t.title || "Untitled")}</a>`
          : escHtml(t.title || "Untitled");
        return `<li class="track-item">
          <span class="track-rank ${rankClass}">${rank}</span>
          ${coverSrc ? `<img class="track-cover" src="${escAttr(coverSrc)}" alt="" loading="lazy"/>` : `<div class="track-cover"></div>`}
          <div class="track-info">
            <div class="track-title">${link}</div>
            <div class="track-artist">${escHtml(t.artist || "Unknown")}</div>
          </div>
          <span class="track-stat">${fmtNum(t.like_count)} ♥</span>
        </li>`;
      })
      .join("");
  }
  renderPagination("all-liked-pagination", page, data.total, PER_PAGE, loadAllLikedPage);
}

function renderPagination(containerId, page, total, perPage, loadFn) {
  const el = $(`#${containerId}`);
  const totalPages = Math.max(1, Math.ceil(total / perPage));
  if (totalPages <= 1) {
    el.innerHTML = total ? `<span class="page-info">${fmtNum(total)} total</span>` : "";
    return;
  }
  el.innerHTML = `
    <button type="button" ${page <= 1 ? "disabled" : ""} data-page="${page - 1}">Prev</button>
    <span class="page-info">Page ${page} of ${totalPages} (${fmtNum(total)} total)</span>
    <button type="button" ${page >= totalPages ? "disabled" : ""} data-page="${page + 1}">Next</button>
  `;
  el.querySelectorAll("button").forEach((btn) => {
    if (btn.disabled) return;
    btn.addEventListener("click", () => loadFn(parseInt(btn.dataset.page, 10)));
  });
}

// ── Wrapped (personal) ──────────────────────────────────────────────────
let wrappedLoaded = false;

async function loadWrapped() {
  if (!currentUser) {
    renderWrappedLogin();
    return;
  }
  if (wrappedLoaded) return;
  const el = $(".wrapped-section");
  el.innerHTML = '<div class="loading"><div class="spinner"></div><div>Loading your Wrapped...</div></div>';

  try {
    const data = await fetchJson("/api/me/wrapped");
    wrappedLoaded = true;
    renderWrapped(data);
  } catch (e) {
    if (e.status === 401) {
      renderWrappedLogin();
    } else {
      el.innerHTML = '<div class="loading">Failed to load wrapped data.</div>';
    }
  }
}

function renderWrappedLogin() {
  $(".wrapped-section").innerHTML = `
    <div class="login-prompt">
      <h2>Your Wrapped</h2>
      <p>Log in with Discord to see your personal listening stats, top tracks, and fun facts.</p>
      <a href="/auth/login" class="auth-btn">Log in with Discord</a>
    </div>`;
}

function renderWrapped(d) {
  const el = $(".wrapped-section");
  const topTrack = d.top_tracks[0];
  const topArtistDisplay = d.top_artist_display;
  const selfArtist = d.top_artists[0];
  const showSelf = !topArtistDisplay || (selfArtist && topArtistDisplay.artist === selfArtist.artist);

  el.innerHTML = `
    <div class="wrapped-hero">
      <h2>${escHtml(d.user.username)}'s Wrapped</h2>
      <div class="subtitle">Your SunoRadio listening journey</div>
    </div>

    <div class="wrapped-stats">
      ${wrappedCard("Total Plays", fmtNum(d.total_plays), `${d.unique_tracks} unique tracks`)}
      ${wrappedCard("DJ Hours", fmtNum(d.listening_hours), "Hours of music you queued")}
      ${wrappedCard("Songs Saved", fmtNum(d.total_likes), "Tracks you saved for autofill")}
      ${wrappedCard("Top Track", topTrack ? escHtml(truncate(topTrack.title, 30)) : "—",
                     topTrack ? `${topTrack.play_count} plays` : "")}
      ${wrappedCard("Fav Artist", topArtistDisplay ? escHtml(truncate(topArtistDisplay.artist, 30)) : "—",
                     topArtistDisplay ? `${topArtistDisplay.play_count} plays` + (showSelf ? "" : " · excluding you") : "")}
      ${wrappedCard("Listening Streak", d.listening_streak ? `${d.listening_streak} day${d.listening_streak !== 1 ? "s" : ""}` : "—",
                     "Consecutive days listening")}
      ${wrappedCard("Busiest Day", d.busiest_day ? d.busiest_day.day : "—",
                     d.busiest_day ? `${d.busiest_day.plays} plays that day` : "")}
    </div>

    <div class="section-title"><span class="icon">🎵</span> Your Top Tracks <button type="button" class="view-all-btn" id="view-all-my-tracks-btn">View all</button></div>
    <ul class="track-list" id="wrapped-tracks"></ul>
    <div id="all-my-tracks-section" class="all-section" style="display:none">
      <ul class="track-list" id="all-my-tracks-list"></ul>
      <div class="pagination" id="all-my-tracks-pagination"></div>
    </div>

    ${d.rarest_finds.length ? `
      <div class="section-title"><span class="icon">💎</span> Your Rarest Finds</div>
      <p style="color:var(--text-dim);margin:-12px 0 20px;font-size:0.85rem">
        Tracks only you have played on the server
      </p>
      <ul class="track-list" id="wrapped-rare"></ul>
    ` : ""}

    ${d.favorite_liked.length ? `
      <div class="section-title"><span class="icon">♥</span> Most Saved by You <button type="button" class="view-all-btn" id="view-all-my-liked-btn">View all</button></div>
      <ul class="track-list" id="wrapped-liked"></ul>
      <div id="all-my-liked-section" class="all-section" style="display:none">
        <ul class="track-list" id="all-my-liked-list"></ul>
        <div class="pagination" id="all-my-liked-pagination"></div>
      </div>
    ` : ""}
  `;

  renderTrackListInto("#wrapped-tracks", d.top_tracks, "play_count", "plays");
  if (d.rarest_finds.length) renderTrackListInto("#wrapped-rare", d.rarest_finds, null, null);
  if (d.favorite_liked.length) renderTrackListInto("#wrapped-liked", d.favorite_liked, "like_count", "♥");

  $("#view-all-my-tracks-btn")?.addEventListener("click", () => toggleAllMyTracks());
  $("#view-all-my-liked-btn")?.addEventListener("click", () => toggleAllMyLiked());
}

function renderTrackListInto(selector, tracks, statKey, statLabel) {
  const el = $(selector);
  if (!el || !tracks.length) return;
  el.innerHTML = tracks
    .map((t, i) => {
      const rankClass = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
      const coverSrc = t.cover_url || "";
      const link = t.source_url
        ? `<a href="${escAttr(t.source_url)}" target="_blank" rel="noopener">${escHtml(t.title || "Untitled")}</a>`
        : escHtml(t.title || "Untitled");
      const stat = statKey && t[statKey] != null ? `<span class="track-stat">${fmtNum(t[statKey])} ${statLabel}</span>` : "";
      return `<li class="track-item">
        <span class="track-rank ${rankClass}">${i + 1}</span>
        ${coverSrc ? `<img class="track-cover" src="${escAttr(coverSrc)}" alt="" loading="lazy"/>` : `<div class="track-cover"></div>`}
        <div class="track-info">
          <div class="track-title">${link}</div>
          <div class="track-artist">${escHtml(t.artist || "Unknown")}</div>
        </div>
        ${stat}
      </li>`;
    })
    .join("");
}

// ── My Tracks (paginated) ────────────────────────────────────────────────
let allMyTracksVisible = false;

async function toggleAllMyTracks() {
  const section = $("#all-my-tracks-section");
  const topList = $("#wrapped-tracks");
  const btn = $("#view-all-my-tracks-btn");
  allMyTracksVisible = !allMyTracksVisible;
  section.style.display = allMyTracksVisible ? "block" : "none";
  topList.style.display = allMyTracksVisible ? "none" : "";
  btn.textContent = allMyTracksVisible ? "Hide" : "View all";
  if (allMyTracksVisible) await loadAllMyTracksPage(1);
}

async function loadAllMyTracksPage(page) {
  const data = await fetchJson(`/api/me/tracks?page=${page}&per_page=${PER_PAGE}`);
  const list = $("#all-my-tracks-list");
  list.innerHTML = data.items
    .map((t, i) => {
      const rank = (page - 1) * PER_PAGE + i + 1;
      const rankClass = rank === 1 ? "gold" : rank === 2 ? "silver" : rank === 3 ? "bronze" : "";
      const coverSrc = t.cover_url || "";
      const link = t.source_url
        ? `<a href="${escAttr(t.source_url)}" target="_blank" rel="noopener">${escHtml(t.title || "Untitled")}</a>`
        : escHtml(t.title || "Untitled");
      return `<li class="track-item">
        <span class="track-rank ${rankClass}">${rank}</span>
        ${coverSrc ? `<img class="track-cover" src="${escAttr(coverSrc)}" alt="" loading="lazy"/>` : `<div class="track-cover"></div>`}
        <div class="track-info">
          <div class="track-title">${link}</div>
          <div class="track-artist">${escHtml(t.artist || "Unknown")}</div>
        </div>
        <span class="track-stat">${fmtNum(t.play_count)} plays</span>
      </li>`;
    })
    .join("");
  renderPagination("all-my-tracks-pagination", page, data.total, PER_PAGE, loadAllMyTracksPage);
}

// ── My Liked (paginated) ─────────────────────────────────────────────────
let allMyLikedVisible = false;

async function toggleAllMyLiked() {
  const section = $("#all-my-liked-section");
  const topList = $("#wrapped-liked");
  const btn = $("#view-all-my-liked-btn");
  allMyLikedVisible = !allMyLikedVisible;
  section.style.display = allMyLikedVisible ? "block" : "none";
  topList.style.display = allMyLikedVisible ? "none" : "";
  btn.textContent = allMyLikedVisible ? "Hide" : "View all";
  if (allMyLikedVisible) await loadAllMyLikedPage(1);
}

async function loadAllMyLikedPage(page) {
  const data = await fetchJson(`/api/me/liked?page=${page}&per_page=${PER_PAGE}`);
  const list = $("#all-my-liked-list");
  list.innerHTML = data.items
    .map((t, i) => {
      const rank = (page - 1) * PER_PAGE + i + 1;
      const rankClass = rank === 1 ? "gold" : rank === 2 ? "silver" : rank === 3 ? "bronze" : "";
      const coverSrc = t.cover_url || "";
      const link = t.source_url
        ? `<a href="${escAttr(t.source_url)}" target="_blank" rel="noopener">${escHtml(t.title || "Untitled")}</a>`
        : escHtml(t.title || "Untitled");
      return `<li class="track-item">
        <span class="track-rank ${rankClass}">${rank}</span>
        ${coverSrc ? `<img class="track-cover" src="${escAttr(coverSrc)}" alt="" loading="lazy"/>` : `<div class="track-cover"></div>`}
        <div class="track-info">
          <div class="track-title">${link}</div>
          <div class="track-artist">${escHtml(t.artist || "Unknown")}</div>
        </div>
        <span class="track-stat">${fmtNum(t.like_count)} ♥</span>
      </li>`;
    })
    .join("");
  renderPagination("all-my-liked-pagination", page, data.total, PER_PAGE, loadAllMyLikedPage);
}

function wrappedCard(label, value, detail) {
  return `<div class="wrapped-card">
    <div class="card-label">${label}</div>
    <div class="card-value">${value}</div>
    ${detail ? `<div class="card-detail">${detail}</div>` : ""}
  </div>`;
}

// ── Range selector + autofill toggle ────────────────────────────────────
document.addEventListener("change", async (e) => {
  if (e.target.id === "range-select") {
    const range = e.target.value;
    const af = `autofill=${includeAutofill}`;
    const tracks = await fetchJson(`/api/stats/top-tracks?range=${range}&limit=10&${af}`);
    renderTopTracks(tracks);
  }
  if (e.target.id === "autofill-toggle") {
    includeAutofill = e.target.checked;
    loadPublicStats();
  }
});

// ── Now Playing ─────────────────────────────────────────────────────────
async function pollNowPlaying() {
  try {
    const data = await fetchJson("/api/stats/now-playing");
    const bar = $("#now-playing-bar");
    if (!data.playing) {
      bar.style.display = "none";
      return;
    }
    bar.style.display = "";
    const cover = $("#np-cover");
    cover.src = data.cover_url || "/static/assets/logo.jpg";
    const titleEl = $("#np-title");
    titleEl.innerHTML = `<span class="np-pulse"></span>` + (data.source_url
      ? `<a href="${escAttr(data.source_url)}" target="_blank" rel="noopener">${escHtml(data.title || "Untitled")}</a>`
      : escHtml(data.title || "Untitled"));
    $("#np-artist").textContent = data.artist || "Unknown";
    const req = $("#np-requester");
    if (data.context === "autofill") {
      req.textContent = "via Autofill";
    } else if (data.requested_by_name) {
      req.textContent = `queued by ${data.requested_by_name}`;
    } else {
      req.textContent = "";
    }
    const joinBtn = $("#np-join");
    if (data.guild_id && data.channel_id) {
      joinBtn.href = `https://discord.com/channels/${data.guild_id}/${data.channel_id}`;
      joinBtn.style.display = "";
    } else {
      joinBtn.style.display = "none";
    }
  } catch (e) {
    // silently ignore polling errors
  }
}

// ── Utilities ───────────────────────────────────────────────────────────
async function fetchJson(url) {
  const res = await fetch(`${API}${url}`);
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function fmtNum(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-US");
}

function shortId(id) {
  if (!id) return "???";
  return id.slice(-4);
}

function truncate(str, len) {
  if (!str) return "";
  return str.length > len ? str.slice(0, len) + "…" : str;
}

function escHtml(s) {
  if (!s) return "";
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function escAttr(s) {
  return escHtml(s);
}
