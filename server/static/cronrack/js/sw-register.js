// Registers the service worker (served by pwa/views.py at /sw.js) for the whole site.
// It only shows an offline page when the server can't be reached; it caches no pages or data.
// After the page has loaded, so it never competes with the first paint. Browsers only allow
// service workers over HTTPS (or on localhost): on plain http:// this does nothing, and the
// failed registration is expected, not an error worth showing.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  });
}
