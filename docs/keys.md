# Keys & navigation

OpenTab is keyboard-first, with mouse support throughout. Press `?` anywhere for the
keys that work where you are; the footer shows the most useful ones.

Every key below can be remapped; see [Remap any key](configuration.md#remap-any-key).

![OpenTab browsing a day across all harnesses](assets/harness-browse.png)

## How the views nest

OpenTab has three levels. `Enter` drills in, `Esc` steps back out.

```text
browse ──Enter──▶ zoom ──Enter──▶ session
       ◀──Esc───      ◀──Esc───
```

- **Browse**: a sidebar on the left (Years, Months, Days, or Projects, Harnesses,
  Machines) and a detail pane on the right.
- **Zoom**: the detail pane takes focus. The sidebar stays visible and clickable.
  `+` maximizes the detail pane; OpenTab remembers that.
- **Session**: one session, full screen, with its own tabs.

Switch what the sidebar groups by with `t` (Time), `p` (Projects), `u` (Harnesses)
or `m` (Machines).

### Detail tabs

| Selection | Tabs |
|-----------|------|
| A year, month or day | Overview, Models, Projects, Sessions; Harnesses in the merged `all` view |
| A model (`Enter` on Models) | Economics, Sessions ([Drill into a model](filters.md#drill-into-a-model)) |
| A session | Overview, Subagents, Turns, Tools, Changes, Context, when its harness supports them |

What each session tab shows: [Reading a session](sessions.md). Changes:
[Changes](changes.md).

## Move around

| Key | Action |
|-----|--------|
| `j` / `k`, `↑` / `↓` | Move in a list, or scroll the detail pane |
| `h` / `l`, `←` / `→` | Previous / next detail tab |
| `Enter` | Drill into the selection |
| `Esc`, `Backspace` | Step back out |
| `Tab` / `Shift-Tab` | Cycle the sidebar panels (Years → Months → Days) |
| `1` `2` `3` / `0` | Jump to a sidebar panel / the detail pane, from anywhere |
| `+` | Focus the detail pane; maximize / restore it |
| `PgDn` / `PgUp` | Half a page; `Ctrl-D` / `Ctrl-U` and `Space` / `Shift-Space` too |
| `g` / `G` | Top / bottom |

Each panel shows its number in its title (`[1] Years`, `[0]` for the detail pane).

The mouse works too: the wheel scrolls, a click selects, a double-click drills in.
Click a tab to switch to it, or a column header to sort (click again, or press `~`, to reverse).

## Key reference

### Everywhere

| Key | Action |
|-----|--------|
| `?` | Keys for the current view |
| `q` | Quit (`Ctrl-C` always quits and cannot be remapped) |
| `r` | Reload the data |
| `R` | Date range ([Scope & filters](filters.md#pick-a-date-range)) |
| `a` | Back to all time |
| `t` / `p` / `u` / `m` | Browse by Time / Projects / Harnesses / Machines |
| `H` | Harness picker |
| `M` | Machine picker (with several machines) |
| `F` | Pull machine summaries again over SSH (with several machines) |
| `$` | Estimates: add list-price estimates to recorded spend ([Pricing](pricing.md)) |
| `T` | Trends ([Overlays](overlays.md#trends)) |
| `P` | Model prices ([Pricing](pricing.md)) |
| `C` | Colour theme |
| `D` | Demo mode, for screenshots |
| `Ctrl-P` | Hide your prompts without demo mode |
| `W` | What's New |
| `N` | Notifications you missed |
| `K` | Edit the keymap in `$EDITOR` |
| `Ctrl-F` | [Search past conversations](conversation-search.md) |

### Lists

| Key | Action |
|-----|--------|
| `s`, `S` | Sort picker |
| `~` | Reverse the sort |
| `f`, `/` | Filter the list as you type |
| `x` | Clear the filter |
| `e` | Export the list to CSV |
| `i` / `I` | Ignore the selection / show ignored rows |
| `b` / `B` | Bookmark a session / show only bookmarks |
| `n` | Note on a session |
| `o` | Open the session's or project's directory |
| `L` | Resume the session in its own tool |
| `y` | Copy the session's conversation as Markdown |
| `w` | Compare the session at another model's prices |

Details: [Working with sessions](sessions.md#working-with-sessions) and
[Scope & filters](filters.md).

### Inside a turn

| Key | Action |
|-----|--------|
| `[` / `]` | Previous / next turn of the same prompt |
| `{` / `}` | Previous / next tool call |
| `c` | Pick a tool call from a list |
| `z` | Show the full recorded content / back to the preview |
| `Enter` | Expand / collapse the selected tool's output |
| `y` | Copy the selected call: arguments, parameters and output |
| `Y` | Copy only the command / arguments |
| `O` | Copy only the output |

See [Turns](sessions.md#turns).

### Changes

| Key | Action |
|-----|--------|
| `v` | Side-by-side / Unified diff |
| `d` | Open the diff in `$OPENTAB_DIFF_PAGER` |
| `[` / `]` | Previous / next recorded edit of the file |

See [Changes](changes.md).

### Conversation search

| Key | Action |
|-----|--------|
| `Enter` | From the query to the results; open the selected match |
| `Ctrl-F`, `/` | Back to the query |
| `Ctrl-G` | Filters: project, harness, message date |
| `h` / `l` | Results / Conversation tab |
| `Tab` / `Shift-Tab` | Finish typing / switch focus |
| `J` / `K` | Scroll only the preview |
| `S` | Scope: all sessions / this session |
| `s` / `a` | This session / all sessions, keeping other filters |
| `H` | Harness |
| `p` | Project |
| `d` | Message dates: today, last 7 / 30 days, or `YYYY-MM-DD..YYYY-MM-DD` (UTC) |
| `R` | Clear project, harness and dates |
| `I` | Update the index |
| `[` / `]` | Earlier / later messages |
| `L` | Launch or copy the session |
| `Esc` | Back, then close |

See [Conversation search](conversation-search.md).

### Pickers and prompts

| Key | Action |
|-----|--------|
| `j` / `k`, `g` / `G` | Move the highlight |
| `Enter` | Apply |
| `Esc` | Cancel |
| `Space` | Toggle an item (multi-select pickers) |
| `a` | All / none (multi-select pickers) |
| `Tab` | Switch picker tabs, such as Single / Multiple |
| `Ctrl-U` / `Ctrl-W` | Clear the field / delete a word |

Buttons on a popup's bottom border are clickable and press their key.

## Space and Shift-Space

`Space` pages down and `Shift-Space` pages up in scrollable views. Where Space has its
own job, it keeps it: pinning in the price table, toggling items in multi-select
pickers, and typing a space in text fields.

Many terminals send the same code for `Shift-Space` and `Space`. OpenTab recognizes
`ESC [ 32 ; 2 u` and `ESC [ 27 ; 2 ; 32 ~`. If yours sends plain Space, map
`Shift-Space` to `\x1b[32;2u` in the terminal, or use `PgUp` / `Ctrl-U`.

## Reading the numbers

- `<$0.01` is a real cost under one cent. A red `$0.00` means **unpriced**: tokens
  with no known price. See [Pricing](pricing.md).
- Git worktrees count under their main repository. `--no-worktrees` keeps them apart.

## Supporting OpenTab

After your third launch, OpenTab asks once whether you'd like to star it on GitHub.

| Key | Action |
|-----|--------|
| `o` | Open the repository; OpenTab stops asking |
| `r`, `Esc` | Remind me in at least 30 days |
| `d` | Don't ask again |

To hear about releases, use **Watch → Custom → Releases** on GitHub. The prompt never
appears in demo mode, with `--no-state`, in web reports or in scripted commands.
