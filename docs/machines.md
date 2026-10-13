# Multiple machines

`opentab pull` fetches usage summaries from your other machines and opens them next
to this machine's history. Each remote only needs `opentab` installed; nothing runs
in the background and nothing listens on a port.

```sh
opentab pull laptop workstation gpu-box   # fetch in parallel, then browse
opentab pull                              # refresh every saved machine
opentab remote                            # reopen the last pull without connecting
```

## Pull a machine over SSH

A target is an SSH host alias (`box`), `user@host`, or `name=user@host` to give it
a label. Your SSH config handles keys, ports and bastions. A port also works inline:

```sh
opentab pull box=ssh://user@host:2222
```

Pull runs `opentab export -` on the remote and streams the summary back. Pull uses
SSH batch mode, so set up key-based login first; it never prompts for a password.

A summary contains session titles, project paths, totals, per-model breakdowns and
the Turns / Tools / Context tabs. It can contain **full user prompts**. It never
contains raw turn traces or your notes. Treat a summary as private, even one made
with [demo mode](privacy.md#demo-mode).

## Pull an exported file from a URL

If you host an exported summary on your own access-controlled server, pull it by URL:

```sh
opentab export usage.json                                   # on the source machine
opentab pull workstation=https://private-host.example/usage.json
```

Pulling the URL again fetches whatever file is hosted there, not a fresh scan of that
machine. `opentab web` does not serve machine summaries; there is no fleet HTTP API.

## Browse and resume

| Key | Action |
|-----|--------|
| `m` | Machines browse mode: all machines, then one row per machine |
| `M` | Filter every view to one or several machines (combines with the `H` harness filter) |
| `L` | Resume the selected session on its own machine, over SSH |
| `F` | Re-pull the selected machine; on the fleet row, every loaded machine with a saved connection |
| `r` | Reload this machine's local data (not remote machines) |

In the `M` picker, `Tab` switches Single / Multiple, `Space` toggles a machine, `a`
selects all or none, `Enter` applies and `Esc` cancels. Selecting every machine
clears the filter.

`L` works only for machines saved with an SSH target. It runs the resume command in
the remote user's `$SHELL` as a login shell, so tools installed in `.zshrc` or similar
are found. The harness must be installed for that SSH user. Hermes sessions without a
recorded directory resume from the remote home directory.

## Saved connections

Pull remembers every target in `~/.config/opentab/remotes.json`. Cached summaries live
in `~/.cache/opentab/remotes/`. Both follow their XDG overrides. Remove a machine and
its summary with:

```sh
opentab forget workstation
```

Edit `remotes.json` when OpenTab is not on the remote's non-interactive `PATH`:

```json
{
  "version": 1,
  "machines": {
    "workstation": {
      "ssh": "user@workstation",
      "cmd": "/home/user/.local/bin/opentab export --label workstation -"
    },
    "archive": {
      "url": "https://private-host.example/usage.json"
    }
  }
}
```

| Field | Meaning |
|-------|---------|
| `ssh` | SSH target for pulls, resumes and turn reads |
| `url` | An exported summary to fetch instead (no resume, no turn reads) |
| `cmd` | Command run **on the remote** to export; not a place for local SSH flags |
| `trace_cmd` | Optional argv prefix for reading turns, when `cmd` can't be reused |

Put ports, keys and bastions in SSH config or an `ssh://` target. Only configure
commands you trust.

### Reading turns with a custom `cmd`

Turn reads reuse `cmd`: OpenTab drops its export part (`export`, `--export -`,
`--label`, date ranges) and runs the rest, so the entry above reads turns through
`/home/user/.local/bin/opentab`. Variables such as `$HOME` still expand. Without
`cmd`, turn reads run `opentab`.

Add `trace_cmd` only when `cmd` is not a plain OpenTab call: a pipeline,
`cd x && ...`, a `bash -lc` wrapper, an unknown flag, or a path with spaces. It is a
list of arguments, not a shell string, and nothing in it expands:

```json
"cmd": "bash -lc 'opentab --export -'",
"trace_cmd": ["/opt/tools/opentab"]
```

Write `["env", "NAME=value", "opentab"]`, not `"NAME=value opentab"`. If neither works,
the Turns reader says so on that machine's sessions.

## Read a remote turn

Browsing a pulled machine, including its Turns, Tools and Context tabs, stays offline.
Opening a turn is the one action that connects: it fetches that turn's full trace over
SSH. The reader shows the machine and a loading state.

| Key | Action |
|-----|--------|
| `Enter` | Open the turn and fetch its trace |
| `[` / `]` | Previous / next turn (fetches that turn) |
| `z` | Expand the turn's content (reuses what was fetched) |
| `Esc` | Close the trace and cancel a running fetch |

After a failure, close and reopen the turn to retry. If the turn changed on the
remote since your last pull, the read fails rather than guessing: refresh with `F` or
`opentab pull`, then reopen it. Refreshing cannot bring back content the harness has
deleted.

Remote turns need all of these:

- The summary was pulled into the default `~/.cache/opentab/remotes/` and opened
  through the fleet view, for example with a bare `opentab remote`.
- The machine has a saved SSH connection and a usable turn command (see above).
- The remote runs an OpenTab recent enough to serve turn content, and its harness
  still has the content.

URL entries, summary files you opened by name, symlinked summaries and demo mode
cannot read remote turns. The same one-turn reads are available through the gated
[CLI and MCP content requests](programmatic.md#raw-content).

## Refresh and offline history

Saved summaries are snapshots. The Machines view shows each one's export time and
OpenTab version; this machine is marked live. Nothing polls in the background.

| Command or key | Effect |
|----------------|--------|
| `opentab pull` | Re-fetch every saved machine, in parallel; failures are reported per machine |
| `opentab pull NAME...` | Re-fetch only those machines |
| `opentab remote` | Open cached summaries without connecting |
| `F` | Same as above; on this machine it reloads local data |
| Web report | A per-machine refresh button in `opentab web` |

A failed fetch keeps the previous summary; old usage never drops to zero. Check the
export time when a machine looks stale. Files opened with `opentab remote FILE...`
can't be re-pulled in place, and demo mode disables in-app refresh.

## Move an export yourself

When this machine can't reach the other one, export there:

```sh
opentab export --label workstation usage.json
```

Copy `usage.json` over however you like, then open it here:

```sh
opentab remote usage.json
```

It opens merged with this machine's history; there is no import step.
`opentab remote` accepts several files or a directory. To keep a file for later, put it
in `~/.cache/opentab/remotes/`, and a bare `opentab remote` picks it up.

With SSH available, you can also capture an export directly:

```sh
ssh box opentab export - > box.json
opentab remote box.json
```

## Good to know

- A machine's name travels inside the export, not in the filename. It defaults to
  the hostname; use `opentab export --label NAME` when two machines share one.
- The `name` in `name=target` names the saved connection, not the label inside the
  export.
- `--demo` changes how the fleet view looks after a pull. It does not skip the
  connection or anonymize the remote export.
- Fleet commands always merge every local harness with the summaries, so a narrower
  local `--harness` is rejected. `--harness remote` is accepted as the same thing.
- A fetched turn stays in memory until you leave it, step, reload or change harness.
  OpenTab never caches raw content on disk and never fetches whole sessions.
- Each turn read has 30 seconds and responses are capped (16 MiB output, 100,000
  turns, 10,000 events). Errors never echo the remote's output.

Changing the export format? See [Portable machine summaries](architecture.md#portable-machine-summaries).
