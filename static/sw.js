self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
self.addEventListener('fetch', function(e) {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
