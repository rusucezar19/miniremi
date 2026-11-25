/* Minimal service worker: makes the app installable and caches static assets.
   Pages are always fetched from the network (data must be fresh and per-user). */
const CACHE = "certtracker-static-v1";
const ASSETS = ["/static/app.css?v=1", "/static/app.js?v=1", "/static/icons/icon.svg", "/static/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));
    return;
  }
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() =>
      new Response("<!doctype html><meta name=viewport content='width=device-width'><title>Offline</title>" +
        "<body style='font-family:system-ui;padding:2rem'><h1>Offline</h1><p>The certificate tracker is not reachable right now.</p>",
        { headers: { "Content-Type": "text/html" } })));
  }
});
