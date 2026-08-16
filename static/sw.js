/* MyPilot service worker.

   SERVED FROM `/sw.js`, NOT `/static/sw.js`. That is deliberate and it is
   load-bearing: a worker's default scope is the directory it was served
   from, so one living under /static/ could only ever control /static/ and
   would never see a navigation or an API call. The route in main.py reads
   this file and serves it at the root.

   CACHE_NAME carries the app version, injected by that same route. This is
   the single most important line in the file. Every deploy changes the
   version, which changes the cache name, which makes `activate` delete
   every older cache. Without it a service worker will happily serve
   yesterday's CSS forever and the owner's `update.sh` workflow silently
   stops reaching anyone's phone — the failure looks exactly like "the
   server didn't update", which is the worst possible thing to debug.

   Three request classes, three strategies:

     static assets  cache-first   they are immutable within a version
     API reads      network-first keep the last good body for offline
     navigations    network-first never serve a stale personalised page

   The API cache exists ONLY so there is something to show when the network
   is gone. It is never preferred over a live answer, and a served-from-
   cache response is tagged so the page can say so out loud rather than
   quietly presenting old numbers as current.
*/
const VERSION = '__APP_VERSION__';
const CACHE_NAME = 'mypilot-v' + VERSION;
const API_CACHE = 'mypilot-api-v' + VERSION;

const SHELL = [
  '/static/app.css',
  '/static/planes.js',
  '/static/offline.html',
  '/static/vendor/leaflet/leaflet.css',
  '/static/vendor/leaflet/leaflet.js',
  '/static/vendor/Sortable.min.js',
  '/static/icon-192.png',
  '/static/favicon-32x32.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // addAll is all-or-nothing: one 404 aborts the whole install and the
      // worker never activates. Add individually and tolerate misses, so a
      // renamed vendor file degrades one asset instead of disabling offline
      // support entirely.
      Promise.all(SHELL.map((url) =>
        cache.add(url).catch(() => null)
      ))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(
      names.filter((n) => n !== CACHE_NAME && n !== API_CACHE)
           .map((n) => caches.delete(n))
    )).then(() => self.clients.claim())
  );
});

function isApiRead(url) {
  return url.pathname.startsWith('/api/');
}

function isStaticAsset(url) {
  return url.pathname.startsWith('/static/');
}

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Anything that changes state goes straight to the network. Caching a
  // POST would be wrong and replaying one would be dangerous.
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never cache authentication. A cached login or logout response served to
  // the next person on a shared device is a real problem, not a stale-data
  // annoyance.
  if (url.pathname.startsWith('/login') || url.pathname.startsWith('/logout') ||
      url.pathname.startsWith('/register') || url.pathname.startsWith('/setup')) {
    return;
  }

  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  if (isApiRead(url)) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(API_CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() =>
        caches.match(req).then((hit) => {
          if (!hit) {
            return new Response(
              JSON.stringify({ error: 'offline', stale: true }),
              { status: 503, headers: { 'Content-Type': 'application/json' } }
            );
          }
          // Rebuild the response so the page can tell this body came from
          // disk. Headers on a cached Response are immutable, hence the copy.
          return hit.blob().then((body) => new Response(body, {
            status: 200,
            headers: {
              'Content-Type': hit.headers.get('Content-Type') || 'application/json',
              'X-MyPilot-Stale': '1'
            }
          }));
        })
      )
    );
    return;
  }

  // Navigations. Pages are per-user and time-sensitive, so a cached one is
  // never served as if it were current — offline gets an honest placeholder
  // instead, and the live page comes back the moment the network does.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('/static/offline.html').then(
        (hit) => hit || new Response('Offline', {
          status: 503, headers: { 'Content-Type': 'text/plain' }
        })
      ))
    );
  }
});
