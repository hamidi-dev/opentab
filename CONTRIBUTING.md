# Contributing to OpenTab

Thanks for your interest! OpenTab is a small, dependency-light project — a few
conventions keep it that way.

## Ground rules

- **Standard library only at runtime.** `curses` + `sqlite3` + the stdlib. The *only*
  third-party runtime dependency is `windows-curses` (Windows-only, for the missing stdlib
  `curses`). Don't add another; ruff and hatchling are dev/build tooling and fine.
- **Read-only on harness data.** OpenTab never writes to the sources it reads (the
  OpenCode database, transcripts, auth files, etc.). Its own files and explicit
  exports are listed in [Privacy](docs/privacy.md#everything-it-writes).
- **Python 3.9+.** Don't reach for newer syntax (`target-version = py39`).

See [`docs/architecture.md`](docs/architecture.md) for the architecture, layering rules, and the backend contract before making larger changes.

## Setup

```sh
git clone https://github.com/hamidi-dev/opentab && cd opentab
pip install -e .                 # editable install (provides the `opentab` command)
pip install ruff==0.1.15         # matches CI
git config core.hooksPath hooks  # run the same checks on every push
```

## Tests & checks

The suite lives in `tests/`, one module per module under test (`tests/test_pricing.py`,
`tests/test_stores_codex.py`, `tests/test_tui_app.py`, …), with the shared fakes and the
per-backend builders in `tests/_support.py`. `run_tests.py` is a custom runner (not
pytest); `tests/__init__.py` prepends `src/` to `sys.path`, so it works without an install:

```sh
python3 run_tests.py             # whole suite
python3 run_tests.py pricing     # only modules/tests matching a substring
```

With Node.js on `PATH`, the suite also executes the browser navigation regression
against the shipped JavaScript. Without Node it prints an explicit skip; CI requires it.

Add a test next to its module's other tests; the runner discovers `tests/test_*.py` by
glob, so there is no list to register it in. A local `pytest tests -k NAME` also works.

Test ownership follows the code being exercised, not the feature that prompted it.
A Context test that parses zaly records belongs in `test_stores_zaly.py`; a test of
the curve's rendering belongs in `test_tui_detail.py`. TUI tests are split by screen
because App and Renderer share one stateful surface. Fixtures used by one module
stay there; move them to `_support.py` when a second module needs them.

`tests/__init__.py` isolates all four XDG roots before importing OpenTab, so tests
use the bundled price catalog rather than a developer's refreshed cache. It also
clears ambient multiplexer markers. Keep this setup on every import path, not in
a runner-only fixture. The runner treats import failures and modules contributing
zero tests as errors; neither should silently reduce coverage while reporting success.

The pre-push hook (and CI) run:

```sh
ruff check src/opentab tests run_tests.py
ruff format --check src/opentab tests run_tests.py
python3 -m compileall -q src/opentab
python3 run_tests.py
shellcheck install.sh hooks/pre-push   # when shellcheck is installed
```

Fix formatting with `ruff format src/opentab tests run_tests.py`. Note that `ruff.toml`
deliberately ignores `E501` (long lines): the TUI f-strings build fixed-width columns, so
don't wrap them to satisfy line length.

## Commits

[Conventional Commits](https://www.conventionalcommits.org): `type(scope): subject`. Keeps
the history scannable and feeds the release-notes pass.

- **Types** (only these): `feat` `fix` `perf` `refactor` `docs` `test` `chore`. A breaking
  change appends `!` after the scope (`refactor!: …`) and/or a `BREAKING CHANGE:` footer.
- **Subject:** imperative mood, lowercase first word (`add`, not `adds`/`added`), no
  trailing period, ≤72 chars. Body is optional; wrap ~72 and explain *why*, not *what*.
- **Releases** use `chore(release): vX.Y.Z` (and bump `__version__` in
  `src/opentab/__init__.py`; the version is not derived from a git tag).
- **No AI attribution:** omit generated-by messages and AI co-author trailers.
- **Scope** is optional but preferred: exactly one, lowercase, from the vocabulary below.
  Don't coin a synonym for an existing scope (`tui` not `ui`, `pricing` not `prices`,
  `sources` not `source`); a genuinely new area not yet listed is fine to add.

  | Group | Scopes |
  |-------|--------|
  | Backends (one store each) | `opencode` `claude` `codex` `hermes` `copilot` `vscode` `pi` `openclaw` `csv` `jsonl` `combined` |
  | Core modules | `tui` `web` `pricing` `heatmap` `sources` `state` `cli` `models` `formatting` `util` `demo` `doctor` |
  | UI features (prefer over bare `tui` when one fits) | `trends` `filter` `sort` `range` `export` `launch` `turns` `tools` `graph` |
  | Meta | `release` `deps` `ci` `dev` |

  `graph` is for the charts themselves — a new visualization, or a change to one — and it
  wins over `tui`/`web` even though a chart usually lands in both frontends, because the
  chart is the unit of work and shipping it in one frontend only is the exception.

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
