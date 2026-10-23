# OpenTab website

The static site behind [opentab.hamidi.dev](https://opentab.hamidi.dev): the landing
page (`index.html`), a docs browser over `docs/` and the README (`docs/`), and
OpenTab's real TUI running in the browser under Pyodide (`try/`).

```sh
site/tools/dist.sh --build    # regenerate everything and assemble site/dist/
python3 -m http.server --directory site/dist
```

The plain server is enough for the landing page and docs; `/try/`
and the app in the hero also need the cross-origin isolation headers from `_headers`.

| Script | Does (generated files are gitignored) |
|---|---|
| `tools/build.sh` | The demo report from the synthetic showcase, `docs/changelog.json` from GitHub Releases, the hero clip and `media/hero-story.json` |
| `tools/build-try.sh` | The in-browser app and its showcase history, with transcripts loaded lazily |
| `tools/vendor.sh` | Pyodide, xterm.js, the fonts and marked, pinned by version and SHA-256 |
| `tools/dist.sh` | `dist/`: the publishable tree, with privacy checks applied |
| `tools/publish.sh` | Uploads `dist/` to Cloudflare Pages; `--preview` uses a preview URL |
| `tools/visitors.sh` | Visit counts by day, country, referring site and page |

`try/py/` adapts OpenTab to the browser without changing it: a pure-Python `curses`
over xterm.js, with threads emulated while it waits for input.

## Privacy

No page contacts a third party. Fonts and libraries are self-hosted, every CSP in
`_headers` is same-origin, and the docs browser turns external images into links.

Visits are counted without cookies or identifiers. `count.js` sends one beacon per
page view to the site's own `/hit`, skipped in frames, automated browsers and under
Global Privacy Control or Do Not Track. `functions/hit.js` adds it to daily totals in
a D1 database in the EU (schema in `tools/stats.sql`): page, event, the country
Cloudflare derives from the IP and, for a visit's first page, the referring site's
host. No IP, user agent or per-visitor row is stored.

`impressum/` and `datenschutz/` (the privacy notice) are German with English below.
The privacy notice describes the hosting, the counter, every `localStorage` key and
the in-browser app, so change it along with any of them.
