/*
 * Cyber Controller — PWA service worker (MB cluster: installable LAN wireless remote).
 *
 * SECURITY INVARIANT (the hard gate for this cluster): this worker MUST NEVER cache authenticated data
 * or serial output. It caches ONLY the static, non-sensitive app shell listed in SHELL_ASSETS. Every
 * /api/ response, every /socket.io/ frame, every non-GET request, and every cross-origin request is
 * network-only and never touches the cache. `caches` is written in EXACTLY ONE place (the fetch handler,
 * inside the branch guarded by isShellAsset), so there is no path by which a device list, a flash log, or
 * a serial line can be persisted to disk. Do NOT add a dynamic or authenticated path to SHELL_ASSETS.
 */

const CACHE_NAME = 'cc-shell-v1';

// Static, non-sensitive shell assets ONLY. No '/', no authenticated HTML page, no '/api/*', no
// '/socket.io/*'. The socket.io library loads cross-origin (a CDN) and is intentionally excluded — the
// worker never caches cross-origin responses.
const SHELL_ASSETS = [
  '/static/style.css',
  '/manifest.webmanifest',
  '/static/icons/ace-192.png',
  '/static/icons/ace-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      // Best-effort per asset: an icon the owner has not dropped yet must not fail the whole install.
      .then((cache) => Promise.allSettled(SHELL_ASSETS.map((asset) => cache.add(asset))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// The ONLY gate to the cache. A request may be served/stored from cache only if it is a same-origin GET
// for a path explicitly enumerated in SHELL_ASSETS — never an API call, a socket frame, or cross-origin.
function isShellAsset(request) {
  if (request.method !== 'GET') return false;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return false;     // never cache cross-origin (e.g. the CDN)
  if (url.pathname.startsWith('/api/')) return false;        // never cache authenticated API data
  if (url.pathname.startsWith('/socket.io/')) return false;  // never cache the live serial/event stream
  return SHELL_ASSETS.includes(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  // Anything that is NOT an allowlisted static shell asset falls through to the browser's default
  // network fetch with no respondWith and no cache write — this is what keeps authenticated /api/ data
  // and serial output entirely off disk.
  if (!isShellAsset(request)) {
    return;
  }
  // Cache-first for the shell (offline app frame), revalidating from network on a miss.
  event.respondWith(
    caches.match(request).then((cached) =>
      cached ||
      fetch(request).then((response) => {
        if (response && response.ok && response.type === 'basic') {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      // Offline + uncached shell asset: fall back to the cached copy, or a proper network-error
      // Response (never resolve respondWith with undefined, which surfaces as a TypeError).
      }).catch(() => cached || Response.error())
    )
  );
});
