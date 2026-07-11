# Windows & WSL

OpenTab uses Python's `curses`, which native Windows Python doesn't bundle. Two ways
to run it:

## Native Windows (cmd / PowerShell)

Just install and run — `opentab-ai` declares `windows-curses` as a Windows-only
dependency, so pipx pulls in the curses shim for you:

```sh
pipx install opentab-ai
opentab
```

`windows-curses` is just an OS-level provider for the stdlib `curses` module — the
lone runtime dependency, and only on Windows. Confirmed working against the
**OpenCode** source; the file-based backends read plain JSON and should behave the
same, but are less exercised on native Windows. The `o` key opens the selected
directory in Explorer (via `os.startfile`), so reveal-in-folder works natively too.
If `curses` is missing, OpenTab prints a short hint (install `windows-curses`)
instead of crashing.

The web browser (`opentab --web`) is curses-free entirely, so it works on any
Windows Python regardless.

## WSL

`curses` is already there, so a plain `opentab` works.

### Reading the Windows-side OpenCode database

OpenCode itself doesn't have to run inside WSL — even on native Windows it keeps its
database under your Windows home, at
`%USERPROFILE%\.local\share\opencode\opencode.db`, which WSL reads through `/mnt/c`:

```sh
# from inside WSL, reading the Windows-side OpenCode database
opentab --db /mnt/c/Users/<you>/.local/share/opencode/opencode.db
```

If OpenCode runs inside WSL, the default path
(`~/.local/share/opencode/opencode.db`) just works. Either way, `--db` points
OpenTab at any non-standard location.

### Reading the Windows-side VS Code store

Copilot Chat in VS Code works the same way from WSL: chat sessions are stored by the
Windows-side VS Code (also for Remote-WSL windows), under the Windows profile. That
store is *not* scanned by default — reading through `/mnt/c` is slow enough to drag
down every startup — so opt in by pointing `--vscode-dir` at it, e.g. via an alias:

```sh
alias opentab='opentab --vscode-dir "/mnt/c/Users/<you>/AppData/Roaming/Code/User"'
```

Remote-WSL workspaces then resolve back to their in-distro project directories, and
native Windows workspaces to their `/mnt/c/...` paths.
