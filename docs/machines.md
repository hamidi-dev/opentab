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
Turns / Tools / Context data, but no full transcripts or turn traces. Summaries
still contain prompt text and identifying metadata such as session titles and project paths.
Use `--demo` when preparing a snapshot for sharing.

If a machine is already serving `opentab web` at an address you can reach, you can
pull from its `http://host:port` URL instead. See [web server access](web.md#security)
for binding beyond localhost.

## Browse and resume

- **`m`** opens Machines mode; **`M`** filters every view to one machine.
- **`L`** resumes a supported session on its original machine when its saved entry
  has an SSH target. Launches and copied commands wrap the resume command in SSH;
  a machine saved only by URL has no SSH resume target.
- **`opentab forget <machine>`** removes a saved machine and its cached summary.

Saved connections live in `~/.config/opentab/remotes.json`; cached summaries live
under `~/.cache/opentab/remotes/`. Both honor their corresponding XDG overrides.

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

A machine's name travels **inside** the export, not in its filename. Give machines
distinct labels with `opentab export --label NAME` when they share a hostname;
otherwise their sessions can merge under the same machine identity.

[Back to the README](../README.md#every-machine-one-tab)
