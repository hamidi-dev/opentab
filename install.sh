#!/usr/bin/env bash
# Install opentab (the `opentab` command).
#
#   From a checkout:    ./install.sh
#   Remote one-liner:   curl -fsSL https://opentab.hamidi.dev/install.sh | bash
#
# opentab is a Python package (PyPI: opentab-ai) that installs the `opentab`
# command. It needs Python 3.9+ and is stdlib-only at runtime; on native Windows
# it also pulls in windows-curses automatically. We install with pipx (isolated
# venv, easy upgrades). Without pipx we build our own venv, because distros that
# follow PEP 668 (Debian 12+, Ubuntu 23.04+) refuse `pip install --user`; that
# stays the last resort for Pythons without the venv module.
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
  # Re-running the installer upgrades this venv in place.
  VENV="${XDG_DATA_HOME:-$HOME/.local/share}/opentab/venv"
  BIN_DIR="$HOME/.local/bin"
  echo "pipx not found — installing into $VENV"
  echo "  (pipx gives easier upgrades: https://pipx.pypa.io)"
  fail() {
    echo "error: could not install opentab. Install pipx (or python3-venv)," >&2
    echo "       e.g. 'sudo apt install pipx', then re-run this script." >&2
    exit 1
  }
  # Debian and Ubuntu strip ensurepip unless python3-venv is installed; a venv
  # without pip still works when the system pip can install into it.
  # A venv without pip is rebuilt, in case python3-venv arrived since.
  if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    python3 -m venv "$VENV" >/dev/null 2>&1 \
      || { rm -rf "$VENV"; python3 -m venv --without-pip "$VENV" >/dev/null 2>&1; } \
      || rm -rf "$VENV"
  fi
  if [ -x "$VENV/bin/python" ]; then
    if "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
      PIP=("$VENV/bin/python" -m pip)
    else
      PIP=(python3 -m pip --python "$VENV/bin/python")
    fi
    "${PIP[@]}" install --quiet --disable-pip-version-check --upgrade "$TARGET" || fail
    mkdir -p "$BIN_DIR"
    ln -sf "$VENV/bin/opentab" "$BIN_DIR/opentab"
  else
    echo "  python3 has no venv module — trying 'pip install --user'"
    python3 -m pip install --user --upgrade "$TARGET" || fail
    BIN_DIR="$(python3 -m site --user-base)/bin"
  fi
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH. Add to your shell rc:"
       echo "      export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
fi

echo "done. try: opentab --help"
