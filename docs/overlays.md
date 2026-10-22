# Overlays

Overlays open on top of the view you're in and close with `Esc`: Trends, the model
price table, help, What's New and notifications. This page also covers the global
toggles: estimates, themes, demo mode and hiding your prompts.

| Key | Opens |
|-----|-------|
| `T` | [Trends](#trends) |
| `P` | Model prices ([Pricing](pricing.md)) |
| `?` | [Help](#help) for the current view |
| `W` | [What's New](#whats-new) |
| `N` | [Notifications](#notifications) |
| `$` | [Estimates](#estimates) on / off |
| `w` | [Compare a session at another model's prices](#compare-model) |
| `C` | [Colour theme](#themes) |
| `D` | [Demo mode](#demo-mode) |
| `Ctrl-P` | [Hide your prompts](#hide-your-prompts) |

## Trends

Trends charts spend over the active date range, and ranks what it went to.

| Tab | Shows |
|-----|-------|
| Daily, Weekly, Monthly | Spend per day, week or month |
| Calendar | A heatmap of daily spend |
| Models, Providers, Projects, Harnesses | Rankings |

| Key | Action |
|-----|--------|
| `h` / `l` | Switch tabs |
| `j` / `k` | Page through months, weeks or years; on rankings, select a row |
| `Enter` | On a chart: focus it, then drill into the picked bar or day |
| Arrows | Pick a bar or day in a focused chart |
| `s` | Sort a ranking |
| `~` | Reverse a ranking |
| `Esc` | Back, then close |

`Enter` on a ranked row opens its sessions. A model opens its **Economics** and
**Sessions** tabs, like [Drill into a model](filters.md#drill-into-a-model) but for the
date range. Its chart always uses list rates; the session table keeps your current
price mode.

## Help

`?` lists the keys that work where you are first, then navigation, then the global
keys. It reflects your [remapped keys](configuration.md#remap-any-key). Close it
before using the keys of the view underneath.

## What's New

`W` shows the highlights of this release and earlier ones, offline.

| Key | Action |
|-----|--------|
| `h` / `←` | Newer release |
| `l` / `→` | Older release |
| `j` / `k`, `g` / `G` | Scroll |
| `o` | Open the full release notes on GitHub |
| `Esc`, `q`, `W` | Close |

## Notifications

Messages appear briefly at the bottom and fade. `N` lists the ones you missed,
including warnings about your keymap.

## Estimates

Subscriptions often record `$0.00`. `$` switches from **Recorded** to **Recorded + estimated**,
pricing that usage at API list rates and labeling it an estimate. Press it again for the
recorded spend. OpenTab remembers the
choice. See [Pricing](pricing.md).

## Compare model

`w` reprices the current session at one model's list rates. Pick from the models it
used or from the catalog: `Tab` switches between them, `f` filters, `Enter` selects.
Overview and Subagents show the comparison; every other number stays as it was. Press
`w` again to clear it. It is not remembered between runs.

See [Comparing models](pricing.md#comparing-models-with-w).

## Themes

`C` opens the theme picker. `j` / `k` preview themes live, `Enter` keeps one, `Esc`
goes back to the one you had. The web report uses the same themes.

## Demo mode

`D` scrambles what you choose, so you can share your screen:

| Category | Scrambles |
|----------|-----------|
| **Titles** | Session, prompt, model and machine names |
| **Paths** | Project directories |
| **Turns** | Full prompt text |
| **Spend** | Dollars and token counts |

Leave **Paths** unchecked to keep real project names on an otherwise scrambled screen.
`Space` toggles a category, `a` all or none, `Enter` applies.

While demo mode is on, `D` turns it straight off; your categories are remembered for
next time. From the command line: `--demo` for everything, or `--demo titles,spend`.
Demo mode saves no preferences. More in [Privacy](privacy.md#demo-mode).

## Hide your prompts

`Ctrl-P` replaces the prompts you typed, titles and full text, with stand-ins on every
session's Turns tab. Everything else stays real: prompts the agent sent to subagents,
titles, paths, spend, turn content and search. A Turns CSV export uses the stand-ins too.
Press `Ctrl-P` again to show them; it is not remembered.

## Keys inside overlays

In Trends and the price table, these still work: `?`, `W`, `Ctrl-F`, `C`, `H`, `M` and
`D`. Help, What's New and notifications also accept `Ctrl-F`, and Help opens the theme,
harness, machine and demo pickers.

Other popups take over the keyboard. Their keys are shown as buttons on the bottom
border; clicking one presses it.
