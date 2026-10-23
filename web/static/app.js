// ASVZ Bot — phone remote, calendar first.
//   Week  next sign-up with countdown, the week as a timeline (queued /
//         signed up / waiting / missed / starred), your courses
//   Find  ASVZ's lesson search with stars (the desktop app's Discover)
//   Bot   timer, run now, notifications, the bot's checks, add by number
// All work happens on the server; this page only sends commands and shows status.
"use strict";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const WD_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TIME_FILTERS = [["Any time", 0, 24], ["Morning (before 12)", 0, 12],
                      ["Afternoon (12–17)", 12, 17], ["Evening (from 17)", 17, 24]];
const ZURICH = "Europe/Zurich";
// No reds: red means "missed / failed" in the week view.
const SPORT_COLORS = ["#f0a03c", "#2fb380", "#3d9be0", "#8b6cf0", "#d65db1", "#20afb0", "#8fa83a", "#e08a3c", "#5c7cfa", "#12a4c8"];
const ICON = {
  week: '<svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>',
  find: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>',
  bot: '<svg viewBox="0 0 24 24"><path d="M3 13h4l2.5-6 4 11 2.5-5H21"/></svg>',
  star: '<svg viewBox="0 0 24 24"><path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8L3.5 9.7l5.9-.9z"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/></svg>',
  eye: '<svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="m7 7 10 10M17 7 7 17"/></svg>',
  bolt: '<svg viewBox="0 0 24 24"><path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v4h-4"/></svg>',
  chev: '<svg viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M8 6h12M8 12h12M8 18h12M4 6h0M4 12h0M4 18h0"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l.8 12.5h9.4L17.5 7"/></svg>',
  ext: '<svg viewBox="0 0 24 24"><path d="M13 4h7v7M20 4l-9 9M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>',
};
// item kind -> [status icon, css class, label]
const KIND = {
  queued: ["clock", "q", "Queued"],
  enrolling: ["bolt", "live", "Signing you up now"],
  retrying: ["retry", "warn", "Retrying after a failed attempt"],
  signed: ["check", "ok", "Signed up"],
  watching: ["eye", "warn", "Full — watching for a free spot"],
  missed: ["x", "bad", "Missed"],
  failed: ["x", "bad", "Failed"],
  starred: ["star", "q", "Starred — not signed up"],
};

const $ = (sel) => document.querySelector(sel);
const state = {
  page: "week", week: null, weekIdx: 0, favs: { sports: {}, offers: {} }, plan: null,
  sports: [], sport: null, offers: [], selected: new Set(), timetableDays: 8, serverOffset: 0,
};

// ---------------------------------------------------------------- helpers

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;              // only our own ICON strings
    else if (k === "color") e.style.setProperty("--c", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}
const icon = (name, cls = "") => el("span", { class: cls, html: ICON[name], "aria-hidden": "true" });

function load(key, fallback) {
  try { const v = localStorage.getItem("vb." + key); return v === null ? fallback : JSON.parse(v); }
  catch { return fallback; }
}
function save(key, value) { try { localStorage.setItem("vb." + key, JSON.stringify(value)); } catch { /* private mode */ } }

class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json", "X-VB": "1" } };
  let r;
  try { r = await fetch("api/" + path, { credentials: "same-origin", ...opts }); }
  catch { throw new HttpError(0, "Can't reach the server — check your connection."); }
  if (r.status === 401 && path !== "login") { showLogin(); throw new HttpError(401, "Please log in"); }
  const data = (r.headers.get("Content-Type") || "").includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new HttpError(r.status, (data && data.error) || `Error ${r.status}`);
  return data;
}

function now() { return new Date(Date.now() + state.serverOffset); }
const fmt = new Intl.DateTimeFormat("en-GB", { timeZone: ZURICH, weekday: "short", day: "2-digit", month: "2-digit",
  hour: "2-digit", minute: "2-digit", hour12: false, year: "numeric" });
function parts(d) { const o = {}; for (const p of fmt.formatToParts(new Date(d))) o[p.type] = p.value; return o; }
function hm(d) { const p = parts(d); return `${p.hour}:${p.minute}`; }
function shortDate(d) { const p = parts(d); return `${p.weekday} ${p.day}.${p.month}. ${p.hour}:${p.minute}`; }
function dayKey(d) { const p = parts(d); return `${p.year}-${p.month}-${p.day}`; }
function countdown(d) {
  let s = Math.floor((new Date(d) - now()) / 1000);
  if (s <= 0) return "now";
  const days = Math.floor(s / 86400); s %= 86400;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (days) return `${days}d ${h}h`;
  if (h) return `${h}h ${m}min`;
  return `${m} min`;
}
function ago(ms) {
  const s = Math.max(0, Math.round((now() - ms) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
function clock(ms) { return new Date(ms).toLocaleTimeString("en-GB", { timeZone: ZURICH, hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function zurichWeekday() { return WD_SHORT.indexOf(parts(now()).weekday); }

function sportColor(name) {
  let h = 0;
  for (const ch of String(name || "?")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return SPORT_COLORS[h % SPORT_COLORS.length];
}
function sportOf(item) {  // "Volleyball: Volleyball Mixed" -> "Volleyball"
  return (item.title || "").split(":")[0] || item.lesson_id;
}

function toast(text, ms = 4000) {
  const box = $("#toasts");
  while (box.children.length >= 2) box.firstChild.remove();
  const t = el("div", { class: "toast" }, text);
  box.append(t);
  setTimeout(() => t.remove(), ms);
}

function confirmDialog(title, body, okLabel, destructive = true) {
  return new Promise((resolve) => {
    $("#dialog-title").textContent = title;
    $("#dialog-body").textContent = body;
    const ok = $("#dialog-ok");
    ok.textContent = okLabel;
    ok.className = "btn " + (destructive ? "danger" : "primary");
    $("#dialog").hidden = false;
    const done = (v) => { $("#dialog").hidden = true; ok.onclick = null; $("#dialog-cancel").onclick = null; resolve(v); };
    ok.onclick = () => done(true);
    $("#dialog-cancel").onclick = () => done(false);
  });
}

async function busy(btn, work) {
  btn.classList.add("busy"); btn.disabled = true;
  try { return await work(); } finally { btn.classList.remove("busy"); btn.disabled = false; }
}

function statusPage(target, kind, title, desc, action) {
  target.replaceChildren();
  if (kind === "loading") target.append(el("div", { class: "spinner", role: "status", "aria-label": "Loading" }));
  else { if (title) target.append(el("h3", {}, title)); if (desc) target.append(el("p", {}, desc)); if (action) target.append(action); }
}

function download(text, filename, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a", { href: url, download: filename });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function starButton(on, label, onToggle) {
  const b = el("button", { class: "star", "aria-pressed": on ? "true" : "false", "aria-label": label, title: label, html: ICON.star,
    onclick: async (ev) => {
      ev.stopPropagation(); ev.preventDefault();
      const next = b.getAttribute("aria-pressed") !== "true";
      b.setAttribute("aria-pressed", next ? "true" : "false");
      try { await onToggle(next); } catch (e) { b.setAttribute("aria-pressed", next ? "false" : "true"); toast(e.message); }
    } });
  return b;
}

function showOverlaps(r, delay = 900) {
  (r.overlaps || []).forEach((m, i) => setTimeout(() => toast("⚠ " + m, 8000), delay * (i + 1)));
}

async function setFav(kind, id, on, extra = {}) {
  state.favs = await api("favs", { kind, id, on, ...extra });
  toast(on ? "Starred" : "Star removed", 1800);
}

// ---------------------------------------------------------------- login

function showLogin() { $("#app").hidden = true; $("#login").hidden = false; $("#login-form").user.focus(); }

async function boot() {
  let me;
  try { me = await api("me"); }
  catch (e) { $("#login").hidden = false; $("#login-error").textContent = e.message; return; }
  if (!me.logged_in) {
    if (!me.password_set) $("#login-error").textContent = "No page password yet. On the server run: deploy/setup.sh (it asks for one).";
    return showLogin();
  }
  $("#login").hidden = true; $("#app").hidden = false;
  startApp();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#login-error").textContent = "";
  try {
    await busy(f.querySelector("button"), () => api("login", { user: f.user.value.trim(), password: f.password.value }));
    f.password.value = ""; boot();
  } catch (e) { $("#login-error").textContent = e.message; }
});

// ---------------------------------------------------------------- window

let started = false;
function startApp() {
  if (started) { refresh(); return; }
  started = true;
  const theme = load("theme", null);
  if (theme) document.documentElement.dataset.theme = theme;
  buildNav();
  buildFilters();
  api("favs").then((f) => { state.favs = f; }).catch(() => {});
  setPage(load("page", "week"));
  loadSports();
  setInterval(tick, 10000);
  setInterval(() => { if (state.page === "week" && !document.hidden) renderNextUp(); }, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}

function refresh() {
  if (state.page === "week") loadWeek();
  if (state.page === "bot") { loadStatus(); loadLog(); }
}
function tick() {
  if (document.hidden) return;
  if (state.page === "week") loadWeek(true);
  if (state.page === "bot") { loadStatus(); loadLog(); }
}

const PAGES = [["week", "Week"], ["find", "Find"], ["bot", "Bot"]];
function buildNav() {
  for (const nav of [$("#nav-top"), $("#tabbar")]) {
    for (const [id, label] of PAGES) {
      const b = el("button", { "data-page": id, onclick: () => setPage(id), html: ICON[id] });
      b.append(el("span", {}, label));
      if (id === "bot") b.append(el("span", { class: "badge", hidden: true }));
      nav.append(b);
    }
  }
}

function setPage(id) {
  if (!PAGES.some(([p]) => p === id)) id = "week";
  state.page = id; save("page", id);
  for (const b of document.querySelectorAll("#nav-top button, #tabbar button"))
    if (b.dataset.page === id) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  for (const [p] of PAGES) $("#page-" + p).hidden = p !== id;
  updateHeader();
  refresh();
  if (id === "find" && state.sport) rebuildOffers();
}

function updateHeader() {
  const inOffers = state.page === "find" && $(".split").classList.contains("show-offers");
  $("#back").hidden = !(inOffers && matchMedia("(max-width: 720px)").matches);
  $("#subtitle").textContent = state.page === "find" && state.sport ? state.sport.name : "";
}
matchMedia("(max-width: 720px)").addEventListener("change", updateHeader);
$("#back").addEventListener("click", () => { $(".split").classList.remove("show-offers"); save("showOffers", false); updateHeader(); });

$("#menu-btn").addEventListener("click", (ev) => { ev.stopPropagation(); $("#menu").hidden = !$("#menu").hidden; });
document.addEventListener("click", () => { $("#menu").hidden = true; });
$("#menu").addEventListener("click", async (ev) => {
  const action = ev.target.dataset.action;
  $("#menu").hidden = true;
  if (action === "reload") location.reload();
  if (action === "theme") {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    save("theme", document.documentElement.dataset.theme);
  }
  if (action === "logout") { try { await api("logout", {}); } finally { location.reload(); } }
});

function setHealthBadge(h) {
  const bad = h && (!h.timer || h.late || h.last_kind === "error");
  for (const b of document.querySelectorAll(".badge")) b.hidden = !bad;
}

// ---------------------------------------------------------------- Week

async function loadWeek(quiet = false) {
  try {
    const w = await api("week");
    state.serverOffset = new Date(w.server_time) - Date.now();
    state.week = w;
    renderWeek();
  } catch (e) {
    if (!quiet && e.status !== 401) statusPage($("#week-grid"), "error", "Couldn't load your week", e.message);
  }
}

function renderNextUp() {
  const w = state.week, box = $("#nextup");
  if (!w) return;
  const n = w.next_up;
  box.replaceChildren();
  if (!n) {
    box.className = "nextup card empty";
    box.append(el("div", { class: "label" }, "Next sign-up"), el("div", { class: "what" }, "Nothing queued"),
      el("div", { class: "where" }, "Star courses in Find, or sign up there — they show up here with a countdown."));
    return;
  }
  box.className = "nextup card";
  const opens = new Date(n.enroll_start);
  const live = opens <= now();
  box.append(el("div", { class: "label" }, live ? "Signing up now" : "Next sign-up"),
    el("div", { class: "what" }, n.title || `Lesson ${n.lesson_id}`),
    el("div", { class: "where" }, [shortDate(n.start), n.location].filter(Boolean).join(" · ")),
    live ? el("div", { class: "count live" }, "Enrollment is open")
         : el("div", { class: "count" }, el("small", {}, "opens in"), countdown(opens)));
  box.onclick = () => openSheet(n);
}

function renderHealth(h) {
  const box = $("#health");
  let cls = "ok", text;
  if (!h.timer) { cls = "bad"; text = [el("b", {}, "Background checks are off"), " — nothing gets signed up. Tap to turn them on."]; }
  else if (h.late) { cls = "bad"; text = [el("b", {}, "The bot hasn't run "), h.last_run ? ago(h.last_run) : "yet"]; }
  else if (h.last_kind === "error") { cls = "warn"; text = [el("b", {}, "Last check had an error"), " · tap for the log"]; }
  else text = [el("b", {}, h.running ? "Bot is checking now" : "Bot ok"),
    h.last_run ? ` · checked ${ago(h.last_run)}` : "", h.next ? ` · next ${hm(h.next)}` : ""];
  box.replaceChildren(el("span", { class: "health-dot " + cls }), el("span", { class: "txt" }, text), icon("chev", "dim"));
  box.onclick = () => setPage("bot");
  setHealthBadge(h);
}

// The days of week idx (0 = this week) as calendar dates in Zurich. Counted in
// days, not 7×24 h, so the clock change in spring/autumn doesn't shift them.
function weekDays(idx) {
  const p = parts(new Date(new Date(state.week.week_start).getTime() + 12 * 3600000));
  return Array.from({ length: 7 }, (_, i) => {
    const noon = new Date(Date.UTC(+p.year, +p.month - 1, +p.day + idx * 7 + i, 11));  // midday in Zurich
    return { key: dayKey(noon), date: noon, parts: parts(noon) };
  });
}

function renderWeek() {
  const w = state.week;
  renderNextUp();
  renderHealth(w.health);
  const pend = $("#pending");
  pend.hidden = !w.pending.length;
  if (w.pending.length) pend.replaceChildren(el("div", { class: "card row" }, icon("clock", "st warn"),
    el("div", { class: "row-text" }, el("div", { class: "row-title" }, "Waiting for the bot to finish its run"),
      el("div", { class: "row-sub" }, w.pending.join(" · ")))));

  state.weekIdx = Math.max(0, Math.min(state.weekIdx, w.weeks - 1));
  const days = weekDays(state.weekIdx);
  const lbl = (d) => d.toLocaleDateString("en-GB", { timeZone: ZURICH, day: "numeric", month: "short" });
  $("#wk-label").replaceChildren(
    state.weekIdx === 0 ? "This week" : state.weekIdx === 1 ? "Next week" : `In ${state.weekIdx} weeks`,
    el("small", {}, `${lbl(days[0].date)} – ${lbl(days[6].date)}`));
  $("#wk-prev").disabled = state.weekIdx === 0;
  $("#wk-next").disabled = state.weekIdx >= w.weeks - 1;
  renderPause(days);

  const byDay = {};
  for (const it of w.items) (byDay[dayKey(it.start)] ||= []).push(it);
  const grid = $("#week-grid");
  grid.replaceChildren();
  grid.classList.toggle("paused", w.paused_weeks.includes(mondayKey(days)));
  const today = dayKey(now());
  for (const { key, parts: p } of days) {
    const items = byDay[key] || [];
    const body = el("div", { class: "day-body" });
    if (!items.length) body.append(el("div", { class: "day-empty" }, "—"));
    for (const it of items) body.append(blockEl(it));
    grid.append(el("div", { class: "day" + (key === today ? " today" : "") + (key < today ? " past" : "") + (items.length ? "" : " empty") },
      el("div", { class: "day-label" }, el("div", { class: "wd" }, p.weekday), el("div", { class: "dn" }, String(+p.day))), body));
  }
  const kinds = new Set(w.items.map((i) => i.kind));
  $("#legend").replaceChildren(...Object.entries(KIND).filter(([k]) => kinds.has(k))
    .map(([, [ic, cls, label]]) => el("span", {}, icon(ic, "st " + cls), label)));
  renderCourses();
}

function blockEl(it) {
  const [ic, cls] = KIND[it.kind] || KIND.queued;
  const sub = it.overlaps ? el("span", { class: "warn-txt" }, `⚠ overlaps ${it.overlaps[0]}`)
    : it.kind === "signed" && it.place ? `place ${it.place}` : it.kind === "starred" && it.free != null
    ? (it.free > 0 ? `${it.free} free` : "full") : it.location || "";
  return el("button", { class: "block " + it.kind + (it.overlaps ? " overlap" : ""), color: sportColor(sportOf(it)), onclick: () => openSheet(it) },
    el("div", { class: "time" }, hm(it.start), el("small", {}, hm(it.end))),
    el("div", { class: "bt" }, el("div", { class: "n" }, it.title || `Lesson ${it.lesson_id}`), el("div", { class: "p" }, sub)),
    icon(ic, "st " + cls));
}

// "YYYY-MM-DD" of the week's Monday, the key the server uses for pauses
function mondayKey(days) { return days[0].key; }

function renderPause(days) {
  const box = $("#wk-pause"), key = mondayKey(days);
  const paused = state.week.paused_weeks.includes(key);
  const past = days[6].key < dayKey(now());
  box.replaceChildren();
  if (past) return;
  const go = (on) => async (ev) => {
    const btn = ev.currentTarget;  // reset to null once the click is over, so keep it before awaiting
    if (on && !await confirmDialog("Pause this week?", "All sign-ups of this week are taken off, and weekly courses "
      + "skip it. Resume puts them back (if enrollment is still ahead).", "Pause", false)) return;
    busy(btn, async () => {
      try { const r = await api("pause", { week: key, on }); toast(r.message, r.pending ? 7000 : 4000); } catch (e) { toast(e.message); }
      loadWeek();
    });
  };
  if (paused) box.append(el("div", { class: "paused-note" }, icon("clock"), "Paused — nothing gets signed up this week",
    el("button", { class: "btn small", onclick: go(false) }, "Resume")));
  else box.append(el("button", { class: "btn small ghost", onclick: go(true) }, "Pause this week"));
}

$("#wk-prev").addEventListener("click", () => { state.weekIdx--; renderWeek(); });
$("#wk-next").addEventListener("click", () => { state.weekIdx++; renderWeek(); });

// ---- the sheet with details and actions for one block
function closeSheet() { $("#sheet").hidden = true; }
$("#sheet").addEventListener("click", (ev) => { if (ev.target.id === "sheet") closeSheet(); });

function openSheet(it) {
  const [ic, cls, label] = KIND[it.kind] || KIND.queued;
  const body = $("#sheet-body");
  let status = label;
  if (it.kind === "queued" && it.enroll_start) status = `Enrollment opens ${shortDate(it.enroll_start)} (in ${countdown(it.enroll_start)})`;
  if (it.kind === "signed" && it.place) status = `Signed up · place ${it.place}`;
  if ((it.kind === "failed" || it.kind === "retrying") && it.error) status = `${label}: ${it.error}`;
  if (it.kind === "starred") status = it.bookable === false ? "Starred · enrollment is closed for this date"
    : `Starred · not signed up${it.enroll_start ? ` · enrollment opens ${shortDate(it.enroll_start)}` : ""}`;
  const actions = el("div", { class: "actions" });
  body.replaceChildren(el("div", { class: "grab" }),
    el("h3", {}, it.title || `Lesson ${it.lesson_id}`),
    el("div", { class: "meta" }, [`${shortDate(it.start)}–${hm(it.end)}`, it.location, it.niveau].filter(Boolean).join(" · ")),
    el("div", { class: "stline" }, icon(ic, "st " + cls), el("div", {}, status)),
    it.overlaps ? el("div", { class: "paused-note" }, `⚠ Overlaps with ${it.overlaps.join(", ")}`) : null, actions);
  const after = async () => { closeSheet(); await loadWeek(); };

  if (it.kind === "starred" && it.bookable !== false) {
    const watch = el("input", { type: "checkbox", class: "switch", checked: true });
    watch.checked = true;
    actions.append(el("label", { class: "opt" }, "If full, wait for a spot", watch),
      el("button", { class: "btn primary wide", onclick: (ev) => busy(ev.currentTarget, async () => {
        try { const r = await api("signup_lesson", { sport: it.sport_id, key: it.offer_key, lesson_id: it.lesson_id, watch: watch.checked });
              toast(r.message, r.pending ? 7000 : 3000); showOverlaps(r); await after(); } catch (e) { toast(e.message); }
      }) }, "Sign up for this date"),
      el("button", { class: "btn wide", onclick: (ev) => busy(ev.currentTarget, async () => {
        try { const r = await api("signup", { sport: it.sport_id, keys: [it.offer_key], weekly: true, watch: watch.checked });
              toast(r.pending ? r.message : `Every week · ${plural(r.queued, "date")} queued`, r.pending ? 7000 : 3000); showOverlaps(r); await after(); }
        catch (e) { toast(e.message); }
      }) }, "Sign up every week"));
  }
  if (it.job_id != null && ["queued", "retrying", "enrolling"].includes(it.kind)) {
    if (it.offer_key && state.plan && state.plan.entries.some((e) => e.key === it.offer_key)) {
      const sw = el("input", { type: "checkbox", class: "switch" });
      sw.checked = !!it.weekly;
      sw.addEventListener("change", async () => {
        try { const r = await api("plan/weekly", { key: it.offer_key, weekly: sw.checked }); if (r.message) toast(r.message); }
        catch (e) { toast(e.message); sw.checked = !sw.checked; }
        loadWeek();
      });
      actions.append(el("label", { class: "opt" }, "Every week", sw));
    }
    actions.append(el("button", { class: "btn danger wide", onclick: (ev) => busy(ev.currentTarget, async () => {
      if (!await confirmDialog("Skip this date?", `${it.title || "Lesson " + it.lesson_id}\n${shortDate(it.start)}`, "Skip")) return;
      try { const r = await api("jobs/remove", { id: it.job_id }); toast(r.message, r.pending ? 7000 : 3000); await after(); }
      catch (e) { toast(e.message); }
    }) }, "Skip this date"));
  }
  if (it.kind === "watching" && it.watcher_id != null) {
    actions.append(el("button", { class: "btn danger wide", onclick: (ev) => busy(ev.currentTarget, async () => {
      try { const r = await api("watchers/stop", { id: it.watcher_id }); toast(r.message); await after(); } catch (e) { toast(e.message); }
    }) }, "Stop watching"));
  }
  if (it.offer_key) {
    const starred = !!state.favs.offers[it.offer_key];
    actions.append(el("button", { class: "btn wide", onclick: (ev) => busy(ev.currentTarget, async () => {
      try { await setFav("offer", it.offer_key, !starred, { sport: it.sport_id || it.offer_key.split("|")[0] }); await after(); }
      catch (e) { toast(e.message); }
    }) }, icon("star"), starred ? "Remove star" : "Star this course"));
  }
  actions.append(el("a", { class: "btn ghost wide", href: it.url, target: "_blank", rel: "noopener noreferrer" }, icon("ext"), "Open on schalter.asvz.ch"));
  $("#sheet").hidden = false;
}

// ---- your courses: plan entries + starred courses
async function renderCourses() {
  if (!state.calShown) { state.calShown = true; renderCalendar(); }  // not on every refresh
  try { state.plan = await api("plan"); } catch { return; }
  const list = $("#courses");
  list.replaceChildren();
  const inPlan = new Set(state.plan.entries.map((e) => e.key));
  const starredOnly = Object.entries(state.favs.offers).filter(([k]) => !inPlan.has(k));
  if (!state.plan.entries.length && !starredOnly.length)
    list.append(el("div", { class: "row" }, el("div", { class: "row-text" }, el("div", { class: "row-title" }, "No courses yet"),
      el("div", { class: "row-sub" }, "Go to Find, star the trainings you like and sign up — every week if you want."))));
  for (const e of state.plan.entries) {
    const sw = el("input", { type: "checkbox", class: "switch", "aria-label": "Every week" });
    sw.checked = !!e.weekly;
    sw.addEventListener("change", async () => {
      try { const r = await api("plan/weekly", { key: e.key, weekly: sw.checked }); if (r.message) toast(r.message, r.pending ? 7000 : 3000); }
      catch (err) { toast(err.message); sw.checked = !sw.checked; }
      loadWeek();
    });
    list.append(el("div", { class: "row offer", color: sportColor(e.sport_name) },
      el("div", { class: "row-text" }, el("div", { class: "row-title" }, `${e.sport_name}: ${e.title}`),
        el("div", { class: "row-sub" }, `${WEEKDAYS[e.weekday]}s ${e.start}–${e.end} · ${e.location}` + (e.watch ? " · waits if full" : ""))),
      el("div", { class: "row-end" },
        starButton(!!state.favs.offers[e.key], "Star", (on) => setFav("offer", e.key, on, { sport: e.key.split("|")[0] })),
        el("label", { class: "switch-label dim caption" }, "Weekly", sw),
        el("button", { class: "btn ghost icon", "aria-label": "Remove", title: "Remove from your courses", html: ICON.trash,
          onclick: async () => {
            if (!await confirmDialog("Remove this course?", `${e.sport_name}: ${e.title} (${WEEKDAYS[e.weekday]} ${e.start}).\nIts queued dates are removed too.`, "Remove")) return;
            try { const r = await api("plan/remove", { key: e.key }); toast(r.message, r.pending ? 7000 : 3000); } catch (err) { toast(err.message); }
            loadWeek();
          } }))));
  }
  for (const [key, m] of starredOnly) {
    list.append(el("div", { class: "row offer", color: sportColor(m.sport_name) },
      el("div", { class: "row-text" }, el("div", { class: "row-title" }, `${m.sport_name}: ${m.title}`),
        el("div", { class: "row-sub" }, `${WEEKDAYS[m.weekday]}s ${m.start}–${m.end} · ${m.location} · starred, not signed up`)),
      el("div", { class: "row-end" },
        starButton(true, "Star", async (on) => { await setFav("offer", key, on, { sport: m.sport_id }); loadWeek(); }),
        el("button", { class: "btn small primary", onclick: (ev) => busy(ev.currentTarget, async () => {
          try { const r = await api("signup", { sport: m.sport_id, keys: [key], weekly: true, watch: true });
                toast(r.pending ? r.message : `Every week · ${plural(r.queued, "date")} queued`, r.pending ? 7000 : 3000); showOverlaps(r); }
          catch (err) { toast(err.message); }
          loadWeek();
        }) }, "Every week"))));
  }
}
async function renderCalendar() {
  let c;
  try { c = await api("calendar"); } catch { return; }
  const url = c.url || location.origin + c.path;
  const webcal = url.replace(/^https?:/, "webcal:");
  const box = $("#calendar");
  const on = el("input", { type: "checkbox", class: "switch", "aria-label": "Calendar link" });
  on.checked = c.enabled;
  on.addEventListener("change", async () => { try { await api("calendar", { enabled: on.checked }); } catch (e) { toast(e.message); } renderCalendar(); });
  box.replaceChildren(el("label", { class: "row tap" }, el("div", { class: "row-text" },
    el("div", { class: "row-title" }, "Calendar link"),
    el("div", { class: "row-sub" }, c.enabled ? "On — your calendar app can read it" : "Off — the link doesn't work")), on));
  if (!c.enabled) return;
  const copy = el("button", { class: "btn small", onclick: async () => {
    try { await navigator.clipboard.writeText(url); toast("Link copied"); } catch { toast(url, 9000); } } }, "Copy");
  box.append(
    el("div", { class: "inline-form" }, el("span", { class: "topic" }, url), copy),
    el("div", { class: "inline-form" },
      el("a", { class: "btn primary", href: "https://calendar.google.com/calendar/r?cid=" + encodeURIComponent(webcal),
        target: "_blank", rel: "noopener noreferrer" }, "Add to Google Calendar"),
      el("a", { class: "btn", href: webcal }, "Other calendar app"),
      el("button", { class: "btn ghost", onclick: async () => {
        if (!await confirmDialog("New link?", "The old link stops working. Subscribe again with the new one.", "New link", false)) return;
        try { await api("calendar", { new_link: true }); } catch (e) { toast(e.message); } renderCalendar();
      } }, "New link")),
    el("div", { class: "row" }, el("div", { class: "row-sub" },
      "Keep the link private: whoever has it sees your sign-ups (course names and times, nothing else). "
      + "Google Calendar refreshes subscribed calendars every few hours, so changes show up with a delay.")));
}

$("#ics-plan").addEventListener("click", (ev) => busy(ev.currentTarget, async () => {
  try { download(await api("ics", { plan: true }), "asvz-plan.ics", "text/calendar"); toast("Saved — import it in Google Calendar (Settings → Import & export)", 6000); }
  catch (e) { toast(e.message); }
}));

// ---------------------------------------------------------------- Find

function buildFilters() {
  WD_SHORT.forEach((d, i) => $("#days").append(el("button", { "aria-pressed": "false", title: WEEKDAYS[i],
    onclick: (ev) => { const b = ev.currentTarget; b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true"); applyFilters(); } },
    d.slice(0, 2))));
  TIME_FILTERS.forEach(([label], i) => $("#f-time").append(el("option", { value: i }, label)));
  for (const id of ["#f-time", "#f-place", "#f-level", "#f-sport"]) $(id).addEventListener("change", applyFilters);
  $("#f-text").addEventListener("input", applyFilters);
  $("#offers-reload").addEventListener("click", () => loadOffers(true));
  $("#sport-search").addEventListener("input", renderSports);
  $("#sport-search").addEventListener("keydown", (ev) => { if (ev.key === "Enter") { const f = $("#sport-list li[data-id]"); if (f) f.click(); } });
  $("#sel-clear").addEventListener("click", () => { state.selected.clear(); syncChecks(); updateSelection(); });
  $("#sel-signup").addEventListener("click", (ev) => busy(ev.currentTarget, signUp));
  $("#sel-ics").addEventListener("click", (ev) => busy(ev.currentTarget, exportSelection));
}

const ALL = () => ({ id: "all", name: "All sports", count: `next ${state.timetableDays} days` });
const STARRED = () => ({ id: "starred", name: "Starred courses", count: Object.keys(state.favs.offers).length });

async function loadSports(force = false) {
  const st = $("#sports-state");
  st.hidden = false; statusPage(st, "loading");
  try {
    const r = await api("sports" + (force ? "?force=1" : ""));
    state.sports = r.sports; state.timetableDays = r.timetable_days;
    st.hidden = true;
    try { state.favs = await api("favs"); } catch { /* keep */ }
    renderSports();
    const last = load("sport", "all");
    const sport = last === "all" ? ALL() : last === "starred" ? STARRED() : state.sports.find((s) => s.id === last);
    if (sport) selectSport(sport, load("showOffers", false));
  } catch (e) {
    if (e.status === 401) return;
    statusPage(st, "error", "Can't reach ASVZ", e.message, el("button", { class: "btn", onclick: () => loadSports(true) }, "Try again"));
  }
}

function sportItem(s, special) {
  const li = el("li", { class: special ? "special" : "", role: "option", "data-id": s.id,
    "aria-selected": state.sport && state.sport.id === s.id ? "true" : "false", onclick: () => selectSport(s, true) });
  if (special) li.append(icon(s.id === "starred" ? "star" : "list", "dim"));
  else { const dot = el("span", { class: "dot" }); dot.style.background = sportColor(s.name); li.append(dot); }
  li.append(el("span", { class: "name" }, s.name), el("span", { class: "count" }, s.count));
  if (!special) li.append(starButton(!!state.favs.sports[s.id], `Star ${s.name}`, async (on) => {
    await setFav("sport", s.id, on, { name: s.name }); renderSports();
  }));
  return li;
}

function renderSports() {
  const q = $("#sport-search").value.trim().toLowerCase();
  const list = $("#sport-list");
  list.replaceChildren();
  const match = (s) => !q || s.name.toLowerCase().includes(q);
  if (!q) {
    if (Object.keys(state.favs.offers).length) list.append(sportItem(STARRED(), true));
    list.append(sportItem(ALL(), true));
  }
  const starred = state.sports.filter((s) => state.favs.sports[s.id] && match(s));
  if (starred.length) { list.append(el("li", { class: "sep" }, "Your sports")); starred.forEach((s) => list.append(sportItem(s))); }
  const rest = state.sports.filter((s) => !state.favs.sports[s.id] && match(s));
  if (rest.length) { if (starred.length) list.append(el("li", { class: "sep" }, "All sports A–Z")); rest.forEach((s) => list.append(sportItem(s))); }
}

function selectSport(sport, showOffers) {
  const changed = !state.sport || state.sport.id !== sport.id;
  state.sport = sport; save("sport", sport.id);
  if (showOffers) $(".split").classList.add("show-offers");
  save("showOffers", $(".split").classList.contains("show-offers"));
  renderSports(); updateHeader();
  if (changed) { state.selected.clear(); updateSelection(); loadOffers(); }
}

let offersGen = 0;
async function loadOffers(force = false) {
  if (!state.sport) return;
  const gen = ++offersGen, sport = state.sport;
  $("#offer-list").replaceChildren(); $("#summary").textContent = "";
  statusPage($("#offers-state"), "loading");
  try {
    const r = await api(`offers?sport=${encodeURIComponent(sport.id)}${force ? "&force=1" : ""}`);
    if (gen !== offersGen) return;
    const today = zurichWeekday();
    state.offers = r.offers.sort((a, b) => ((a.weekday - today + 7) % 7) - ((b.weekday - today + 7) % 7)
      || a.start.localeCompare(b.start) || a.sport_name.localeCompare(b.sport_name) || a.title.localeCompare(b.title));
    fillSelect("#f-place", "All locations", state.offers.map((o) => o.location));
    fillSelect("#f-level", "All levels", state.offers.map((o) => o.niveau));
    fillSelect("#f-sport", "All sports", state.offers.map((o) => o.sport_name));
    $("#f-sport").hidden = !["all", "starred"].includes(sport.id);
    if (!state.plan) { try { state.plan = await api("plan"); } catch { /* shown without plan marks */ } }
    rebuildOffers();
  } catch (e) {
    if (gen !== offersGen || e.status === 401) return;
    statusPage($("#offers-state"), "error", "Couldn't load offers", e.message, el("button", { class: "btn", onclick: () => loadOffers(true) }, "Try again"));
  }
}

function fillSelect(sel, allLabel, values) {
  const s = $(sel), keep = s.value;
  s.replaceChildren(el("option", { value: "" }, allLabel));
  for (const v of [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b))) s.append(el("option", { value: v }, v));
  s.value = [...s.options].some((o) => o.value === keep) ? keep : "";
}

function offerRow(o, inPlan) {
  let name = o.title;
  if ((o.partial || state.sport.id === "starred") && o.sport_name && o.sport_name !== o.title) name = `${o.sport_name}: ${o.title}`;
  const check = el("input", { type: "checkbox", class: "check", "aria-label": name });
  check.checked = state.selected.has(o.key);
  check.addEventListener("change", () => { if (check.checked) state.selected.add(o.key); else state.selected.delete(o.key); updateSelection(); });
  const end = el("div", { class: "row-end" });
  if (inPlan) end.append(el("span", { class: "chip ok" }, "In your courses"));
  if (o.next) {
    end.append(el("span", { class: "dim caption" }, `next ${parts(o.next.start).day}.${parts(o.next.start).month}.`));
    if (o.next.free !== null && o.next.free !== undefined) end.append(el("span", { class: "chip " + (o.next.free <= 0 ? "bad" : "accent") }, o.next.free <= 0 ? "full" : `${o.next.free} free`));
    else if (o.next.max) end.append(el("span", { class: "chip" }, `${o.next.max} places`));
  } else end.append(el("span", { class: "dim caption" }, "no open dates"));
  end.append(starButton(!!state.favs.offers[o.key], "Star this course", (on) => setFav("offer", o.key, on, { sport: o.sport_id })));
  const row = el("label", { class: "row offer", color: sportColor(o.sport_name) }, check,
    el("div", { class: "row-text" }, el("div", { class: "row-title" }, el("b", {}, `${o.start}–${o.end}`), name),
      el("div", { class: "row-sub" }, [o.location, o.niveau, o.instructor].filter(Boolean).join(" · "))), end);
  row.offer = o; row.check = check;
  return row;
}

let rows = [];
function rebuildOffers() {
  const inPlan = new Set((state.plan ? state.plan.entries : []).map((e) => e.key));
  rows = state.offers.map((o) => offerRow(o, inPlan.has(o.key)));
  applyFilters(); updateSelection();
}

function applyFilters() {
  const list = $("#offer-list"), st = $("#offers-state");
  if (!state.sport) { statusPage(st, "empty", "Pick a sport", "Choose a sport to see its weekly courses."); return; }
  const days = new Set([...document.querySelectorAll("#days button")].map((b, i) => (b.getAttribute("aria-pressed") === "true" ? i : -1)).filter((i) => i >= 0));
  const [, lo, hi] = TIME_FILTERS[+$("#f-time").value || 0];
  const place = $("#f-place").value, level = $("#f-level").value;
  const sport = ["all", "starred"].includes(state.sport.id) ? $("#f-sport").value : "";
  const q = $("#f-text").value.trim().toLowerCase();
  const frag = document.createDocumentFragment();
  let shown = 0, lastDay = -1, box = null;
  for (const r of rows) {
    const o = r.offer, hour = +o.start.slice(0, 2);
    const ok = (!days.size || days.has(o.weekday)) && lo <= hour && hour < hi && (!place || o.location === place)
      && (!level || o.niveau === level) && (!sport || o.sport_name === sport)
      && (!q || `${o.title} ${o.sport_name} ${o.instructor}`.toLowerCase().includes(q));
    if (!ok) continue;
    if (o.weekday !== lastDay) { frag.append(el("div", { class: "weekday-header" }, WEEKDAYS[o.weekday])); box = el("div", { class: "list" }); frag.append(box); lastDay = o.weekday; }
    box.append(r); shown++;
  }
  list.replaceChildren(frag);
  $("#summary").textContent = rows.length ? `${shown} of ${plural(rows.length, "weekly course")} · ${state.sport.name}` : "";
  if (!rows.length) statusPage(st, "empty", state.sport.id === "starred" ? "No starred courses" : "No courses",
    state.sport.id === "starred" ? "Tap ★ next to a course to keep it here." : "ASVZ has no upcoming lessons for this right now.");
  else if (!shown) statusPage(st, "empty", "No matching courses", "Try fewer filters.");
  else st.replaceChildren();
}

function syncChecks() { for (const r of rows) r.check.checked = state.selected.has(r.offer.key); }
function updateSelection() {
  const n = state.selected.size;
  $("#sel-label").textContent = `${plural(n, "course")} selected`;
  $("#selection-bar").hidden = n === 0;
}

async function signUp() {
  const keys = [...state.selected];
  if (!keys.length) return;
  const weekly = $("#sel-weekly").checked, watch = $("#sel-watch").checked;
  try {
    const r = await api("signup", { sport: state.sport.id, keys, weekly, watch });
    state.selected.clear(); updateSelection();
    if (r.pending) { toast(r.message, 7000); syncChecks(); return; }
    toast(`Signed up for ${plural(r.offers, "course")} (${weekly ? "every week" : "the next date"}) · ${plural(r.queued, "date")} queued`);
    r.messages.forEach((m, i) => setTimeout(() => toast(m, 6000), 700 * (i + 1)));
    showOverlaps(r, 700 * (r.messages.length + 1));
    try { state.plan = await api("plan"); } catch { /* ignore */ }
    rebuildOffers();
  } catch (e) { toast(e.message, 6000); }
}

async function exportSelection() {
  try {
    const text = await api("ics", { sport: state.sport.id, keys: [...state.selected], weekly: $("#sel-weekly").checked });
    download(text, `asvz-${state.sport.name.toLowerCase().replace(/\s+/g, "-")}.ics`, "text/calendar");
    toast("Saved — import it in Google Calendar (Settings → Import & export)", 6000);
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- Bot

async function loadStatus() {
  try {
    const s = await api("status");
    state.serverOffset = new Date(s.server_time) - Date.now();
    $("#act-timer").checked = !!s.timer;
    $("#act-timer-sub").textContent = `Your bot's timer (${s.timer_unit}) processes due sign-ups, phone or no phone.`;
    $("#act-login").textContent = s.credentials.set
      ? "Saved on the server. This page can't read it — only whether it exists."
      : "No login saved on the server yet — see README (“ASVZ login”).";
    $("#act-server").textContent = (s.running ? "The bot is running a check now" : "Idle")
      + (s.timer && s.timer_next ? ` · next check ${hm(s.timer_next)} (in ${countdown(s.timer_next)})` : "")
      + (s.pending ? ` · ${plural(s.pending, "change")} waiting` : "")
      + (s.helper_error ? ` · ERROR: ${s.helper_error}` : "");
  } catch (e) { if (e.status !== 401) $("#act-server").textContent = e.message; }
  loadNotify();
}

async function setTimer(on) {
  if (!on && !await confirmDialog("Turn off background checks?", "Your bot then signs up for nothing until you turn them on again — also not from the text menu's queue.", "Turn off")) {
    loadStatus(); return;
  }
  try { toast((await api("timer", { on })).message); } catch (e) { toast(e.message); }
  loadStatus();
}
$("#act-timer").addEventListener("change", (ev) => setTimer(ev.target.checked));

$("#act-run").addEventListener("click", (ev) => busy(ev.currentTarget, async () => {
  let r;
  try { r = await api("run", {}); } catch (e) { toast(e.message); return; }
  toast("Check started");
  for (let i = 0; i < 30; i++) {
    await new Promise((ok) => setTimeout(ok, 2000));
    const log = await loadLog();
    const run = log && log.runs.find((x) => x.start >= r.started - 3000);
    if (run && run.end) { toast(`Check done: ${run.label}`); return; }
  }
}));

const RUN_CHIP = { error: "bad", missed: "bad", enrolled: "ok", watching: "warn", enrolling: "accent", idle: "", checked: "" };
const openRuns = new Set();
async function loadLog() {
  let r;
  try { r = await api("log"); }
  catch (e) { $("#runs").replaceChildren(el("div", { class: "row" }, el("div", { class: "row-sub" }, e.message))); return null; }
  const list = $("#runs");
  list.replaceChildren();
  if (!r.runs.length) list.append(el("div", { class: "row" }, el("div", { class: "row-sub" }, "No runs in the log yet.")));
  for (const run of r.runs.slice(0, 40)) {
    const key = String(run.start);
    const lines = el("pre", { class: "log-view" }, run.lines.map((l) => `${clock(l.t)}  ${l.msg}`).join("\n") || "(no output)");
    lines.hidden = !openRuns.has(key);
    const row = el("div", { class: "row tap", role: "button", tabindex: "0",
      onclick: () => { lines.hidden = !lines.hidden; if (lines.hidden) openRuns.delete(key); else openRuns.add(key); } },
      el("div", { class: "row-text" },
        el("div", { class: "row-title" }, el("span", { class: "tnum" }, clock(run.start).slice(0, 5)), " ", el("span", { class: "dim caption" }, ago(run.start))),
        run.detail && run.kind !== "idle" ? el("div", { class: "row-sub" }, run.detail) : null),
      el("span", { class: "chip " + (run.end ? RUN_CHIP[run.kind] ?? "" : "accent") }, run.end ? run.label : "Running…"));
    list.append(el("div", {}, row, lines));
  }
  const wl = $("#watch-logs");
  wl.replaceChildren();
  $("#watch-group").hidden = !r.watchers.length;
  for (const w of r.watchers) {
    const pre = el("pre", { class: "log-view" }, w.text || "(empty)");
    pre.hidden = true;
    wl.append(el("div", {}, el("div", { class: "row tap", role: "button", tabindex: "0", onclick: () => { pre.hidden = !pre.hidden; } },
      el("div", { class: "row-text" }, el("div", { class: "row-title" }, w.name), el("div", { class: "row-sub" }, `last written ${ago(w.modified)}`))), pre));
  }
  return r;
}
$("#log-reload").addEventListener("click", loadLog);

// ---- notifications
async function loadNotify() {
  let n;
  try { n = await api("notify"); } catch { return; }
  const box = $("#notify");
  box.replaceChildren();
  const on = el("input", { type: "checkbox", class: "switch", "aria-label": "Notifications" });
  on.checked = n.enabled;
  on.addEventListener("change", async () => { try { await api("notify", { enabled: on.checked }); toast(on.checked ? "Notifications on" : "Notifications off"); } catch (e) { toast(e.message); } loadNotify(); });
  box.append(el("label", { class: "row tap" }, el("div", { class: "row-text" }, el("div", { class: "row-title" }, "Send notifications"),
    el("div", { class: "row-sub" }, n.enabled ? `to topic ${n.topic}` : "Off")), on));
  const copy = el("button", { class: "btn small", onclick: async () => {
    try { await navigator.clipboard.writeText(n.topic); toast("Topic copied"); } catch { toast("Copy it by hand: " + n.topic, 7000); } } }, "Copy");
  box.append(el("div", {},
    el("ol", { class: "steps" },
      el("li", {}, "Install ", el("b", {}, "ntfy"), " from the Play Store (free, by Philipp Heckel)."),
      el("li", {}, "In ntfy tap ", el("b", {}, "+"), ", enter this topic, keep the server ", el("b", {}, n.server.replace("https://", "")), ", subscribe:",
        el("div", { class: "inline-form topic-row" }, el("span", { class: "topic" }, n.topic), copy)),
      el("li", {}, "Switch notifications on above and tap ", el("b", {}, "Send test"), ".")),
    el("div", { class: "inline-form" },
      el("button", { class: "btn primary", onclick: (ev) => busy(ev.currentTarget, async () => { try { toast((await api("notify/test", {})).message, 6000); } catch (e) { toast(e.message, 6000); } }) }, "Send test"),
      el("button", { class: "btn ghost", onclick: async () => {
        if (!await confirmDialog("New topic?", "Your phone then has to subscribe to the new topic. Use it if someone else might know the old one.", "New topic", false)) return;
        try { await api("notify", { new_topic: true }); } catch (e) { toast(e.message); } loadNotify();
      } }, "New topic"))));
  box.append(el("div", { class: "row" }, el("div", { class: "row-sub" },
    "The topic works like a password: whoever knows it can read the messages. They contain course names and times only — never your login.")));
  for (const [k, label] of Object.entries(n.labels)) {
    const sw = el("input", { type: "checkbox", class: "switch", "aria-label": label });
    sw.checked = n.events[k] !== false;
    sw.addEventListener("change", async () => { try { await api("notify", { events: { [k]: sw.checked } }); } catch (e) { toast(e.message); sw.checked = !sw.checked; } });
    box.append(el("label", { class: "row tap" }, el("div", { class: "row-text" }, el("div", { class: "row-sub" }, label)), sw));
  }
}

// ---- add by number (the text menu's "1) Add a lesson")
$("#add-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("#add-id");
  if (!input.value.trim()) return;
  busy(ev.target.querySelector("button"), async () => {
    toast("Reading the lesson page on the server… (up to a minute)");
    try { const r = await api("jobs/add", { lesson_id: input.value.trim(), watch: $("#add-watch").checked }); toast(r.message, 6000); input.value = ""; }
    catch (e) { toast(e.message, 8000); }
  });
});

// ---------------------------------------------------------------- start

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
boot();
