# Troubleshooting

Start with **`opentab doctor`** — one block covering this copy of OpenTab (version, how it
was installed, and whether the `opentab` on your `PATH` is even the one that answered),
every harness backend (found, or not found and why, with the fix), the terminal's colour
and glyph capabilities including any multiplexer in the way, the price catalog, and
OpenTab's own files.

It **reports and never repairs** — nothing is created, warmed or fetched — and it reads no
transcript, so it cannot print a prompt, a session title or a project name. Paths fold to
`~` and pulled machines are counted rather than named, which makes the output safe to
paste into a public issue as-is; `--full` opts out for your own eyes. The exit code is `1`
only when something is genuinely broken (a `WARN` never moves it).

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

The pull target is handed straight to `ssh`, so anything `ssh` understands works:

```sh
opentab pull box=ssh://user@host:2222      # a nonstandard port, inline
```

or give the box a `Host` block in `~/.ssh/config` (`Port`, `IdentityFile`, `ProxyJump`,
a bastion — all of it applies) and pull it by that alias. If `opentab` isn't on the
remote's non-interactive `PATH`, set that machine's `cmd` in `remotes.json` — `cmd` is
the command run **on the far side**, so it cannot carry local `ssh` flags.

## Moving a machine's spend across by hand

When SSH from here isn't possible at all, export on the far side and open the file here.
There is no `import` verb — the file *is* the machine:

```sh
ssh box opentab export - > box.json        # on the other box (or run it there directly)
opentab remote box.json                    # here: opens it merged with this machine
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
