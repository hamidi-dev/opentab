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

`test_opentab.py` is a custom runner (not pytest) — it runs every `test_*` function in
order and prepends `src/` to `sys.path`, so it works without an install:

```sh
python3 test_opentab.py          # whole suite
```

To run a single test, import and call it (`python3 -c "import test_opentab as t; t.test_NAME()"`),
or use a local `pytest test_opentab.py -k NAME`.

The pre-push hook (and CI) run:

```sh
ruff check src/opentab test_opentab.py
ruff format --check src/opentab test_opentab.py
python3 -m compileall -q src/opentab
python3 test_opentab.py
shellcheck install.sh hooks/pre-push   # when shellcheck is installed
```

Fix formatting with `ruff format src/opentab test_opentab.py`. Note that `ruff.toml`
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
