# Troubleshooting

Start with **`opentab doctor`** — one block covering this copy of OpenTab (version, how it
was installed, and whether the `opentab` on your `PATH` is even the one that answered),
every harness backend (found, or not found and why, with the fix), the terminal's colour
and glyph capabilities including any multiplexer in the way, the price catalog, and
OpenTab's own files.

It **reports and never repairs**: nothing is created, migrated, warmed or fetched.
Availability checks can inspect record contents, such as VS Code's token-usage
markers, but the report does not display prompts or session content. Home paths
fold to `~` and pulled machines are counted rather than named. Review paths and
configuration hints before posting publicly; `opentab doctor --full` reveals full
paths and machine names for local investigation, not a transcript dump. The exit
code is `1` only when something is genuinely broken (a `WARN` never moves it).

Anything it tells you to set is written in your own shell's syntax — `export FOO=bar`,
`set -gx FOO bar`, or `$env:FOO = "bar"` — so the hint is a line you can run.

## Every theme looks the same

The tell: `C` cycles through the themes and **nothing on screen changes**. (Other apps
re-colouring fine is not evidence — they use truecolor escapes, which take a different
path.)

OpenTab hits its themes' exact colours by redefining palette slots (`init_color`). Most
terminals honour that; a few accept the call and quietly ignore it, and there is nothing
readable back that distinguishes the two — a terminal can advertise the capability, accept
every write, and discard it.

Known hosts are detected and switched over automatically, so there is nothing to set:
currently [herdr](https://herdr.dev), which re-emits each pane's cells and forwards a
palette *index* rather than the colour behind it. Anywhere else:

```sh
export OPENTAB_NO_INIT_COLOR=1   # nearest standard 256-colour instead (matched in CIE Lab, so hues survive)
export OPENTAB_NO_INIT_COLOR=0   # force the exact colours back on, e.g. once your terminal is fixed
```

It's an environment variable rather than a CLI flag on purpose: it describes the
*terminal*, not the run — set it once in that terminal's profile.

A multiplexer is the prime suspect whenever colour or glyphs come out wrong, since it
consumes OpenTab's escapes and re-emits its own; `opentab doctor` names the one it can see
and warns when several are nested.

## Boxes and frames render as garbage

OpenTab asks the locale whether the screen can encode its box-drawing glyphs and falls back
to a locale-independent ASCII/ACS frame when it can't. If frames still look wrong, your
locale is claiming UTF-8 while the terminal isn't — check `LANG` / `LC_ALL`; `opentab
doctor` reports what it detected.

## A tool I use isn't showing up

`opentab doctor` separates the cases that look identical from outside — a harness that
isn't installed, one installed but with nothing recorded, one whose export is opt-in
(GitHub Copilot's OTEL export), one pointed a level too deep by a `--*-dir` flag, and VS
Code sessions that exist but recorded no tokens. It also reports a **remembered `H`
choice** pinning you to a single harness, which nothing in the UI announces.

Per-harness paths, flags and env vars: [sources.md](sources.md).

## Sessions show tokens but `$0.00`

That's not a bug — it's usage recorded without a per-token price, normal on subscription
and credit plans. Press **`$`** to reprice those tokens at API list rates, and **`P`** to
see the rates behind the estimate. See [pricing.md](pricing.md).

## `opentab pull` can't reach a machine

For SSH pulls, test login with the same target first. OpenTab uses non-interactive
batch mode, so a connection that needs a password prompt will fail. SSH targets
can include a port:

```sh
opentab pull box=ssh://user@host:2222      # a nonstandard port, inline
```

or give the box a `Host` block in `~/.ssh/config` (`Port`, `IdentityFile`, `ProxyJump`,
a bastion — all of it applies) and pull it by that alias. If `opentab` isn't on the
remote's non-interactive `PATH`, set that machine's `cmd` in `remotes.json` — `cmd` is
the command run **on the far side**, so it cannot carry local `ssh` flags.

For HTTP(S), the URL must return JSON produced by `opentab export`, not the
`opentab web` home page. See [saved connections and URL pulls](machines.md).

## Moving a machine's spend across by hand

When SSH from here isn't possible at all, export on the far side and open the file here.
There is no `import` verb — the file *is* the machine:

```sh
opentab export --label workstation box.json  # there: then transfer the file here
opentab remote box.json                      # here: merge it with local history
```

`opentab remote` also takes several files or a directory. To keep a summary for later,
drop it in the pull directory (`~/.cache/opentab/remotes/`, or `$XDG_CACHE_HOME/opentab/
remotes`) — create it first, OpenTab only makes it when pulling — and a bare `opentab
remote` finds it from then on.

Two things to know:

- **The machine's name comes from the export, not the filename.** If both boxes have the
  same hostname, the imported sessions merge into your local machine — re-export the
  other one with `opentab export --label NAME`. OpenTab says so on startup when it spots
  the collision.
- **A summary that won't parse is skipped**, not fatal, so a truncated copy shows as
  "this machine only". OpenTab names the file it skipped; re-copy it if you see that.

## Notes cannot be saved

An unreadable or malformed `notes.json` blocks edits deliberately: OpenTab will
not replace your authored notes with an empty file. `opentab doctor` reports the
same readability check without moving or changing the file. Back it up, then
check permissions and JSON structure (a top-level object with a `notes` object).
Restore or repair it before retrying; deleting the cache cannot recover notes.
Notes are also intentionally disabled under `--demo` and `--no-state`.
See [note storage](privacy.md#notes-are-authored-data).

## Browser session tabs are missing

`opentab web --html FILE` produces a static snapshot without Turns, Tools or
Context. Use `opentab web` or `opentab web --headless` for live details. Even live,
tabs depend on the selected session's retained data; older fleet summaries may
have only rollups. Re-export on the source machine if its records still exist.
Raw turn traces and notes are local TUI features, not browser tabs.
See [the live browser](web.md#served-live).
