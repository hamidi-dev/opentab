# Windows & WSL

OpenTab runs on native Windows and inside WSL. The terminal app needs Python's
`curses`, which native Windows Python doesn't include; installing OpenTab adds it.

## Native Windows (cmd / PowerShell)

```sh
pipx install opentab-ai
opentab
```

On Windows, `opentab-ai` also installs `windows-curses`, its only runtime dependency.
If `curses` is still missing, OpenTab prints a hint to install `windows-curses`.

| Feature | On native Windows |
|---------|-------------------|
| OpenCode | Tested |
| Other harnesses | Read plain files and should work, but are less tested |
| `o` (open folder) | Opens Explorer |
| `L` then `y` (copy resume command) | Copies a PowerShell 5 command that runs only if changing folder succeeds |
| `opentab web` | Works with any Windows Python; it doesn't need `curses` |

## WSL

`curses` is already there, so `opentab` works as is. Harnesses running inside WSL are
found at their usual paths. Records kept by Windows-side tools need a path through
`/mnt/c`.

### Reading the Windows-side OpenCode database

OpenCode on native Windows keeps its database at
`%USERPROFILE%\.local\share\opencode\opencode.db`. Read it from WSL with `--db`:

```sh
opentab --db /mnt/c/Users/<you>/.local/share/opencode/opencode.db
```

### Reading the Windows-side VS Code store

VS Code keeps Copilot Chat sessions under your Windows profile, also for Remote-WSL
windows. OpenTab doesn't scan it from WSL by default, because reading `/mnt/c` slows
every start. Opt in with `--vscode-dir`, for example through an alias:

```sh
alias opentab='opentab --vscode-dir "/mnt/c/Users/<you>/AppData/Roaming/Code/User"'
```

The alias covers plain `opentab`. For a subcommand, put the option after the command:

```sh
command opentab sessions list --vscode-dir "/mnt/c/Users/<you>/AppData/Roaming/Code/User"
```

Remote-WSL workspaces then map back to their folders inside the distro, and Windows
workspaces to their `/mnt/c/...` paths.
