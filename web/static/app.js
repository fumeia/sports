// ASVZ Bot — phone remote. Same pages as the desktop app (app.py):
// Discover, My plan, Activity. All work happens on the server; this page
// only sends commands and shows status.
"use strict";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const WEEKDAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TIME_FILTERS = [["Any time", 0, 24], ["Morning (before 12)", 0, 12],
                      ["Afternoon (12–17)", 12, 17], ["Evening (from 17)", 17, 24]];
const ZURICH = "Europe/Zurich";
const ICONS = {
  discover: '<svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3.5 3.5"/></svg>',
  plan: '<svg viewBox="0 0 16 16"><rect x="2" y="3" width="12" height="11" rx="2"/><path d="M2 6.5h12M5 1.5v3M11 1.5v3"/></svg>',
  activity: '<svg viewBox="0 0 16 16"><path d="M1.5 8.5h3l2-5 3 9 2-4h3"/></svg>',
  trash: '<svg viewBox="0 0 16 16"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 9h5.8l.6-9"/></svg>',
  skip: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="m5.5 5.5 5 5M10.5 5.5l-5 5"/></svg>',
  eye: '<svg viewBox="0 0 16 16"><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8Z"/><circle cx="8" cy="8" r="2"/></svg>',
  web: '<svg viewBox="0 0 16 16"><path d="M9 2.5h4.5V7M13.5 2.5 7 9M11.5 9.5v3a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3"/></svg>',
  refresh: '<svg viewBox="0 0 16 16"><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v2.5h-2.5"/></svg>',
  list: '<svg viewBox="0 0 16 16"><path d="M5.5 4h8M5.5 8h8M5.5 12h8M2.5 4h0M2.5 8h0M2.5 12h0"/></svg>',
  chev: '<svg viewBox="0 0 16 16"><path d="m6 3 5 5-5 5"/></svg>',
  cal: '<svg viewBox="0 0 16 16"><rect x="2" y="3" width="12" height="11" rx="2"/><path d="M2 6.5h12M5 1.5v3M11 1.5v3M5 9.5h2"/></svg>',
};

const $ = (sel) => document.querySelector(sel);
const state = {
  page: "discover", sports: [], sport: null, offers: [], selected: new Set(),
  plan: null, status: null, timetableDays: 8, serverOffset: 0,
};

// ---------------------------------------------------------------- helpers

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;            // only for our own ICONS
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

function load(key, fallback) {
  try { const v = localStorage.getItem("vb." + key); return v === null ? fallback : JSON.parse(v); }
  catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem("vb." + key, JSON.stringify(value)); } catch { /* private mode */ }
}

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-VB": "1" },
  };
  let r;
  try { r = await fetch("api/" + path, { credentials: "same-origin", ...opts }); }
  catch { throw new HttpError(0, "Can't reach the server — check your connection."); }
  if (r.status === 401 && path !== "login") { showLogin(); throw new HttpError(401, "Please log in"); }
  const ctype = r.headers.get("Content-Type") || "";
  const data = ctype.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new HttpError(r.status, (data && data.error) || `Error ${r.status}`);
  return data;
}

function now() { return new Date(Date.now() + state.serverOffset); }

const fmtParts = new Intl.DateTimeFormat("en-GB", {
  timeZone: ZURICH, weekday: "short", day: "2-digit", month: "2-digit",
  hour: "2-digit", minute: "2-digit", hour12: false,
});
function parts(d) {
  const o = {};
  for (const p of fmtParts.formatToParts(d)) o[p.type] = p.value;
  return o;
}
function shortDate(iso) {  // "Tue 21.10. 18:15", like app.py's short_date
  if (!iso) return "";
  const p = parts(new Date(iso));
  return `${p.weekday} ${p.day}.${p.month}. ${p.hour}:${p.minute}`;
}
function dayMonth(iso) { const p = parts(new Date(iso)); return `${p.day}.${p.month}.`; }
function countdown(iso) {
  let secs = Math.floor((new Date(iso) - now()) / 1000);
  if (secs <= 0) return "now";
  const d = Math.floor(secs / 86400); secs %= 86400;
  const h = Math.floor(secs / 3600); const m = Math.floor((secs % 3600) / 60);
  if (d) return `in ${d}d ${h}h`;
  if (h) return `in ${h}h ${m}min`;
  return `in ${m} min`;
}
function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function zurichWeekday() { return (["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]).indexOf(parts(now()).weekday); }

function toast(text, ms = 4000) {
  const t = el("div", { class: "toast" + (text.length > 60 ? " long" : "") }, text);
  const box = $("#toasts");
  while (box.children.length >= 2) box.firstChild.remove();  // keep at most two on screen
  box.append(t);
  setTimeout(() => t.remove(), ms);
}

function confirmDialog(title, body, okLabel, destructive = true) {
  return new Promise((resolve) => {
    const d = $("#dialog");
    $("#dialog-title").textContent = title;
    $("#dialog-body").textContent = body;
    const ok = $("#dialog-ok");
    ok.textContent = okLabel;
    ok.className = "btn " + (destructive ? "destructive" : "suggested");
    d.hidden = false;
    const done = (v) => { d.hidden = true; ok.onclick = null; $("#dialog-cancel").onclick = null; resolve(v); };
    ok.onclick = () => done(true);
    $("#dialog-cancel").onclick = () => done(false);
  });
}

async function busy(btn, work) {
  btn.classList.add("busy");
  btn.disabled = true;
  try { return await work(); }
  finally { btn.classList.remove("busy"); btn.disabled = false; }
}

function statusPage(target, kind, title, desc, action) {
  target.replaceChildren();
  if (kind === "loading") target.append(el("div", { class: "spinner", role: "status", "aria-label": "Loading" }));
  else {
    if (title) target.append(el("h3", {}, title));
    if (desc) target.append(el("p", {}, desc));
    if (action) target.append(action);
  }
}

function download(text, filename, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a", { href: url, download: filename });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ---------------------------------------------------------------- login

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-form").user.focus();
}

async function boot() {
  let me;
  try { me = await api("me"); }
  catch (e) { $("#login").hidden = false; $("#login-error").textContent = e.message; return; }
  if (!me.logged_in) {
    if (!me.password_set) $("#login-error").textContent =
      "No page password yet. On the server run: deploy/setup.sh (it asks for one).";
    return showLogin();
  }
  $("#login").hidden = true;
  $("#app").hidden = false;
  startApp();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#login-error").textContent = "";
  const btn = f.querySelector("button");
  try {
    await busy(btn, () => api("login", { user: f.user.value.trim(), password: f.password.value }));
    f.password.value = "";
    boot();
  } catch (e) { $("#login-error").textContent = e.message; }
});

// ---------------------------------------------------------------- window

let started = false;
function startApp() {
  if (started) { refreshAll(); return; }
  started = true;
  buildSwitchers();
  buildFilters();
  setPage(load("page", "discover"));
  refreshStatus();
  loadSports();
  setInterval(tick, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
}

function refreshAll() {
  refreshStatus();
  if (state.page === "plan") refreshPlan();
  if (state.page === "activity") { refreshStatus(); loadLog(); }
}

function tick() {
  if (document.hidden) return;
  if (state.page === "plan") refreshPlan(true);
}
// The Activity page follows the bot live.
setInterval(() => {
  if (!document.hidden && started && state.page === "activity") { refreshStatus(); loadLog(); }
}, 10000);

function buildSwitchers() {
  const pages = [["discover", "Discover"], ["plan", "My plan"], ["activity", "Activity"]];
  for (const nav of [$("#switcher-top"), $("#switcher-bottom")]) {
    for (const [id, label] of pages) {
      const b = el("button", { "data-page": id, onclick: () => setPage(id) });
      b.innerHTML = ICONS[id];
      b.append(el("span", {}, label));
      nav.append(b);
    }
  }
}

function setPage(id) {
  state.page = id;
  save("page", id);
  for (const b of document.querySelectorAll(".switcher button"))
    if (b.dataset.page === id) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  for (const p of ["discover", "plan", "activity"]) $("#page-" + p).hidden = p !== id;
  updateHeader();
  if (id === "plan") refreshPlan();
  if (id === "activity") { refreshStatus(); loadLog(); }
  if (id === "discover" && state.sport) rebuildOffers();
}

function updateHeader() {
  const inOffers = state.page === "discover" && $(".split").classList.contains("show-offers");
  $("#back").hidden = !(inOffers && window.matchMedia("(max-width: 720px)").matches);
  $("#subtitle").textContent = state.page === "discover" && state.sport ? state.sport.name : "";
}
window.matchMedia("(max-width: 720px)").addEventListener("change", updateHeader);

$("#back").addEventListener("click", () => {
  $(".split").classList.remove("show-offers");
  updateHeader();
});

$("#menu-btn").addEventListener("click", (ev) => { ev.stopPropagation(); $("#menu").hidden = !$("#menu").hidden; });
document.addEventListener("click", () => { $("#menu").hidden = true; });
$("#menu").addEventListener("click", async (ev) => {
  const action = ev.target.dataset.action;
  $("#menu").hidden = true;
  if (action === "reload") location.reload();
  if (action === "logout") { try { await api("logout", {}); } finally { location.reload(); } }
  if (action === "about") confirmDialog("ASVZ Bot", "Remote for the ASVZ enrollment bot on your server.\n" +
    "Sign-ups run on the server, even when this page is closed.", "OK", false);
});

$("#banner-on").addEventListener("click", () => setTimer(true));

// ---------------------------------------------------------------- status

async function refreshStatus() {
  try {
    const s = await api("status");
    state.status = s;
    state.serverOffset = new Date(s.server_time) - Date.now();
    $("#banner").hidden = s.timer;
    $("#act-timer").checked = s.timer;
    $("#act-timer-sub").textContent = `Your bot's timer (${s.timer_unit}): processes due sign-ups, phone or no phone`;
    const c = s.credentials;
    $("#act-login").textContent = c.set
      ? "Saved on the server. This page can't read it — only whether it exists."
      : "No login saved on the server yet — see README (“ASVZ login”).";
    const next = s.timer && s.timer_next ? ` · next check ${clock(s.timer_next).slice(0, 5)} (${countdown(new Date(s.timer_next).toISOString())})` : "";
    const t = new Date(s.server_time);
    $("#act-server").textContent = (s.running ? "The bot is running a check now" : "Idle") + next +
      (s.pending ? ` · ${plural(s.pending, "change")} waiting` : "") +
      (s.scheduler_ok ? "" : " · ERROR: scheduler script not found") +
      ` · server clock ${t.toLocaleTimeString("en-GB", { timeZone: ZURICH, hour: "2-digit", minute: "2-digit" })} Zurich time` +
      "";
  } catch (e) { if (e.status !== 401) $("#act-server").textContent = e.message; }
}

async function setTimer(on) {
  if (!on && !await confirmDialog("Turn off background checks?",
      "Your bot then signs up for nothing until you turn them on again — also not from the text menu's queue.", "Turn off")) {
    refreshStatus(); return;
  }
  try {
    const r = await api("timer", { on });
    toast(r.message);
  } catch (e) { toast(e.message); }
  refreshStatus();
}
$("#act-timer").addEventListener("change", (ev) => setTimer(ev.target.checked));

$("#act-run").addEventListener("click", (ev) => busy(ev.currentTarget, async () => {
  let r;
  try { r = await api("run", {}); } catch (e) { toast(e.message); return; }
  toast("Check started");
  // Follow that run until it has finished (a sign-up can take up to ~20 min;
  // after a minute we stop waiting here, the list keeps updating anyway).
  for (let i = 0; i < 30; i++) {
    await new Promise((ok) => setTimeout(ok, 2000));
    const log = await loadLog();
    const run = log && log.runs.find((x) => x.start >= r.started - 3000);
    if (run && run.end) { toast(`Check done: ${run.label}`); return; }
  }
}));

const RUN_KIND = { error: "full", missed: "full", enrolled: "ok", watching: "", enrolling: "", idle: "dim", checked: "dim" };
const openRuns = new Set();

function ago(ms) {
  const s = Math.max(0, Math.round((now() - ms) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
function clock(ms) {
  return new Date(ms).toLocaleTimeString("en-GB", { timeZone: ZURICH, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function loadLog() {
  let r;
  try { r = await api("log"); }
  catch (e) { $("#runs").replaceChildren(el("div", { class: "row" }, el("div", { class: "row-sub" }, e.message))); return null; }
  const list = $("#runs");
  list.replaceChildren();
  if (!r.runs.length) list.append(el("div", { class: "row" }, el("div", { class: "row-sub" }, "No runs in the log yet.")));
  for (const run of r.runs.slice(0, 40)) {
    const key = String(run.start);
    const lines = el("pre", { class: "log-view run-lines" }, run.lines.map((l) => `${clock(l.t)}  ${l.msg}`).join("\n") || "(no output)");
    lines.hidden = !openRuns.has(key);
    const label = run.end ? run.label : "Running…";
    const row = el("div", { class: "row activatable run", role: "button", tabindex: "0",
      onclick: () => { lines.hidden = !lines.hidden; if (lines.hidden) openRuns.delete(key); else openRuns.add(key); } },
      el("div", { class: "row-text" },
        el("div", { class: "row-title" }, el("b", { class: "tnum" }, clock(run.start).slice(0, 5)), "  ", el("span", { class: "dim caption" }, ago(run.start))),
        run.detail && run.kind !== "idle" ? el("div", { class: "row-sub" }, run.detail) : null),
      el("span", { class: "pill-label " + (run.end ? RUN_KIND[run.kind] ?? "" : "") }, label));
    list.append(el("div", {}, row, lines));
  }
  const wl = $("#watch-logs");
  wl.replaceChildren();
  $("#watch-group").hidden = !r.watchers.length;
  for (const w of r.watchers) {
    const pre = el("pre", { class: "log-view run-lines" }, w.text || "(empty)");
    pre.hidden = true;
    wl.append(el("div", {}, el("div", { class: "row activatable", role: "button", tabindex: "0", onclick: () => { pre.hidden = !pre.hidden; } },
      el("div", { class: "row-text" }, el("div", { class: "row-title" }, w.name), el("div", { class: "row-sub" }, `last written ${ago(w.modified)}`))), pre));
  }
  return r;
}
$("#log-reload").addEventListener("click", loadLog);

// ---------------------------------------------------------------- Discover

function buildFilters() {
  const days = $("#days");
  WEEKDAYS_SHORT.forEach((d, i) => days.append(el("button", {
    "aria-pressed": "false", title: WEEKDAYS[i],
    onclick: (ev) => { const b = ev.currentTarget; b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true"); applyFilters(); },
  }, d.slice(0, 2))));
  TIME_FILTERS.forEach(([label], i) => $("#f-time").append(el("option", { value: i }, label)));
  for (const id of ["#f-time", "#f-place", "#f-level", "#f-sport"]) $(id).addEventListener("change", applyFilters);
  $("#f-text").addEventListener("input", applyFilters);
  $("#offers-reload").addEventListener("click", () => loadOffers(true));
  $("#sport-search").addEventListener("input", renderSports);
  $("#sport-search").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { const first = $("#sport-list li"); if (first) first.click(); }
  });
  $("#sel-clear").addEventListener("click", () => { state.selected.clear(); syncChecks(); updateSelection(); });
  $("#sel-signup").addEventListener("click", (ev) => busy(ev.currentTarget, signUp));
  $("#sel-ics").addEventListener("click", (ev) => busy(ev.currentTarget, exportSelection));
}

async function loadSports(force = false) {
  const st = $("#sports-state");
  st.hidden = false;
  statusPage(st, "loading");
  try {
    const r = await api("sports" + (force ? "?force=1" : ""));
    state.sports = r.sports;
    state.timetableDays = r.timetable_days;
    st.hidden = true;
    renderSports();
    const last = load("sport", "all");
    const sport = last === "all" ? allSports() : state.sports.find((s) => s.id === last);
    if (sport) selectSport(sport, load("showOffers", false));
  } catch (e) {
    if (e.status === 401) return;
    statusPage(st, "error", "Can't reach ASVZ", e.message,
      el("button", { class: "btn pill", onclick: () => loadSports(true) }, "Try again"));
  }
}

function allSports() { return { id: "all", name: "All sports", count: `next ${state.timetableDays} days` }; }

function renderSports() {
  const q = $("#sport-search").value.trim().toLowerCase();
  const list = $("#sport-list");
  list.replaceChildren();
  for (const s of [allSports(), ...state.sports]) {
    if (q && (s.id === "all" || !s.name.toLowerCase().includes(q))) continue;
    const li = el("li", { class: s.id === "all" ? "all" : "", role: "option",
      "aria-selected": state.sport && state.sport.id === s.id ? "true" : "false",
      onclick: () => selectSport(s, true) });
    if (s.id === "all") li.innerHTML = ICONS.list;
    li.append(el("span", { class: "name" }, s.name), el("span", { class: "count" }, s.count));
    const chev = el("span", { class: "chev" }); chev.innerHTML = ICONS.chev; li.append(chev);
    list.append(li);
  }
}

function selectSport(sport, showOffers) {
  const changed = !state.sport || state.sport.id !== sport.id;
  state.sport = sport;
  save("sport", sport.id);
  if (showOffers) $(".split").classList.add("show-offers");
  save("showOffers", $(".split").classList.contains("show-offers"));
  renderSports();
  updateHeader();
  if (changed) { state.selected.clear(); updateSelection(); loadOffers(); }
}

let offersGen = 0;
async function loadOffers(force = false) {
  if (!state.sport) return;
  const gen = ++offersGen;
  const sport = state.sport;
  $("#offer-list").replaceChildren();
  $("#summary").textContent = "";
  statusPage($("#offers-state"), "loading");
  try {
    const r = await api(`offers?sport=${encodeURIComponent(sport.id)}${force ? "&force=1" : ""}`);
    if (gen !== offersGen) return;  // user moved on meanwhile
    const today = zurichWeekday();
    state.offers = r.offers.sort((a, b) =>
      ((a.weekday - today + 7) % 7) - ((b.weekday - today + 7) % 7) ||
      a.start.localeCompare(b.start) || a.sport_name.localeCompare(b.sport_name) || a.title.localeCompare(b.title));
    fillSelect("#f-place", "All locations", state.offers.map((o) => o.location));
    fillSelect("#f-level", "All levels", state.offers.map((o) => o.niveau));
    fillSelect("#f-sport", "All sports", state.offers.map((o) => o.sport_name));
    $("#f-sport").hidden = sport.id !== "all";
    if (!state.plan) await refreshPlan(true, false);
    rebuildOffers();
  } catch (e) {
    if (gen !== offersGen || e.status === 401) return;
    statusPage($("#offers-state"), "error", "Couldn't load offers", e.message,
      el("button", { class: "btn pill", onclick: () => loadOffers(true) }, "Try again"));
  }
}

function fillSelect(sel, allLabel, values) {
  const s = $(sel);
  const keep = s.value;
  s.replaceChildren(el("option", { value: "" }, allLabel));
  for (const v of [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b)))
    s.append(el("option", { value: v }, v));
  s.value = [...s.options].some((o) => o.value === keep) ? keep : "";
}

function offerRow(o, inPlan) {
  let name = o.title;
  if (o.partial && o.sport_name && o.sport_name !== o.title) name = `${o.sport_name}: ${o.title}`;
  const check = el("input", { type: "checkbox", class: "check", "aria-label": name });
  check.checked = state.selected.has(o.key);
  check.addEventListener("change", () => {
    if (check.checked) state.selected.add(o.key); else state.selected.delete(o.key);
    updateSelection();
  });
  const suffix = el("div", { class: "row-suffix" });
  if (inPlan) suffix.append(el("span", { class: "pill-label ok" }, "In your plan"));
  if (o.next) {
    suffix.append(el("span", { class: "dim caption" }, `next ${dayMonth(o.next.start)}`));
    if (o.next.free !== null && o.next.free !== undefined) {
      const full = o.next.free <= 0;
      suffix.append(el("span", { class: "pill-label" + (full ? " full" : "") }, full ? "full" : `${o.next.free} free`));
    } else if (o.next.max) suffix.append(el("span", { class: "pill-label" }, `${o.next.max} places`));
  } else suffix.append(el("span", { class: "dim caption" }, "no open dates"));
  const row = el("label", { class: "row offer" }, check,
    el("div", { class: "row-text" },
      el("div", { class: "row-title" }, el("b", {}, `${o.start}–${o.end}`), name),
      el("div", { class: "row-sub" }, [o.location, o.niveau, o.instructor].filter(Boolean).join("  ·  "))),
    suffix);
  row.offer = o;
  row.check = check;
  return row;
}

let rows = [];
function rebuildOffers() {
  if (!state.offers) return;
  const inPlan = new Set((state.plan ? state.plan.entries : []).map((e) => e.key));
  rows = state.offers.map((o) => offerRow(o, inPlan.has(o.key)));
  applyFilters();
  updateSelection();
}

function applyFilters() {
  const list = $("#offer-list");
  const st = $("#offers-state");
  if (!state.sport) { statusPage(st, "empty", "Pick a sport", "Choose a sport to see all its weekly offers."); return; }
  const days = new Set([...document.querySelectorAll("#days button")]
    .map((b, i) => (b.getAttribute("aria-pressed") === "true" ? i : -1)).filter((i) => i >= 0));
  const [, lo, hi] = TIME_FILTERS[+$("#f-time").value || 0];
  const place = $("#f-place").value, level = $("#f-level").value;
  const sport = state.sport.id === "all" ? $("#f-sport").value : "";
  const q = $("#f-text").value.trim().toLowerCase();
  const frag = document.createDocumentFragment();
  let shown = 0, lastDay = -1, box = null;
  for (const r of rows) {
    const o = r.offer;
    const hour = +o.start.slice(0, 2);
    const ok = (!days.size || days.has(o.weekday)) && lo <= hour && hour < hi &&
      (!place || o.location === place) && (!level || o.niveau === level) &&
      (!sport || o.sport_name === sport) &&
      (!q || `${o.title} ${o.sport_name} ${o.instructor}`.toLowerCase().includes(q));
    if (!ok) continue;
    if (o.weekday !== lastDay) {
      frag.append(el("div", { class: "weekday-header" }, WEEKDAYS[o.weekday]));
      box = el("div", { class: "boxed-list" });
      frag.append(box);
      lastDay = o.weekday;
    }
    box.append(r);
    shown++;
  }
  list.replaceChildren(frag);
  $("#summary").textContent = rows.length ? `${shown} of ${plural(rows.length, "weekly offer")} · ${state.sport.name}` : "";
  if (!rows.length) statusPage(st, "empty", "No offers", "ASVZ has no upcoming lessons for this right now.");
  else if (!shown) statusPage(st, "empty", "No matching offers", "Try fewer filters.");
  else st.replaceChildren();
}

function syncChecks() { for (const r of rows) r.check.checked = state.selected.has(r.offer.key); }

function updateSelection() {
  const n = state.selected.size;
  $("#sel-label").textContent = `${plural(n, "offer")} selected`;
  $("#selection-bar").hidden = n === 0;
}

async function signUp() {
  const keys = [...state.selected];
  if (!keys.length) return;
  const weekly = $("#sel-weekly").checked, watch = $("#sel-watch").checked;
  try {
    const r = await api("signup", { sport: state.sport.id, keys, weekly, watch });
    if (r.pending) { toast(r.message, 7000); state.selected.clear(); updateSelection(); syncChecks(); return; }
    toast(`Signed up for ${plural(r.offers, "offer")} (${weekly ? "every week" : "the next date"}) · ` +
          `${plural(r.queued, "date")} queued`);
    r.messages.forEach((m, i) => setTimeout(() => toast(m, 6000), 600 * (i + 1)));
    state.selected.clear();
    await refreshPlan(true, false);
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

// ---------------------------------------------------------------- My plan

let planSig = "";
async function refreshPlan(quiet = false, render = true) {
  try {
    const p = await api("plan");
    state.plan = p;
    const sig = JSON.stringify(p) + Math.floor(now() / 60000);
    if (render && (sig !== planSig || !quiet)) { planSig = sig; renderPlan(); }
  } catch (e) {
    if (!quiet && e.status !== 401) statusPage($("#plan-body"), "error", "Couldn't load your plan", e.message);
  }
}

function group(title, desc, suffix) {
  const g = el("div", { class: "group" },
    el("div", { class: "group-head" }, el("div", {}, el("h2", {}, title), desc ? el("p", {}, desc) : null), suffix));
  const list = el("div", { class: "boxed-list" });
  g.append(list);
  return [g, list];
}

function iconButton(icon, label, onclick) {
  const b = el("button", { class: "btn flat icon", "aria-label": label, title: label, onclick });
  b.innerHTML = ICONS[icon];
  return b;
}

function renderPlan() {
  const p = state.plan;
  const body = $("#plan-body");
  body.replaceChildren();

  // -- offers
  const ics = el("button", { class: "btn pill suggested", disabled: !p.entries.length,
    onclick: (ev) => busy(ev.currentTarget, async () => {
      try { download(await api("ics", { plan: true }), "asvz-plan.ics", "text/calendar");
            toast("Saved — import it in Google Calendar (Settings → Import & export)", 6000); }
      catch (e) { toast(e.message); }
    }) });
  ics.innerHTML = ICONS.cal;
  ics.append("Calendar file…");
  const sync = iconButton("refresh", "Look for newly published dates now", (ev) => busy(ev.currentTarget, async () => {
    try { const r = await api("plan/sync", {}); toast(r.message); }
    catch (e) { toast(e.message); }
    refreshPlan();
  }));
  let [g, list] = group("Your offers", "Weekly offers get new dates queued automatically as ASVZ publishes them.",
    el("div", { class: "row-suffix" }, sync, ics));
  if (!p.entries.length) list.append(el("div", { class: "row" }, el("div", { class: "row-text" },
    el("div", { class: "row-title" }, "Nothing here yet"),
    el("div", { class: "row-sub" }, "Find a sport in Discover, tick the offers you like and press “Sign me up”."))));
  for (const e of p.entries) {
    const sw = el("input", { type: "checkbox", class: "switch", "aria-label": "Every week" });
    sw.checked = !!e.weekly;
    sw.addEventListener("change", async () => {
      try {
        const r = await api("plan/weekly", { key: e.key, weekly: sw.checked });
        if (r.message) toast(r.message, r.pending ? 7000 : 4000);
      } catch (err) { toast(err.message); sw.checked = !sw.checked; }
      refreshPlan();
    });
    list.append(el("div", { class: "row wrap" },
      el("div", { class: "row-text" },
        el("div", { class: "row-title" }, `${e.sport_name}: ${e.title}`),
        el("div", { class: "row-sub" }, `${WEEKDAYS[e.weekday]}s ${e.start}–${e.end}  ·  ${e.location}  ·  ${e.niveau}` +
          (e.watch ? "  ·  waits for a spot if full" : ""))),
      el("div", { class: "row-suffix" }, el("label", { class: "switch-label dim" }, "Every week", sw),
        iconButton("trash", "Remove from plan", async () => {
          if (!await confirmDialog("Remove from plan?", `${e.sport_name}: ${e.title} (${WEEKDAYS[e.weekday]} ${e.start}).\n` +
            "Queued dates of this offer are removed too.", "Remove")) return;
          try { const r = await api("plan/remove", { key: e.key });
                toast(r.pending ? r.message : `${e.title}: ${r.message}`, r.pending ? 7000 : 4000); }
          catch (err) { toast(err.message); }
          await refreshPlan(); rebuildOffers();
        }))));
  }
  body.append(g);

  // -- changes waiting for a run to finish
  if (p.pending.length) {
    [g, list] = group("Waiting for the bot", "The bot is in the middle of a run; these are applied right after it.");
    for (const label of p.pending) list.append(el("div", { class: "row" }, el("div", { class: "row-title" }, label)));
    body.append(g);
  }

  // -- upcoming sign-ups
  [g, list] = group("Upcoming sign-ups", "The bot enrolls you the moment enrollment opens (usually 24 h before). " +
    `Weekly offers are queued about ${p.queue_days} days ahead.`);
  if (!p.jobs.length) list.append(el("div", { class: "row" }, el("div", { class: "row-title" }, "No sign-ups queued")));
  for (const j of p.jobs) {
    const opens = j.enrollment_start;
    const status = opens && new Date(opens) > now()
      ? `Enrollment opens ${shortDate(opens)} (${countdown(opens)})`
      : "Enrollment is open — the bot is signing you up";
    const suffix = el("div", { class: "row-suffix" });
    if (j.watch) { const i = el("span", { class: "dim", title: "Waits for a free spot if booked out" }); i.innerHTML = ICONS.eye; suffix.append(i); }
    const link = el("a", { class: "btn flat icon", href: j.lesson_url, target: "_blank", rel: "noopener noreferrer",
      "aria-label": "Open lesson page", title: "Open lesson page" });
    link.innerHTML = ICONS.web;
    suffix.append(link, iconButton("skip", "Skip this date", async () => {
      if (!await confirmDialog("Skip this date?", `${j.title || "Lesson " + j.lesson_id}\n${shortDate(j.lesson_start)}`, "Skip")) return;
      try { const r = await api("jobs/remove", { id: j.id }); toast(r.message, r.pending ? 7000 : 4000); } catch (e) { toast(e.message); }
      refreshPlan();
    }));
    list.append(el("div", { class: "row" },
      el("div", { class: "row-text" },
        el("div", { class: "row-title" }, el("b", { class: "tnum" }, shortDate(j.lesson_start)), "   ", j.title || `Lesson ${j.lesson_id}`),
        el("div", { class: "row-sub" }, status + (j.location ? `  ·  ${j.location}` : ""))),
      suffix));
  }
  body.append(g);

  // -- watchers
  if (p.watchers.length) {
    [g, list] = group("Waiting for a free spot", "These lessons were full; the bot keeps checking until a place frees up.");
    for (const w of p.watchers) {
      list.append(el("div", { class: "row" },
        el("div", { class: "row-text" },
          el("div", { class: "row-title" }, w.title || `Lesson ${w.lesson_id}`),
          el("div", { class: "row-sub" }, `Watching since ${shortDate(w.since)}`)),
        el("button", { class: "btn", onclick: (ev) => busy(ev.currentTarget, async () => {
          try { const r = await api("watchers/stop", { id: w.id }); toast(r.message); } catch (e) { toast(e.message); }
          refreshPlan();
        }) }, "Stop")));
    }
    body.append(g);
  }

  // -- add by lesson number (the text menu's "1) Add a lesson")
  [g, list] = group("Add a lesson by number", "Like “1) Add a lesson” in the text menu: the number from schalter.asvz.ch/tn/lessons/…");
  const input = el("input", { class: "entry-inline", inputmode: "numeric", placeholder: "e.g. 757666", "aria-label": "Lesson number" });
  const watch = el("input", { type: "checkbox", class: "switch", "aria-label": "If full, wait for a spot" });
  const add = el("button", { class: "btn suggested", type: "submit" }, "Add");
  const form = el("form", { class: "add-form" }, input, el("label", { class: "switch-label" }, "If full, wait", watch), add);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!input.value.trim()) return;
    busy(add, async () => {
      toast("Reading the lesson page on the server… (up to a minute)");
      try { const r = await api("jobs/add", { lesson_id: input.value.trim(), watch: watch.checked });
            toast(r.message, 6000); input.value = ""; }
      catch (e) { toast(e.message, 8000); }
      refreshPlan();
    });
  });
  list.append(form);
  body.append(g);
}

// ---------------------------------------------------------------- start

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
boot();
