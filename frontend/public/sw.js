/* Noctalix Nowcast service worker: Web Push + offline app shell */
const CACHE = "nowcast-shell-v1";
const SHELL = ["/", "/manifest.json", "/icons/icon-192.png", "/icons/storm-alert.png", "/icons/badge.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Network-first for navigations and same-origin static files; radar data/API are never cached here.
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && (e.request.mode === "navigate" || url.pathname.startsWith("/_next/static/"))) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/"))),
  );
});

self.addEventListener("push", (e) => {
  let p = {};
  try { p = e.data ? e.data.json() : {}; } catch { p = { title: "Noctalix Nowcast", body: e.data && e.data.text() }; }
  const title = p.title || "Allerta meteo";
  e.waitUntil(
    self.registration.showNotification(title, {
      body: p.body || "",
      icon: p.icon || "/icons/storm-alert.png",
      badge: p.badge || "/icons/badge.png",
      tag: p.tag || "nowcast",
      renotify: !!p.renotify,
      requireInteraction: !!p.requireInteraction,
      vibrate: [200, 100, 200, 100, 400],
      data: p.data || { url: "/" },
    }),
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if (new URL(c.url).origin === new URL(target, self.location.origin).origin && "focus" in c) {
          c.navigate(target);
          return c.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});

// Subscription rotated by the browser: swap endpoint/keys on the backend (keeps location & prefs).
const API = new URL(self.location.href).searchParams.get("api") || "https://api-nowcast.noctalix.com";
self.addEventListener("pushsubscriptionchange", (e) => {
  e.waitUntil(
    (async () => {
      const old = e.oldSubscription;
      const sub = e.newSubscription ||
        (old && old.options ? await self.registration.pushManager.subscribe(old.options) : null);
      if (!old || !sub) return;
      await fetch(`${API}/api/v1/resubscribe`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_endpoint: old.endpoint, subscription: sub.toJSON() }),
      });
    })(),
  );
});
