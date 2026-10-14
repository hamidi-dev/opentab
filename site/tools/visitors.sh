#!/usr/bin/env bash
# Visit counts from the D1 database functions/hit.js writes (see tools/stats.sql).
#   site/tools/visitors.sh            the last 30 days of opentab.hamidi.dev
#   site/tools/visitors.sh 7          the last 7 days
#   site/tools/visitors.sh 30 test    preview deployments instead
# A visit is a first page from outside the site (or typed in); views count every page.
set -euo pipefail

site="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
days="${1:-30}"
which="${2:-live}"
[[ "$days" =~ ^[1-9][0-9]*$ ]] || { echo "visitors: days must be a number" >&2; exit 2; }
[[ "$which" =~ ^(live|test)$ ]] || { echo "visitors: live or test" >&2; exit 2; }

w="site = '$which' AND day >= date('now', '-$((days - 1)) days')"
sql="
SELECT day, SUM(n * (event = 'visit')) AS visits, SUM(n * (event != 'start')) AS views,
       SUM(n * (event = 'start')) AS app_starts FROM hits WHERE $w GROUP BY day ORDER BY day;
SELECT country, SUM(n * (event = 'visit')) AS visits, SUM(n * (event != 'start')) AS views
       FROM hits WHERE $w GROUP BY country ORDER BY visits DESC, views DESC LIMIT 20;
SELECT ref AS referrer, SUM(n) AS visits FROM hits WHERE $w AND event = 'visit' AND ref != ''
       GROUP BY ref ORDER BY visits DESC LIMIT 20;
SELECT page, SUM(n * (event = 'visit')) AS visits, SUM(n * (event != 'start')) AS views
       FROM hits WHERE $w GROUP BY page ORDER BY views DESC;"

cd "$site"
npx --yes wrangler@4.143.0 d1 execute opentab-stats --remote --json --command "$sql" 2>/dev/null |
python3 -c '
import json, sys
titles = ["By day", "By country", "Referring sites (visits)", "By page"]
for title, part in zip(titles, json.load(sys.stdin)):
    rows = part.get("results") or []
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        continue
    cols = list(rows[0])
    width = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  " + "  ".join(c.ljust(width[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r[c]).rjust(width[c]) if isinstance(r[c], int) else str(r[c]).ljust(width[c]) for c in cols))
' && echo
