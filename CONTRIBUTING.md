# Contributing to OpenTab

Thanks for your interest! OpenTab is a small, dependency-light project — a few
conventions keep it that way.

## Ground rules

- **Standard library only at runtime.** `curses` + `sqlite3` + the stdlib. The *only*
  third-party runtime dependency is `windows-curses` (Windows-only, for the missing stdlib
  `curses`). Don't add another; ruff and hatchling are dev/build tooling and fine.
- **Read-only on user data.** OpenTab never writes to the sources it reads (the OpenCode
  database, transcripts, …). The only files it writes are its own prefs/price cache under
  `~/.config/opentab/` and the `opentab-*.csv` you ask for with `e`.
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

Add a test next to its module's other tests; the runner discovers `tests/test_*.py` by
glob, so there is no list to register it in. A local `pytest tests -k NAME` also works.

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
  `src/opentab/__init__.py`).
- **Scope** is optional but preferred: exactly one, lowercase, from the vocabulary below.
  Don't coin a synonym for an existing scope (`tui` not `ui`, `pricing` not `prices`,
  `sources` not `source`); a genuinely new area not yet listed is fine to add.

  | Group | Scopes |
  |-------|--------|
  | Backends (one store each) | `opencode` `claude` `codex` `hermes` `copilot` `vscode` `pi` `openclaw` `csv` `jsonl` `combined` |
  | Core modules | `tui` `web` `pricing` `heatmap` `sources` `state` `cli` `models` `formatting` `util` `demo` |
  | UI features (prefer over bare `tui` when one fits) | `trends` `filter` `sort` `range` `export` `launch` `turns` `tools` |
  | Meta | `release` `deps` `ci` `dev` |

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
