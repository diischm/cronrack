// Test harness for the service worker (used by tests/test_pwa.py, needs Node 18+).
//
// Reads the sw.js the server sends on stdin, runs it in a fresh sandbox per scenario with a
// fake `caches`, a fake network (`fetch`), instant timers and a fake `self`, fires install /
// activate / fetch events at it, and prints one JSON object describing what it did.
// No real network, no files.

import { readFileSync } from "node:fs";
import vm from "node:vm";

const ORIGIN = "https://cronrack.test";
const SOURCE = readFileSync(0, "utf8");

function keyOf(request) {
  const url = new URL(typeof request === "string" ? request : request.url, ORIGIN);
  return url.pathname + url.search;
}

function tagged(src, init = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("x-src", src);
  return new Response(init.body === undefined ? src : init.body, {
    status: init.status ?? 200,
    headers,
  });
}

// The server during install: every precached URL answers 200, unless a scenario says the
// offline page redirects (fetch with redirect: "error" then rejects, as browsers do) or fails.
function installServer(offlineBehaviour) {
  return (request, init = {}) => {
    const key = keyOf(request);
    if (key === "/offline/" && offlineBehaviour === "redirect") {
      if (init.redirect === "error") return Promise.reject(new TypeError("redirect"));
      return Promise.resolve(tagged("redirected-page"));
    }
    if (key === "/offline/" && offlineBehaviour === "error500") {
      return Promise.resolve(tagged("error-page", { status: 500 }));
    }
    return Promise.resolve(tagged(key === "/offline/" ? "offline" : "cache"));
  };
}

// One sandbox = one fresh browser profile with this service worker loaded.
function boot(network, { offlineBehaviour = "ok" } = {}) {
  const state = { skipWaiting: false, claimed: false, installed: false, writesAfterInstall: 0 };
  const stores = new Map();
  const handlers = {};
  const server = installServer(offlineBehaviour);

  function openStore(name) {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    const write = () => {
      if (state.installed) state.writesAfterInstall += 1;
    };
    return {
      async addAll(urls) {
        write();
        for (const url of urls) store.set(keyOf(url), "cache");
      },
      async add(request) {
        write();
        store.set(keyOf(request), "cache");
      },
      async put(request, response) {
        write();
        store.set(keyOf(request), response.headers.get("x-src"));
      },
      async match(request) {
        const src = store.get(keyOf(request));
        return src ? tagged(src) : undefined;
      },
    };
  }

  const sandbox = {
    console,
    URL,
    Response,
    Headers,
    Error,
    // Instant timers: a "timeout" fires right after everything already settled.
    setTimeout: (fn) => setTimeout(fn, 0),
    clearTimeout,
    fetch: (request, init) => (state.installed ? network(request, init) : server(request, init)),
    caches: {
      open: async (name) => openStore(name),
      keys: async () => [...stores.keys()],
      delete: async (name) => stores.delete(name),
      match: async (request) => {
        for (const name of stores.keys()) {
          const hit = await openStore(name).match(request);
          if (hit) return hit;
        }
        return undefined;
      },
    },
    self: {
      location: { origin: ORIGIN, href: `${ORIGIN}/sw.js` },
      addEventListener: (type, fn) => {
        handlers[type] = fn;
      },
      skipWaiting: async () => {
        state.skipWaiting = true;
      },
      clients: {
        claim: async () => {
          state.claimed = true;
        },
      },
    },
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(SOURCE, context, { filename: "sw.js" });
  const constants = vm.runInContext("({ CACHE, PRECACHE })", context);

  async function fire(type, extra = {}) {
    let waited;
    let responded;
    let handled = false;
    const event = {
      ...extra,
      waitUntil(promise) {
        waited = promise;
      },
      respondWith(promise) {
        handled = true;
        responded = promise;
      },
    };
    handlers[type]?.(event);
    let failed = false;
    if (waited) {
      try {
        await waited;
      } catch {
        failed = true;
      }
    }
    return { handled, failed, response: handled ? await responded : undefined };
  }

  async function install() {
    const result = await fire("install");
    state.installed = true;
    return result;
  }

  return { state, stores, constants, fire, install, openStore };
}

async function classify(result) {
  if (!result.handled) return "not-handled";
  if (!result.response || result.response.type === "error") return "error";
  return result.response.headers.get("x-src");
}

const ok = () => Promise.resolve(tagged("network"));
const status = (code) => () => Promise.resolve(tagged("network", { status: code, body: null }));
const down = () => Promise.reject(new TypeError("Failed to fetch"));
const hang = () => new Promise(() => {});

function nav(path, method = "GET", origin = ORIGIN) {
  return { url: origin + path, method, mode: "navigate" };
}
function get(path, origin = ORIGIN) {
  return { url: origin + path, method: "GET", mode: "cors" };
}

const out = { install: {}, activate: {}, fetch: {} };

// Install: what goes into the cache, and skipWaiting.
{
  const sw = boot(ok);
  const result = await sw.install();
  out.install.failed = result.failed;
  out.install.added = [...sw.stores.get(sw.constants.CACHE).keys()];
  out.install.offlineStored = (await sw.openStore(sw.constants.CACHE).match("/offline/"))
    ?.headers.get("x-src");
  out.install.skipWaiting = sw.state.skipWaiting;
}

// Install must fail (and store nothing usable) when /offline/ redirects or errors.
for (const behaviour of ["redirect", "error500"]) {
  const sw = boot(ok, { offlineBehaviour: behaviour });
  const result = await sw.install();
  const stored = await sw.openStore(sw.constants.CACHE).match("/offline/");
  out.install[behaviour] = {
    failed: result.failed,
    skipWaiting: sw.state.skipWaiting,
    offlineStored: stored ? stored.headers.get("x-src") : null,
  };
}

// Activate: an older CronRack cache and another app's cache exist.
{
  const sw = boot(ok);
  sw.openStore("cronrack-shell-v1-00000000");
  sw.openStore("other-app-cache");
  await sw.install();
  const before = new Set(sw.stores.keys());
  await sw.fire("activate");
  out.activate.deleted = [...before].filter((name) => !sw.stores.has(name));
  out.activate.claimed = sw.state.claimed;
}

const precachedCss = boot(ok).constants.PRECACHE.find((url) => url.endsWith(".css"));

// [case, network, request, precache installed?]
const cases = [
  ["navigate_ok", ok, nav("/"), true],
  ["navigate_network_error", down, nav("/hosts/1/"), true],
  ["navigate_hang", hang, nav("/"), true],
  ["navigate_502", status(502), nav("/"), true],
  ["navigate_503", status(503), nav("/"), true],
  ["navigate_504", status(504), nav("/"), true],
  ["navigate_500", status(500), nav("/"), true],
  ["navigate_404", status(404), nav("/hosts/999/"), true],
  ["navigate_redirect", status(302), nav("/"), true],
  ["navigate_network_error_offline_not_cached", down, nav("/"), false],
  ["navigate_502_offline_not_cached", status(502), nav("/"), false],
  ["post_navigate", down, nav("/login/", "POST"), true],
  ["cross_origin_navigate", down, nav("/", "GET", "https://elsewhere.test"), true],
  ["api_get", down, get("/api/v1/overview.json"), true],
  ["app_css_get", down, get("/static/cronrack/css/components.css"), true],
  ["cross_origin_get", down, get(precachedCss, "https://elsewhere.test"), true],
  ["precached_css_get", down, get(precachedCss), true],
  ["precached_css_with_query", down, get(`${precachedCss}?v=2`), true],
];

let writesAfterInstall = 0;
for (const [name, network, request, installed] of cases) {
  const sw = boot(network);
  if (installed) await sw.install();
  else sw.state.installed = true;
  out.fetch[name] = await classify(await sw.fire("fetch", { request }));
  writesAfterInstall += sw.state.writesAfterInstall;
}
out.writesAfterInstall = writesAfterInstall;

process.stdout.write(JSON.stringify(out));
// The "hang" scenario leaves a promise that never settles; nothing else keeps Node alive.
process.exit(0);
