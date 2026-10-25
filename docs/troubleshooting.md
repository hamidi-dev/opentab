# Troubleshooting

Start with `opentab doctor`. It checks this OpenTab install, every harness, your
terminal and OpenTab's own files, and tells you what to fix.

```sh
opentab doctor          # paths shortened, safe to paste
opentab doctor --full   # full paths and machine names, for local use
```

- It only reports; it never changes or creates anything (unless you add `--debug`).
- It shows no prompts or session content. Home paths become `~` and pulled machines
  are counted, not named. Still review it before posting publicly.
- Fixes come in your shell's syntax (`export FOO=bar`, `set -gx FOO bar` or
  `$env:FOO = "bar"`), ready to run.
- It exits with `1` only when something is broken; a `WARN` doesn't change that.
- It also reports the version, install method, Python, SQLite, and whether the
  `opentab` on your `PATH` is the one that answered.

## Slow startup, reloads or session opening

**Cause**: usually a cache miss or a large source. A debug log shows which.

**Fix**: run with a debug log, then follow it from another terminal with `tail -f`:

```sh
opentab --debug --timings   # startup and cache decisions
opentab --debug             # also navigation and reload (r) in the TUI
opentab web --debug         # also live browser session reads
```

OpenTab prints the log path at startup; `--debug-log FILE` picks one. The log shows
cache hits and misses with reasons, phase durations and peak memory. A phase that
starts but never ends is the one still running. See
[Debugging a slow run](caching.md#debugging-a-slow-run).

## Every theme looks the same

**Cause**: pressing `C` cycles themes but nothing changes. OpenTab sets exact colors by
redefining palette slots, and some terminals accept that but ignore it. Other apps
recoloring fine is no evidence; they use a different method.

**Fix**: tell OpenTab to use the nearest standard colors instead. Set it once in that
terminal's profile:

```sh
export OPENTAB_NO_INIT_COLOR=1   # nearest 256-color match, hues kept
export OPENTAB_NO_INIT_COLOR=0   # force exact colors back on
```

Known cases such as [herdr](https://herdr.dev) are detected automatically. A
multiplexer is the usual suspect when colors or glyphs look wrong; `opentab doctor`
names the one it sees and warns when several are nested.

## Boxes and frames render as garbage

**Cause**: your locale claims UTF-8 but the terminal isn't using it. OpenTab already
falls back to plain ASCII frames when the locale can't encode its box characters.

**Fix**: check `LANG` and `LC_ALL`. `opentab doctor` shows what it detected.

## A tool I use isn't showing up

**Cause**: one of several cases that look the same from outside: the harness isn't
installed, has nothing recorded, needs an opt-in export (GitHub Copilot), or a
`--*-dir` flag points one level too deep. VS Code chats may exist but have no tokens.
A remembered `H` choice can also pin you to one harness without saying so.

A first run with no history at all exits with "No supported AI usage history was
found" and the locations it checked.

**Fix**: run `opentab doctor`; it names the case. Press `H` to switch harness. Paths,
flags and setup per harness: [Data harnesses](sources.md).

## Sessions show tokens but `$0.00`

**Cause**: not a bug. The usage was recorded without a price, which is normal on
subscription and credit plans.

**Fix**: press `$` to price those tokens at API list rates, and `P` to see the rates.
See [Pricing](pricing.md).

## `opentab pull` can't reach a machine

**Cause**: OpenTab runs SSH in batch mode, so a login that asks for a password fails.
Over HTTP(S), the URL must return `opentab export` JSON, not the `opentab web` page.

**Fix**: test the same SSH login by hand first. Give a port inline, or use a `Host`
block in `~/.ssh/config` (port, key, `ProxyJump` all apply) and pull by its alias:

```sh
opentab pull box=ssh://user@host:2222
```

If `opentab` isn't on the remote's non-interactive `PATH`, set that machine's `cmd` in
`remotes.json`. `cmd` runs on the far side, so it can't carry local `ssh` flags. See
[Multiple machines](machines.md).

## Moving a machine's spend across by hand

**Cause**: SSH from here isn't possible.

**Fix**: export on that machine, copy the file over, and open it here:

```sh
opentab export --label workstation box.json  # there
opentab remote box.json                      # here: merged with local history
```

`opentab remote` also takes several files or a folder. To keep a summary, put it in
`~/.cache/opentab/remotes/` (or `$XDG_CACHE_HOME/opentab/remotes`; create the folder
first). A bare `opentab remote` then finds it.

- **The machine's name comes from the export, not the file name.** Two boxes with the
  same hostname merge into one; re-export with `--label NAME`. OpenTab warns at startup.
- **A summary that won't parse is skipped**, and OpenTab names the file. Copy it again.

## Notes cannot be saved

**Cause**: `notes.json` is unreadable or malformed. OpenTab refuses to replace your
notes with an empty file. Notes are also off under `--demo` and `--no-state`.

**Fix**: back up the file, then check its permissions and that it's a JSON object with
a `notes` object inside. `opentab doctor` runs the same check without touching it.
Deleting the cache can't recover notes. See
[Notes are authored data](privacy.md#notes-are-authored-data).

## Browser session tabs are missing

**Cause**: `opentab web --html FILE` leaves out Turns, Tools and Context by default.
Older fleet summaries may hold only totals.

**Fix**: add `--include-details`, or use the live `opentab web` (or `--headless`).
Re-export on the source machine if its records still exist. Turn traces and notes are
TUI and CLI/MCP only, never in the browser. See
[the live browser](web.md#served-live) and [remote turns](machines.md#read-a-remote-turn).

## OpenCode Changes say SQLite cannot run the query

**Cause**: SQLite 3.54.1 rejects queries that reference a CTE after opening 1000
cursors. The Changes tab's scoped reads on current OpenCode databases cross that cap.
SQLite reverted it in later releases; cost, tokens and turns are unaffected.

**Fix**: update SQLite, or run OpenTab with a Python that links another version.
`opentab doctor` shows which SQLite your Python uses. Homebrew Python links Homebrew's
SQLite, so `brew upgrade sqlite` fixes it once a newer release is out.
