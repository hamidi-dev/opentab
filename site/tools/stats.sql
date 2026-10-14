-- Visit counts written by functions/hit.js (D1 database opentab-stats, EU jurisdiction).
-- Daily totals only: no IP, user agent, cookie or other identifier is stored.
-- Apply once: npx wrangler@4.143.0 d1 execute opentab-stats --remote --file tools/stats.sql
CREATE TABLE IF NOT EXISTS hits (
  day     TEXT NOT NULL,    -- UTC date, YYYY-MM-DD
  site    TEXT NOT NULL,    -- "live" (opentab.hamidi.dev) or "test" (preview deployments)
  page    TEXT NOT NULL,    -- home, docs, try, 404
  event   TEXT NOT NULL,    -- visit (first page of a visit), view (later pages), start (the app)
  country TEXT NOT NULL,    -- ISO code Cloudflare derived from the IP; XX if unknown
  ref     TEXT NOT NULL,    -- referring site's host on a visit, else ""
  n       INTEGER NOT NULL,
  PRIMARY KEY (day, site, page, event, country, ref)
) WITHOUT ROWID;
