// Service worker: lets the page be installed and open instantly.
// Caches only the page itself (network first), never /api/ data —
// status must always come live from the server.
const CACHE = "vb-shell-v3";
const SHELL = ["./", "index.html", "app.css", "app.js", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  ev.respondWith(fetch(ev.request)
    .then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(ev.request, copy)); }
      return r;
    })
    .catch(() => caches.match(ev.request)));
});
