# OpenTab website

A static site: the landing page (`index.html`), a docs browser over `docs/` and the
README (`docs/`), and OpenTab's real TUI running in the browser under Pyodide (`try/`).

```sh
site/tools/dist.sh --build    # regenerate everything and assemble site/dist/
python3 -m http.server --directory site/dist
```

The plain server is enough for the landing page and docs. `/try/` and the app in the
landing page's hero also need the cross-origin isolation headers from `_headers`
(`Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Embedder-Policy:
require-corp`); without them the page explains that it cannot start.

| Script | Does (generated files are gitignored) |
|---|---|
| `tools/build.sh` | `report.html` plus `report-details/` (one file per session, fetched on open) from the synthetic showcase, `docs/changelog.json` from GitHub Releases (needs `gh`), the hero clip |
| `tools/build-try.sh` | `try/app.tar.gz` (OpenTab plus the browser shim) and `try/data/home.tar.gz` (the showcase history, cache prewarmed); cache-covered transcripts and the OpenCode database (in 256 KB chunks) go to `try/data/lazy/` and load when a session is opened. Pyodide's stdlib is split into `try/stdlib/core.zip` (the modules in `try/stdlib-core.txt`) and `rest.zip`, fetched only if a left-out module is imported |
| `tools/vendor.sh` | `try/vendor/` (Pyodide, xterm.js) and `vendor/` (the fonts, marked), pinned by version and SHA-256 |
| `tools/dist.sh` | `dist/`: the publishable tree, symlinks dereferenced, privacy checks applied |
| `tools/publish.sh` | Uploads `dist/` to Cloudflare Pages (opentab.hamidi.dev) with the Function in `functions/`; `--build` regenerates first, `--preview` uses a preview URL |
| `tools/visitors.sh` | Visits, page views and app starts by day, country, referring site and page (`7` for a week; `30 test` for previews) |

The landing hero uses `media/hero-story.json`, a small checked-in snapshot of one
synthetic session. `tools/build-story.py` extracts its daily totals, agents, prompt,
tool names and context curve from the report; `build.sh` refreshes it with the
report. `hero-story.mjs` renders the three steps without fetching the full report.

Below the live demo, `landing.mjs` renders token economics from the same snapshot's
`economics` object: `tokens`, `cost`, `basis: "API list-price estimate"`, and `types`
with `key`, `label`, `tokens` and `cost`. Bucket keys are `cache-read`, `output`,
`cache-write`, `input` and `reasoning`; bucket amounts must sum to the totals.
This is a full API-list-rate repricing of the same synthetic token buckets, not
the report's recorded-plus-estimated spend. Missing or invalid data shows a
pricing-guide fallback, never hardcoded percentages. No report fetch is needed.

`landing.mjs` also handles the Context / Tool calls / Changes tabs: click, arrow
keys, Home and End select a view, with inactive panels hidden. Screenshots keep
their full aspect ratio. The supported-tool list is stationary; workflow tiles
explain exports, explicit remote reads and the limits of selective scrambling.
The real terminal/browser embed, start action and hero deep links stay in
`index.html`; a terminal already started stays mounted when switching modes. The
browser tab's report loads on first view, and the app mounts hidden when the start
button is hovered or focused, so its download has a head start.

No page contacts a third party: fonts (`fonts.css`) and libraries are self-hosted,
and the docs browser loads this repo's screenshots locally and turns any other
image into a link. Every CSP in `_headers` is same-origin. `404.html` uses
root-absolute links because Pages serves it at the missing path.

Visits are counted without cookies or identifiers. `count.js` sends one beacon per
page view to the site's own `/hit` (skipped in frames and automated browsers);
`functions/hit.js`, the only Pages Function, adds it to daily totals in the D1
database `opentab-stats` (EU jurisdiction, schema in `tools/stats.sql`, binding in
`wrangler.toml`): page, event, the country Cloudflare derives from the IP and, for
a visit's first page, the referring site's host. A visit is a page reached from
outside the site; `Referrer-Policy: same-origin` keeps the referrer on internal
links and sends none off-site. No IP, user agent or per-visitor row is stored.

The builds read the showcase generator and media renders under `dev/`, which is not
in the repository. `try/py/` adapts OpenTab to the browser without changing it: a
pure-Python `curses` over xterm.js and threads emulated during idle input waits.
