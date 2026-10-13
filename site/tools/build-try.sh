#!/usr/bin/env bash
# Build the in-browser OpenTab (/try/): app.tar.gz (OpenTab source + browser curses)
# and data/home.tar.gz (a synthetic year at /tmp/opentab-demo, with a pre-built cache).
# The browser mounts the history at the same path, so the cache's (path, size,
# mtime) fingerprints match and the first launch is a warm start.
set -euo pipefail

site="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$site/.." && pwd)"
try="$site/try"
home=/tmp/opentab-demo
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT

# 1. Synthetic history at the browser's path.
rm -rf "$home"
python3 "$repo/dev/fixtures/showcase/generate.py" --out "$home" >/dev/null
if grep -rlq "/Users/" "$home"; then echo "build-try: /Users/ path in the fixture" >&2; exit 2; fi

# 2. Whole-second mtimes survive tar and the browser filesystem exactly.
python3 - "$home" <<'PY'
import os, sys
for root, dirs, files in os.walk(sys.argv[1]):
    for name in dirs + files:
        p = os.path.join(root, name)
        st = os.lstat(p)
        s = int(st.st_mtime)
        os.utime(p, ns=(s * 10**9, s * 10**9), follow_symlinks=False)
PY

# 3. Warm the cache through the same browser curses + inline threads, natively.
cat > "$work/warm.py" <<'PY'
import sys, types
fake = types.ModuleType("otterm")
fake.write = lambda s: None
fake.read = lambda ms: "q"
fake.size = lambda: [40, 140]
fake.sleep = lambda ms: None
sys.modules["otterm"] = fake
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import otboot
sys.exit(otboot.main([]))
PY
env -i HOME="$home" XDG_CONFIG_HOME="$home/.config" XDG_STATE_HOME="$home/.local/state" \
  XDG_DATA_HOME="$home/.local/share" XDG_CACHE_HOME="$home/.cache" TERM=xterm-256color \
  LANG=en_US.UTF-8 PATH=/usr/bin:/bin python3 "$work/warm.py" "$try/py" "$repo/src" >/dev/null 2>&1
rm -rf "$home/.local/state" "$home/.config"   # preferences start fresh in every browser
find "$home" -name .DS_Store -delete

# 4. Transcripts the warm cache already covers are only read when a session is opened, so
#    they leave the archive. Each is served as data/lazy/<n>.json (JSON type, so nginx
#    gzips it) and recreated in the browser as a lazy file with its real size and mtime.
rm -rf "$try/data/lazy"
mkdir -p "$try/data/lazy"
python3 - "$home" "$try/data/lazy" <<'PY'
import json, os, shutil, sys
home, out = sys.argv[1:]
roots = (".claude/projects", ".codex/sessions", ".gemini/tmp", ".pi/agent/sessions")
files = []
for root in roots:
    for dirpath, _, names in os.walk(os.path.join(home, root)):
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            n = len(files)
            shutil.move(path, os.path.join(out, f"{n}.json"))  # the directory stays in the archive
            files.append([os.path.relpath(path, home), st.st_size, int(st.st_mtime)])
json.dump(files, open(os.path.join(out, "manifest.json"), "w"), separators=(",", ":"))
print(f"try: {len(files)} transcripts load lazily ({sum(f[1] for f in files) / 1048576:.0f} MB)")
PY

# 5. Archives.
COPYFILE_DISABLE=1 tar -czf "$try/data/home.tar.gz" -C "$home" .
mkdir -p "$work/app/src" "$work/app/py"
rsync -a --exclude=__pycache__ --exclude='*.pyc' "$repo/src/opentab" "$work/app/src/"
rsync -a --exclude=__pycache__ --exclude='*.pyc' "$try/py/" "$work/app/py/"
COPYFILE_DISABLE=1 tar -czf "$try/app.tar.gz" -C "$work/app" .
# Content hashes in the URLs: Cloudflare rewrites .gz responses to a 4 h browser TTL,
# so a fixed name would keep serving the previous build. build.json itself is fetched
# with no-store and isn't an edge-cached type.
python3 - "$try" <<'PY'
import hashlib, json, os, sys
try_dir = sys.argv[1]
def digest(rel):
    with open(os.path.join(try_dir, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]
json.dump({"app": digest("app.tar.gz"), "data": digest("data/home.tar.gz"),
           "lazy": digest("data/lazy/manifest.json")},
          open(os.path.join(try_dir, "build.json"), "w"))
PY
echo "try: app $(du -h "$try/app.tar.gz" | cut -f1), history $(du -h "$try/data/home.tar.gz" | cut -f1) (cache $(du -sh "$home/.cache" | cut -f1) unpacked)"
