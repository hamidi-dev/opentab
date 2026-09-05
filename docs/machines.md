# Every machine, one tab

`opentab pull` gathers usage summaries from your machines and opens them alongside
your local history. Each SSH remote needs `opentab` on its `PATH` or a configured
command path; no background agent or listening service is required.

## Pull over SSH

```sh
opentab pull laptop workstation gpu-box   # fetch in parallel, then browse
opentab pull                              # refresh all saved machines
opentab remote                            # reopen the last pull, without connecting
```

A target can be an SSH host alias (`box`), `user@host`, or `name=user@host` to give
it a label. SSH config handles keys, ports, and bastions as usual. You can also
specify a port directly:

```sh
opentab pull box=ssh://user@host:2222
```

Pull runs `opentab export -` remotely and streams the summary back over SSH. It
includes session metadata, totals, per-model breakdowns, and available
Turns / Tools / Context data, but no raw turn traces or authored notes. Summaries
can still contain **full user prompts**, session titles and project paths. They
are private usage records, not anonymous statistics; inspect any snapshot before
sharing it, even when using [demo mode](privacy.md#demo-mode).

## Pull an exported URL

HTTP(S) pulls fetch an **exported JSON file** hosted at a URL you can reach:

```sh
opentab pull workstation=https://private-host.example/usage.json
```

Generate that file with `opentab export usage.json` on the source machine and host
it using your own access-controlled file server. The root of `opentab web` serves
HTML, not a machine summary; OpenTab currently has **no fleet-export HTTP API**.
Fetching the URL again gets whatever snapshot is hosted there, not a fresh scan
of that machine's harnesses.

## Browse and resume

- **`m`** opens Machines mode; **`M`** filters every view to one machine.
- **`L`** resumes a supported session on its original machine when its saved entry
  has an SSH target. Launches and copied commands wrap the resume command in SSH;
  a machine saved only by URL has no SSH resume target.
- **`opentab forget <machine>`** removes a saved machine and its cached summary.

Saved connections live in `~/.config/opentab/remotes.json`; cached summaries live
under `~/.cache/opentab/remotes/`. Both honor their corresponding XDG overrides.

### Saved connections

Pull remembers targets automatically. Edit `remotes.json` when you need a custom
remote command, for example when OpenTab is not on the non-interactive SSH `PATH`:

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

Each entry uses `ssh` or `url`; optional `cmd` is a command run **on the SSH
machine**, not a place for local SSH flags. Put ports, keys and bastions in SSH
config or use an `ssh://` target. Pull uses batch mode, so test key-based login
first; it will not prompt for a password. Configure only commands you trust.

Trace reads reuse `cmd`. OpenTab strips its export tail (an `export` subcommand,
`--export -`, `--label`, a date range) and runs what is left as the command prefix,
so the entry above traces through `/home/user/.local/bin/opentab` with nothing
further to configure. That prefix reaches the remote shell exactly as a pull's does,
variables included, so `$HOME/.local/bin/opentab` still expands; the `sessions turns`
and `sessions content` arguments OpenTab appends are quoted. With no `cmd` at all,
trace reads use `opentab`.

Add an optional `trace_cmd` only when `cmd` is not a plain OpenTab invocation OpenTab
can take apart: a pipeline, `cd x && ...`, a `bash -lc` wrapper, an unrecognized flag,
or a path containing spaces. So `"cmd": "bash -lc 'opentab --export -'"` needs
`"trace_cmd": ["/opt/tools/opentab"]` beside it. It is an **argv prefix**, not a shell
string: write `["env", "NAME=value", "opentab"]`, not `"NAME=value opentab"`. Every
argument in it is quoted, so a `trace_cmd` cannot expand `$HOME`; give a real path.
When neither the derived prefix nor a `trace_cmd` is usable, the Turns reader says so
on that machine's sessions instead of doing nothing.

## Read a remote turn

Ordinary fleet browsing, including Turns / Tools / Context and trace capability
checks, stays offline. In the TUI, opening a selected turn explicitly fetches that
turn's trace over SSH. The reader shows the machine and loading state; **`Esc`
closes the trace and cancels an in-flight request**. After a failure, close and
reopen the trace to retry. `[` / `]` steps to another turn and requests that turn.

Remote traces require all of the following:

- The managed default cache directory, `~/.cache/opentab/remotes/` (honoring XDG),
  opened as a directory through the fleet view, such as bare `opentab remote`.
- A saved SSH connection for the summary's encoded cache filename, not its display
  label, plus a valid trace command as described above.
- A compatible remote OpenTab CLI supporting JSON `sessions turns` with content
  keys and gated `sessions content`, and retained content in a supported harness.

URL entries, arbitrary snapshot files/directories, and explicit file/list imports
are unsupported for traces, even when a named file is inside the managed directory.
Symlinked summary files and demo mode cannot enable remote trace reads. Older
summaries remain browsable without promising that live content can be resolved.

The reader keeps **one selected remote turn in memory**, both its capped preview
and fetched full content. `z` and individual output expansion reuse that content;
collapsing does not fetch again. Leaving the turn, stepping, reloading, or changing
harness clears it. There is **no raw-content disk cache** and no bulk session fetch.

Each read first resolves the snapshot turn against the live remote timeline using
its recorded identity and accounting fields, then reads its unique live content
key. It never matches by ordinal or guesses the nearest turn. Stale, missing, or
ambiguous matches fail closed: refresh the summary with `F` or `opentab pull`, then
reopen the turn. Refresh cannot recover content that the harness no longer retains.

Both SSH commands share one **30-second per-turn deadline**. Each response is
limited to 16 MiB stdout and 64 KiB stderr; timelines are capped at 100,000 turns
and content at 10,000 events. Oversized or invalid responses fail rather than being
partially displayed. Errors do not echo remote stderr, payloads, or command arguments.

The same one-turn reads are available through explicitly gated
[CLI/MCP content requests](programmatic.md#raw-content). Raw traces and their
content keys remain absent from web payloads and fleet exports; SSH trace reads
are a separate opt-in channel, not an extension of the summary format.

## Refresh and offline history

Saved summaries are snapshots: Machines shows their export time and OpenTab
version, while this machine is marked live. No background polling is involved.

- `opentab pull` fetches all saved targets; naming targets fetches only those.
  Fetches run in parallel and failures are reported per machine.
- `opentab remote` opens cached summaries without connecting. Ordinary `r` reloads
  local data, not remote harnesses.
- In the TUI, `F` on a machine re-pulls that machine; on the fleet scope it
  re-pulls the loaded machines with saved connections. On the live machine it
  reloads locally. The live web report offers a per-machine refresh button too.
- Files opened explicitly with `opentab remote FILE...` cannot be re-pulled in
  place. Replace the files yourself and reopen them, or use the managed pull
  directory and saved connections. Demo disables in-app fleet refresh.

A failed fetch leaves the previous cached summary available; it does not turn
old usage into zero. Check the export time when a machine looks stale. A URL
refresh can still return an old file if its publisher has not regenerated it.

## Move an export yourself

If this machine cannot reach the other one, run this **on the other machine**:

```sh
opentab export --label workstation usage.json
```

Transfer `usage.json` by your usual method, then run this **here**:

```sh
opentab remote usage.json
```

The file opens merged with this machine's history, with no separate import step.
`opentab remote FILE...` accepts several files or a directory. To retain one for
later browsing, place it in `~/.cache/opentab/remotes/`; a bare `opentab remote`
then picks it up.

You can also capture a remote export directly when SSH is available:

```sh
ssh box opentab export - > box.json
opentab remote box.json
```

A machine's name travels **inside** the export, not in its filename. It defaults
to the source hostname; give machines distinct labels with
`opentab export --label NAME` when they share one, or their usage appears under
the same machine identity. A `name=target` pull alias names the saved connection,
not the label inside the export.

## Contributing to transfers

The portable format in `stores/remote.py` is separate from the local warm-start
cache. Its `opentab_export` version is currently 2 (adding optional Turns / Tools /
Context); `opentab_version` records the producing application's version. Older
summaries still load without those tabs, and unknown fields are tolerated.

Session IDs survive transfer so synced sessions are counted once, with live local
records taking precedence. Keep model rows and extras attached only to sessions
retained from the same file. The export label identifies the machine; the encoded
cache filename identifies its saved connection for refresh and opt-in traces.
Trace routing retains the winning file's provenance per session, even if labels
collide. Preserve that distinction, normalize incoming detail rows before rendering,
and keep raw traces, their content keys and notes out of portable summaries.

[Back to the README](../README.md#every-machine-one-tab)
