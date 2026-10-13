#!/usr/bin/env bash
# Regenerate the landing page's generated files: report.html from the synthetic
# showcase fixture (opentab web --html, hostname and fixture paths neutralized),
# docs/changelog.json from GitHub Releases, and the hero clip. /try/ has build-try.sh.
# Run from anywhere. Never points OpenTab at real history (showcase.sh enforces that).
set -euo pipefail

site="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$site/.." && pwd)"
showcase="$repo/dev/fixtures/showcase/showcase.sh"
fixture_home="$repo/dev/fixtures/showcase/home"
export SHOWCASE_HOSTNAME=workstation
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# The report shows the whole fixture year, and every session opens into Turns/Tools/Context.
# Embedding those would make a ~69 MB page, so --details-dir writes one small file per
# session to report-details/, fetched only when a visitor opens that session.
"$showcase" web --html "$tmp/report.html" --details-dir "$tmp/report-details" >/dev/null 2>&1
LC_ALL=C sed "s#${fixture_home}#/home/dev#g" "$tmp/report.html" > "$site/report.html"
rm -rf "$site/report-details"
mkdir -p "$site/report-details"
for f in "$tmp"/report-details/*.json; do
  LC_ALL=C sed "s#${fixture_home}#/home/dev#g" "$f" > "$site/report-details/$(basename "$f")"
done
# OpenTab writes details owner-only (they hold prompts); these are synthetic and public.
chmod 644 "$site/report.html" "$site"/report-details/*.json
chmod 755 "$site/report-details"

# The docs changelog: every published GitHub release, fetched at build time (the docs
# CSP keeps the browser off api.github.com). Without gh, the last good file stays.
if gh api 'repos/hamidi-dev/opentab/releases?per_page=100' --paginate \
     --jq '.[] | select(.draft | not) | {tag: .tag_name, name, date: .published_at[:10], url: .html_url, body}' \
     > "$tmp/releases.jsonl" 2>/dev/null; then
  # dev/site-denylist (untracked, one term per line) lists names the site must not show.
  python3 - "$tmp/releases.jsonl" "$site/docs/changelog.json" "$repo/dev/site-denylist" <<'PY'
import json, os, re, sys
src, out, denylist = sys.argv[1:]
releases = [json.loads(line) for line in open(src) if line.strip()]
blob = json.dumps({"releases": releases}, separators=(",", ":"))
assert not re.search(r"(^|[^A-Za-z])/Users/", blob), "a home path is in the release notes"
denied = [t.strip() for t in open(denylist)] if os.path.exists(denylist) else []
hits = [t for t in denied if t and t.lower() in blob.lower()]
assert not hits, f"the release notes name a denied term: {hits}"
open(out, "w").write(blob)
print(f"changelog.json: {len(releases)} releases, newest {releases[0]['tag']}")
PY
else
  echo "build: gh could not list releases; keeping the existing docs/changelog.json" >&2
fi

# The hero's preview clip is an untracked marketing render; the screenshots are
# symlinks into docs/assets.
clip="$repo/dev/dist/youtube/refresh-highlights"
if [[ -f "$clip.clean.mp4" && -f "$clip.poster.png" ]]; then
  cp "$clip.clean.mp4" "$site/media/tui.mp4"
  cp "$clip.poster.png" "$site/media/tui-poster.png"
elif [[ ! -f "$site/media/tui.mp4" ]]; then
  echo "build: no hero clip; render $clip.clean.mp4 first" >&2; exit 2
fi

if grep -rq "/Users/" "$site/report.html" "$site/report-details"; then echo "build: /Users/ path leaked into the report" >&2; exit 2; fi
echo "report.html: $(du -h "$site/report.html" | cut -f1), details: $(ls "$site/report-details" | wc -l | tr -d ' ') files, $(du -sh "$site/report-details" | cut -f1)"
