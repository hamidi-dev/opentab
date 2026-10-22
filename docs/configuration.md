# Configuration

OpenTab needs no configuration. It remembers your view choices between runs, keeps
your session notes in their own file, and lets you remap every key and route launches
through your own script.

| File | Holds |
|------|-------|
| `~/.local/state/opentab/state.json` | Remembered view choices |
| `~/.local/share/opentab/notes.json` | Your [session notes](sessions.md#notes) |
| `~/.config/opentab/keymap.conf` | Your key bindings |
| `~/.config/opentab/launcher` | An optional [launcher hook](#custom-launchers) |

The directories follow `$XDG_STATE_HOME`, `$XDG_DATA_HOME` and `$XDG_CONFIG_HOME` when
set.

## What persists between runs

| Remembered | Not remembered |
|------------|----------------|
| Harness selection and browse mode (`t` / `p` / `u` / `m`) | The date range (`R`) |
| Focused panel and maximized detail pane | The `w` comparison model |
| Sort order of each table | Hidden prompts (`Ctrl-P`) |
| Diff layout in [Changes](changes.md) | Machine and harness filters across machines |
| `$` estimates, the `#` chart metric and the colour theme | |
| Ignored projects and sessions, bookmarks, pinned price rows | |
| Dismissed warnings and the last release you were shown | |

The date range starts fresh on purpose: a range narrowed for one look would otherwise
hide spend in later runs. Pass `--since`, `--until` or `--days` to start on another
range.

`--no-state` turns remembering off for a run. Demo mode never saves anything.

Notes are kept apart from the rest because you wrote them. Each one is saved the
moment you press `Enter`, and a note is never deleted, even when its session no
longer exists.

## Remap any key

Every key in every view, picker, overlay and text field can be remapped, except
`Ctrl-C`, which always quits.

Press `K` to open `keymap.conf` in your editor (`$VISUAL`, then `$EDITOR`, then `vi`).
Save and quit, and the new keys work at once; the footer, help and popup titles show
them. `opentab --keymap` prints the file's path. The file is created on first run,
fully commented.

One line per action. The first key is the one OpenTab displays; more keys are aliases.
An empty value unbinds the action.

```ini
[main]
# sort this list
sort = o
# step back out — session → zoom → browse
back = esc, backspace, h
# an empty value unbinds (comments are full-line only: # or ; at line start)
export =

[menu]
# one line re-teaches j/k in EVERY picker (sort, themes, launch, …)
down = n
up = e
```

### Key names

| Write | For |
|-------|-----|
| `j`, `S`, `ö` | A character; case matters (`S` is Shift-s) |
| `enter`, `esc`, `space`, `shift-space`, `tab`, `shift-tab`, `backspace`, `delete`, `insert` | Named keys |
| `up`, `down`, `left`, `right`, `pgup`, `pgdn`, `home`, `end`, `f1` … `f12` | Movement and function keys |
| `comma` | A literal `,` |
| `ctrl-u` or `^u` | A control chord (letters only) |

### Sections

Each section matches what has the keyboard. A sub-section such as `[menu.sort]` falls
back to its family (`[menu]`) for anything it doesn't name, and anything the file
doesn't name keeps its default. Deleting a line, or the whole file, restores the
defaults.

| Section | Covers |
|---------|--------|
| `[main]` | Browse, zoom and session views |
| `[search]`, `[search.edit]` | [Conversation search](conversation-search.md) commands / typing |
| `[trends]`, `[trends.chart]`, `[trends.drill]` | Trends, a focused chart, a ranked row's sessions |
| `[prices]`, `[prices.sessions]` | The price table and its session list |
| `[help]`, `[whats-new]`, `[notices]` | Help, What's New, notifications |
| `[menu]` | Every picker; per picker: `[menu.sort]`, `[menu.theme]`, `[menu.launch]`, `[menu.range]`, `[menu.whatif]`, `[menu.harness]`, `[menu.machine]`, `[menu.demo]`, `[menu.search-project]`, … |
| `[filter]` | The live filter line |
| `[input]`, `[input.range]` | Text prompts such as notes; the date-range fields |
| `[prompt.prices]`, `[prompt.star]`, `[prompt.warning]` | One-off prompts |

### When something conflicts

A typo never breaks OpenTab. A bad key name, an unknown action, or two actions on one
key fall back to something sensible and show a warning; press `N` to read it.

Binding a key takes it away from the action that had it. Bind `x` to `down` and `x`
no longer clears the filter; OpenTab warns that `clear_filter` now has no key.

The letters in the launch menu are fixed, but are hidden when you bind one of them in
`[menu.launch]`.

### Older keymaps and Space

`Space` and `Shift-Space` now page down and up. If your keymap sets `page_down` or
`page_up` explicitly, add `, space` or `, shift-space`, or delete those lines to get
the defaults. Leave `[prices]` alone: Space pins rows there.

## Custom launchers

If an executable exists at `~/.config/opentab/launcher`, or `$OPENTAB_LAUNCHER` points
to one, every launch from the `L` menu goes to it instead of tmux:

```sh
launcher <kind> <directory> <command>
# kind ∈ window | hsplit | vsplit | popup
# e.g. launcher window /repo/myproj 'claude --resume abc123'
```

If it exits with an error, OpenTab shows its stderr. The footer reads "launch via
launcher hook" while one is active.

For a session from another machine, `<command>` is already the full
`ssh -t … 'cd … && …'` line and `<directory>` is your home directory.

This example routes launches through zellij:

```sh
#!/bin/sh
# ~/.config/opentab/launcher — example: zellij instead of tmux
kind=$1 dir=$2 cmd=$3
case $kind in
  window) exec zellij action new-tab --cwd "$dir" -- sh -c "$cmd" ;;
  popup)  exec zellij run --floating --cwd "$dir" -- sh -c "$cmd" ;;
  *)      exec zellij run --cwd "$dir" -- sh -c "$cmd" ;;
esac
```
