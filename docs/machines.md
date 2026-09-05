# Every machine, one tab

`opentab pull` gathers usage summaries from your machines and opens them alongside
your local history. Each SSH remote needs `opentab` on its `PATH`; no background
agent or listening service is required.

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
cache filename identifies its saved connection for refresh. Preserve that
distinction, normalize incoming detail rows before rendering, and keep raw traces,
their local content keys and notes out of transfers.

[Back to the README](../README.md#every-machine-one-tab)
