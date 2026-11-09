// AI Amalgam's helper for the phone: makes it installable, shows notifications, and shows a
// "start the Terminal" page instead of Chrome's error when AI Amalgam isn't running.
// It never stores your chats; every request goes straight to the app.
const OFFLINE = "amalgam-offline-v3";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(OFFLINE).then(c => c.addAll(["/offline.html"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== OFFLINE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;            // everything else: straight to the app
  e.respondWith(fetch(e.request).catch(() => caches.match("/offline.html")));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) if ("focus" in c) return c.focus();
    return self.clients.openWindow("/");
  }));
});
