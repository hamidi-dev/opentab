#!/usr/bin/env bash
# Install opentab.
#
#   From a checkout:    ./install.sh
#   Remote one-liner:   curl -fsSL https://raw.githubusercontent.com/hamidi-dev/opentab/main/install.sh | bash
#   Custom target dir:  BIN_DIR=~/bin ./install.sh
#
# Checkout mode symlinks the local script, so a later `git pull` updates the
# tool instantly. Remote mode downloads the single script into BIN_DIR; re-run
# the one-liner to update. opentab itself is stdlib-only Python 3.9+.
set -euo pipefail

# Source of truth for remote installs (override via OPENTAB_REPO / OPENTAB_REF).
REPO="${OPENTAB_REPO:-hamidi-dev/opentab}"
REF="${OPENTAB_REF:-main}"
RAW="https://raw.githubusercontent.com/$REPO/$REF/opentab"

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
DEST="$BIN_DIR/opentab"

command -v python3 >/dev/null 2>&1 \
  || echo "warning: python3 not found; opentab needs Python 3.9+." >&2

mkdir -p "$BIN_DIR"

# Prefer a local checkout copy (only when run as a real file, not piped via stdin).
SRC=""
if [ -n "${BASH_SOURCE:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SRC_DIR="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  [ -f "$SRC_DIR/opentab" ] && SRC="$SRC_DIR/opentab"
fi

if [ -n "$SRC" ]; then
  ln -sf "$SRC" "$DEST"
  chmod +x "$SRC"
  echo "linked $DEST -> $SRC"
  echo "(git pull in $SRC_DIR to update)"
else
  echo "downloading opentab from $RAW"
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$RAW" -o "$tmp"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp" "$RAW"
  else
    echo "error: need curl or wget to download opentab." >&2
    exit 1
  fi
  head -n1 "$tmp" | grep -q python \
    || { echo "error: downloaded file does not look like opentab." >&2; exit 1; }
  install -m 0755 "$tmp" "$DEST"
  echo "installed $DEST"
  echo "(re-run the install command to update)"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on your PATH. Add to your shell rc:"
     echo "      export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo "done. try: opentab --help"
