#!/usr/bin/env bash
# Fetch the third-party runtime /try/ serves from its own origin (its CSP allows no CDN):
# Pyodide (CPython on WebAssembly, MPL-2.0) and xterm.js (MIT), pinned by version and
# SHA-256. Files already present with the right hash are kept. Run from anywhere.
set -euo pipefail

vendor="$(cd "$(dirname "${BASH_SOURCE[0]}")/../try" && pwd)/vendor"
npm="https://cdn.jsdelivr.net/npm"

# sha256  local path  source
files="
5dc2fc119108bc148c7457dc86e7675b5c87e1cafd420b9c34c1eaef7b36c010 pyodide/pyodide-lock.json  pyodide@314.0.7/pyodide-lock.json
cc36e3cab04fdfc9a63ff13eb52eae2b911bf46c025cc7b281f394bd3de1d5e6 pyodide/pyodide.asm.wasm   pyodide@314.0.7/pyodide.asm.wasm
f7cdc8ece80678ceb712f8e65ebe6d3a83203a180c399865f49612a051693635 pyodide/pyodide.asm.mjs    pyodide@314.0.7/pyodide.asm.mjs
6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109 pyodide/pyodide.mjs        pyodide@314.0.7/pyodide.mjs
fa1957e5777068fc4f7437f96d860ae2fbe9c19732ba06c84e004ec16dd7dd7a pyodide/python_stdlib.zip  pyodide@314.0.7/python_stdlib.zip
b336ec65a086c056d4804b3d4c2347da5663d3f23c3f25be866467bd8857ad59 xterm/xterm.mjs           @xterm/xterm@6.0.0/lib/xterm.mjs
854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6 xterm/xterm.css           @xterm/xterm@6.0.0/css/xterm.css
b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd xterm/LICENSE             @xterm/xterm@6.0.0/LICENSE
2d87e1bddc73be9111de8beee5370c3bb7aac9c94e18e6f245f02ca741ef1769 xterm/addon-fit.mjs       @xterm/addon-fit@0.11.0/lib/addon-fit.mjs
"

sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -c1-64; }

fetched=0
while read -r hash path source; do
  [[ -n "$hash" ]] || continue
  dest="$vendor/$path"
  [[ -f "$dest" && "$(sha "$dest")" == "$hash" ]] && continue
  mkdir -p "$(dirname "$dest")"
  curl -fsSL --retry 3 "$npm/$source" -o "$dest.part"
  got="$(sha "$dest.part")"
  if [[ "$got" != "$hash" ]]; then
    rm -f "$dest.part"
    echo "vendor: $source has SHA-256 $got, expected $hash" >&2
    exit 2
  fi
  mv "$dest.part" "$dest"
  fetched=$((fetched + 1))
done <<< "$files"
echo "vendor: $fetched fetched, $(find "$vendor" -type f | wc -l | tr -d ' ') files verified in try/vendor"
