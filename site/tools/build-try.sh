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
# No bytecode: macOS's Python would cache it under $HOME/Library with the build's paths.
env -i HOME="$home" XDG_CONFIG_HOME="$home/.config" XDG_STATE_HOME="$home/.local/state" \
  XDG_DATA_HOME="$home/.local/share" XDG_CACHE_HOME="$home/.cache" TERM=xterm-256color \
  LANG=en_US.UTF-8 PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  python3 "$work/warm.py" "$try/py" "$repo/src" >/dev/null 2>&1
rm -rf "$home/.local/state" "$home/.config"   # preferences start fresh in every browser
# Copilot's cache keys on platform paths and misses in the browser anyway.
rm -f "$home"/.cache/opentab/cache/copilot-*.json
find "$home" -name .DS_Store -delete
if grep -rlqa "/Users/" "$home"; then
  echo "build-try: /Users/ path after warming the cache:" >&2; grep -rla "/Users/" "$home" | head >&2; exit 2
fi

# 4. Files the warm cache already covers are only read when a session is opened, so they
#    leave the archive and are recreated in the browser as lazy files with their real size
#    and mtime. A transcript is served whole as data/lazy/<n>.json; the OpenCode database
#    (its cache hits once otboot rebinds the unpacked file's identity) is split into
#    data/lazy/<n>-<i>.json chunks, so opening a session fetches only the pages it reads.
#    The .json name gets the chunks compressed in transit; the worker reads raw bytes.
rm -rf "$try/data/lazy"
mkdir -p "$try/data/lazy"
python3 - "$home" "$try/data/lazy" <<'PY'
import json, os, shutil, sys
home, out = sys.argv[1:]
roots = (".claude/projects", ".codex/sessions", ".gemini/tmp", ".pi/agent/sessions")
databases = (".local/share/opencode/opencode.db",)
CHUNK = 256 * 1024  # a multiple of SQLite's page size
files = []
for root in roots:
    for dirpath, _, names in os.walk(os.path.join(home, root)):
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            n = len(files)
            shutil.move(path, os.path.join(out, f"{n}.json"))  # the directory stays in the archive
            files.append([os.path.relpath(path, home), st.st_size, int(st.st_mtime)])
for rel in databases:
    path = os.path.join(home, rel)
    if not os.path.exists(path):
        continue
    if os.path.exists(path + "-wal"):
        sys.exit(f"build-try: {rel} has a WAL; checkpoint it before splitting")
    st = os.stat(path)
    n = len(files)
    with open(path, "rb") as fh:
        for i, data in enumerate(iter(lambda: fh.read(CHUNK), b"")):
            with open(os.path.join(out, f"{n}-{i}.json"), "wb") as part:
                part.write(data)
    os.remove(path)
    files.append([rel, st.st_size, int(st.st_mtime), CHUNK])
json.dump(files, open(os.path.join(out, "manifest.json"), "w"), separators=(",", ":"))
print(f"try: {len(files)} files load lazily ({sum(f[1] for f in files) / 1048576:.0f} MB)")
PY

# 5. Archives.
COPYFILE_DISABLE=1 tar -czf "$try/data/home.tar.gz" -C "$home" .
mkdir -p "$work/app/src" "$work/app/py"
rsync -a --exclude=__pycache__ --exclude='*.pyc' "$repo/src/opentab" "$work/app/src/"
rsync -a --exclude=__pycache__ --exclude='*.pyc' "$try/py/" "$work/app/py/"
COPYFILE_DISABLE=1 tar -czf "$try/app.tar.gz" -C "$work/app" .

# 6. Pyodide's stdlib, split: core.zip holds the modules listed in try/stdlib-core.txt and
#    loads at startup; rest.zip holds the others, fetched only if one is imported (otboot's
#    fallback). Refresh the list after OpenTab changes: open /try/?debug=1, use every tab,
#    quit with q, and copy the console's "stdlib [...]" names into the file.
"$site/tools/vendor.sh" >/dev/null
rm -rf "$try/stdlib"; mkdir -p "$try/stdlib"
python3 - "$try/vendor/pyodide/python_stdlib.zip" "$try/stdlib-core.txt" "$try/stdlib" <<'PY'
import os, sys, zipfile
src, listing, out = sys.argv[1:]
keep = {line.strip() for line in open(listing) if line.strip() and not line.startswith("#")}
for name in list(keep):  # a kept module needs its packages
    parts = name.split(".")
    keep.update(".".join(parts[:i]) for i in range(1, len(parts)))
def module(entry):  # the module an entry belongs to; data files go with their package
    if entry.endswith(".py"):
        return entry[:-3].replace("/", ".").removesuffix(".__init__")
    return os.path.dirname(entry).replace("/", ".")
core, rest = [], []
with zipfile.ZipFile(src) as z:
    for info in z.infolist():
        mod = module(info.filename)
        kept = mod in keep or mod.startswith("_sysconfigdata") or info.is_dir()
        (core if kept else rest).append((info.filename, z.read(info)))
names = sorted({module(name) for name, _ in rest if name.endswith(".py")})
header = "# Generated by tools/build-try.sh: the modules in rest.zip."
core.append(("_otstdlib_rest.py", f"{header}\nNAMES = frozenset({names!r})\n".encode()))
for label, entries in (("core", core), ("rest", rest)):
    with zipfile.ZipFile(f"{out}/{label}.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in entries:
            z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
size = lambda label: os.path.getsize(f"{out}/{label}.zip") / 1e6
print(f"try: stdlib core {size('core'):.2f} MB ({len(core)} files), rest {size('rest'):.2f} MB ({len(names)} modules)")
PY
# Content hashes in the URLs: Cloudflare rewrites .gz responses to a 4 h browser TTL,
# so a fixed name would keep serving the previous build. build.json itself is fetched
# with no-store and isn't an edge-cached type.
python3 - "$try" <<'PY'
import hashlib, json, os, sys
try_dir = sys.argv[1]
def digest(rel):
    with open(os.path.join(try_dir, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]
# Decoded sizes for the progress bar: compressed responses carry no Content-Length.
size = lambda rel: os.path.getsize(os.path.join(try_dir, rel))
json.dump({"app": digest("app.tar.gz"), "data": digest("data/home.tar.gz"),
           "lazy": digest("data/lazy/manifest.json"),
           "stdlib": digest("stdlib/core.zip"), "rest": digest("stdlib/rest.zip"),
           "sizes": {"wasm": size("vendor/pyodide/pyodide.asm.wasm"), "stdlib": size("stdlib/core.zip"),
                     "app": size("app.tar.gz"), "data": size("data/home.tar.gz")}},
          open(os.path.join(try_dir, "build.json"), "w"))
PY
echo "try: app $(du -h "$try/app.tar.gz" | cut -f1), history $(du -h "$try/data/home.tar.gz" | cut -f1) (cache $(du -sh "$home/.cache" | cut -f1) unpacked)"
