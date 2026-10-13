#!/usr/bin/env bash
# Assemble the publishable site in site/dist/: symlinks dereferenced, tooling left out,
# and privacy gates applied. Any static host can serve the result; _headers carries the
# cross-origin isolation /try/ needs (Cloudflare Pages reads it; other hosts must send
# the same headers themselves).
#   site/tools/dist.sh            stage the current generated files
#   site/tools/dist.sh --build    regenerate them first (build.sh, build-try.sh)
set -euo pipefail

site="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist="$site/dist"

"$site/tools/vendor.sh"
if [[ "${1:-}" == "--build" ]]; then "$site/tools/build.sh"; "$site/tools/build-try.sh"; fi

missing=()
for f in report.html report-details/opentab-session-0.json docs/changelog.json media/tui.mp4 media/tui-poster.png \
         try/build.json try/app.tar.gz try/data/home.tar.gz; do
  [[ -f "$site/$f" ]] || missing+=("$f")
done
if (( ${#missing[@]} )); then
  echo "dist: missing generated files (run with --build): ${missing[*]}" >&2
  exit 2
fi

rm -rf "$dist"
mkdir -p "$dist"
# -L dereferences docs/md, docs/README.md and media/*.png into real files.
rsync -aL --exclude=.DS_Store --exclude=/tools/ --exclude=/dist/ --exclude=/README.md \
  "$site/" "$dist/"

# Everything published is public: files saved owner-only (screenshots, detail exports)
# would otherwise be unreadable to the web server.
chmod -R u+rwX,go+rX "$dist"

# A macOS home path, not a placeholder such as /mnt/c/Users/<you> in the release notes.
home_path='(^|[^A-Za-z])/Users/'
if grep -rlqE "$home_path" "$dist" --include='*.html' --include='*.json'; then
  echo "dist: refusing, a /Users/ path is in the staged site:" >&2
  grep -rlE "$home_path" "$dist" --include='*.html' --include='*.json' >&2
  exit 2
fi
echo "dist: $(du -sh "$dist" | cut -f1) in site/dist"
