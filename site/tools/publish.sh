#!/usr/bin/env bash
# Publish site/dist/ to Cloudflare Pages (project "opentab", served at opentab.hamidi.dev).
#   site/tools/publish.sh               upload the current generated files
#   site/tools/publish.sh --build       regenerate everything first (needs dev/)
#   site/tools/publish.sh --preview     upload to a preview URL instead of production
# Needs Node for wrangler and a one-time `npx wrangler login`; no secrets live here.
set -euo pipefail

site="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wrangler="wrangler@4.143.0"
branch=main
build=()
for arg in "$@"; do
  case "$arg" in
    --build) build=(--build) ;;
    --preview) branch=preview ;;
    *) echo "publish: unknown option $arg" >&2; exit 2 ;;
  esac
done

"$site/tools/dist.sh" ${build[@]+"${build[@]}"}
# _headers must reach Pages: it carries the isolation the in-browser app depends on.
[[ -f "$site/dist/_headers" ]] || { echo "publish: dist/_headers is missing" >&2; exit 2; }
# From site/: wrangler reads wrangler.toml (the D1 binding) and bundles functions/ (/hit).
cd "$site"
npx --yes "$wrangler" pages deploy dist --project-name opentab --branch "$branch" --commit-dirty=true
