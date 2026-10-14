// POST /hit: one anonymous count from count.js. Only daily totals are kept: the page, the
// event, the country Cloudflare derived from the IP and, on the first page of a visit, the
// referring site's host. No IP, user agent, cookie or other identifier is stored.
// This is the site's only Function; Pages routes nothing else here, so every page stays static.
const PAGES = new Set(["home", "docs", "try", "404"]);
const EVENTS = new Set(["visit", "view", "start"]);
const HOST = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/;

export async function onRequestPost({ request, env }) {
  const text = await request.text();
  let body;
  try {
    body = text.length <= 512 ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!body || !PAGES.has(body.page) || !EVENTS.has(body.event)) {
    return new Response(null, { status: 400 });
  }
  const ref = body.event === "visit" && typeof body.ref === "string" ? body.ref.toLowerCase().replace(/^www\./, "") : "";
  const country = /^[A-Z]{2}$/.test(request.cf?.country || "") ? request.cf.country : "XX";
  const site = new URL(request.url).hostname === "opentab.hamidi.dev" ? "live" : "test";
  await env.STATS.prepare(
    "INSERT INTO hits VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT DO UPDATE SET n = n + 1",
  )
    .bind(new Date().toISOString().slice(0, 10), site, body.page, body.event, country,
          ref.length <= 80 && HOST.test(ref) ? ref : "")
    .run();
  return new Response(null, { status: 204 });
}
