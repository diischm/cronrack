/* CronRack service worker. Served by pwa/views.py at /sw.js, which fills in CACHE and
   PRECACHE. Plain JS, no libraries, no build step.

   What it does, and nothing more:
   - Install: store the offline page and the few static files it needs (PRECACHE).
   - Page loads (same-origin GET navigations): always ask the network first. If the network
     fails (offline, VPN down) or the proxy answers 502/503/504 (CronRack itself is down),
     show the stored offline page instead. Every other answer (200, redirects, 4xx, 500)
     passes through untouched.
   - The offline page's own CSS, JS, font and icons: served from the cache, so the page
     works with no network at all. These are content-hashed files that never change.

   What it never does: store a page, an API answer or anything that needs a login (a
   monitor must never show old status as if it were live); touch other origins or non-GET
   requests; touch WebSockets (service workers can't see them).

   PRECACHE holds paths (/offline/, /static/...): STATIC_URL is a path, never another origin
   (tests/test_pwa.py checks), so url.pathname below matches them. */
"use strict";

const CACHE = __CRONRACK_CACHE__;
const PRECACHE = __CRONRACK_PRECACHE__;
const OFFLINE_URL = "/offline/";
const SERVER_DOWN = new Set([502, 503, 504]);
// A VPN that is down often makes requests hang instead of fail. After this long without an
// answer the offline page is shown (the request itself carries on; a late answer is dropped).
const NAVIGATION_TIMEOUT_MS = 10000;

// Store one precached file. Only a direct 200: a redirect or an error page must never be
// kept in place of the offline page or its files.
async function precache(cache, url) {
  const response = await fetch(url, { redirect: "error", cache: "no-cache" });
  if (!response.ok || response.redirected) throw new Error(`precache failed: ${url}`);
  await cache.put(url, response);
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.all(PRECACHE.map((url) => precache(cache, url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => name.startsWith("cronrack-shell-") && name !== CACHE)
          .map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
  } else if (url.search === "" && PRECACHE.includes(url.pathname)) {
    event.respondWith(cacheFirst(request));
  }
  // Anything else: no respondWith, so the browser handles it as if there were no worker.
});

async function offlinePage() {
  const cache = await caches.open(CACHE);
  return cache.match(OFFLINE_URL);
}

function timeout(ms) {
  return new Promise((_, reject) => {
    setTimeout(() => reject(new Error("navigation timed out")), ms);
  });
}

async function networkFirst(request) {
  let response;
  try {
    response = await Promise.race([fetch(request), timeout(NAVIGATION_TIMEOUT_MS)]);
  } catch {
    return (await offlinePage()) || Response.error();
  }
  if (SERVER_DOWN.has(response.status)) {
    return (await offlinePage()) || response;
  }
  return response;
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  return (await cache.match(request)) || fetch(request);
}
