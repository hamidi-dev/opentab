#!/usr/bin/env bash
# Install opentab (the `opentab` command).
#
#   From a checkout:    ./install.sh
#   Remote one-liner:   curl -fsSL https://raw.githubusercontent.com/hamidi-dev/opentab/main/install.sh | bash
#
# opentab is a Python package (PyPI: opentab-ai) that installs the `opentab`
# command. It needs Python 3.9+ and is stdlib-only at runtime; on native Windows
# it also pulls in windows-curses automatically. We install with pipx (isolated
# venv, easy upgrades) and fall back to `pip install --user`.
set -euo pipefail

# Published distribution name on PyPI (the import package + command stay `opentab`).
PYPI_NAME="${OPENTAB_PYPI_NAME:-opentab-ai}"

command -v python3 >/dev/null 2>&1 \
  || { echo "error: python3 not found; opentab needs Python 3.9+." >&2; exit 1; }

# Install from the local checkout when run as a real file inside the repo (so an
# in-tree build is used); otherwise install the published package from PyPI.
TARGET="$PYPI_NAME"
if [ -n "${BASH_SOURCE:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SRC_DIR="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  [ -f "$SRC_DIR/pyproject.toml" ] && TARGET="$SRC_DIR"
fi

if command -v pipx >/dev/null 2>&1; then
  echo "installing opentab with pipx ($TARGET)"
  pipx install --force "$TARGET"
  pipx ensurepath >/dev/null 2>&1 || true
else
  echo "pipx not found — using 'pip install --user' instead."
  echo "  (recommended: install pipx, then re-run — https://pipx.pypa.io)"
  python3 -m pip install --user --upgrade "$TARGET"
  BIN_DIR="$(python3 -m site --user-base)/bin"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR may not be on your PATH. Add to your shell rc:"
       echo "      export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
fi

echo "done. try: opentab --help"
