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
| `tools/build-try.sh` | `try/app.tar.gz` (OpenTab plus the browser shim) and `try/data/home.tar.gz` (the showcase history, cache prewarmed); cache-covered transcripts go to `try/data/lazy/` and load when a session is opened |
| `tools/vendor.sh` | `try/vendor/`: Pyodide and xterm.js, pinned by version and SHA-256 |
| `tools/dist.sh` | `dist/`: the publishable tree, symlinks dereferenced, privacy checks applied |
| `tools/publish.sh` | Uploads `dist/` to Cloudflare Pages (opentab.hamidi.dev); `--build` regenerates first, `--preview` uses a preview URL |

The builds read the showcase generator and media renders under `dev/`, which is not
in the repository. `try/py/` adapts OpenTab to the browser without changing it: a
pure-Python `curses` over xterm.js and threads emulated during idle input waits.
