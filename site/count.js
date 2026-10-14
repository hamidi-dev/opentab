// Anonymous visit counting: one request to the site's own /hit per page view (functions/hit.js).
// No cookie, no ID, nothing stored in the browser, no third party. Framed pages (the app and
// the report inside the landing page) don't count; the landing page reports starting the app.
(() => {
  const script = document.currentScript;
  const hit = new URL("hit", script.src).href;
  const page = script.dataset.page;
  self.otCount = event => {
    if (navigator.webdriver) return;
    let ref = "";
    try { ref = new URL(document.referrer).host; } catch {}
    // Referrer-Policy: same-origin, so a link inside the site carries its referrer and an
    // empty or foreign one means the visit starts here.
    const internal = ref === location.host;
    if (!event) event = internal ? "view" : "visit";
    const body = JSON.stringify({ page, event, ref: event === "visit" ? ref : "" });
    try { navigator.sendBeacon(hit, body); } catch {}
  };
  if (self === top) self.otCount();
})();
