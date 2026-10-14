#!/usr/bin/env bash
# Fetch the third-party files the site serves from its own origin, so no visit contacts a
# CDN: Pyodide (CPython on WebAssembly, MPL-2.0) and xterm.js (MIT) for /try/, the fonts
# (SIL OFL 1.1) and marked (MIT) for the pages. Pinned by version and SHA-256; files
# already present with the right hash are kept. Run from anywhere.
set -euo pipefail

site="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
npm="https://cdn.jsdelivr.net/npm"

# sha256  path under site/  source
files="
5dc2fc119108bc148c7457dc86e7675b5c87e1cafd420b9c34c1eaef7b36c010 try/vendor/pyodide/pyodide-lock.json  pyodide@314.0.7/pyodide-lock.json
cc36e3cab04fdfc9a63ff13eb52eae2b911bf46c025cc7b281f394bd3de1d5e6 try/vendor/pyodide/pyodide.asm.wasm   pyodide@314.0.7/pyodide.asm.wasm
f7cdc8ece80678ceb712f8e65ebe6d3a83203a180c399865f49612a051693635 try/vendor/pyodide/pyodide.asm.mjs    pyodide@314.0.7/pyodide.asm.mjs
6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109 try/vendor/pyodide/pyodide.mjs        pyodide@314.0.7/pyodide.mjs
fa1957e5777068fc4f7437f96d860ae2fbe9c19732ba06c84e004ec16dd7dd7a try/vendor/pyodide/python_stdlib.zip  pyodide@314.0.7/python_stdlib.zip
b336ec65a086c056d4804b3d4c2347da5663d3f23c3f25be866467bd8857ad59 try/vendor/xterm/xterm.mjs           @xterm/xterm@6.0.0/lib/xterm.mjs
854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6 try/vendor/xterm/xterm.css           @xterm/xterm@6.0.0/css/xterm.css
b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd try/vendor/xterm/LICENSE             @xterm/xterm@6.0.0/LICENSE
2d87e1bddc73be9111de8beee5370c3bb7aac9c94e18e6f245f02ca741ef1769 try/vendor/xterm/addon-fit.mjs       @xterm/addon-fit@0.11.0/lib/addon-fit.mjs
a756882d8a7802ebd4663b4544a62ebedafc7227c1c159ab95eea61393af3f8e vendor/fonts/bricolage-grotesque-latin-standard-normal.woff2      @fontsource-variable/bricolage-grotesque@5.3.0/files/bricolage-grotesque-latin-standard-normal.woff2
e1a04c169c022f63ad24fc1b6fbd932dee1ded62ac08734eeb1d5d51afee38dd vendor/fonts/bricolage-grotesque-latin-ext-standard-normal.woff2  @fontsource-variable/bricolage-grotesque@5.3.0/files/bricolage-grotesque-latin-ext-standard-normal.woff2
923f4ddf0fd39f9b7794ab0df7332f3d95dc43e8ad7ec2289d6d9e8491177f51 vendor/fonts/LICENSE-bricolage-grotesque                         @fontsource-variable/bricolage-grotesque@5.3.0/LICENSE
2c295d99e26dcf357d4d01bcf270fd6924b600c9a13dd8c363ef114f4c6976fa vendor/fonts/inter-latin-opsz-normal.woff2                       @fontsource-variable/inter@5.3.0/files/inter-latin-opsz-normal.woff2
5e6d4fe9d9f4bff8b2a2469d25ab19576bb85331e22c6ed51398e16f95d56a9c vendor/fonts/inter-latin-ext-opsz-normal.woff2                   @fontsource-variable/inter@5.3.0/files/inter-latin-ext-opsz-normal.woff2
3b0a5fca3d17942cde889069889dedbbbd075e9b599968c82a95f4d944e9b345 vendor/fonts/LICENSE-inter                                       @fontsource-variable/inter@5.3.0/LICENSE
18be452724bfdc236c074ca94a249a7f41a86752c7d04ab258ce9ed5651f6a7e vendor/fonts/jetbrains-mono-latin-wght-normal.woff2              @fontsource-variable/jetbrains-mono@5.3.0/files/jetbrains-mono-latin-wght-normal.woff2
79bfdab9ba467e26eea4122e6f2567e188dd8a09a8c730d501fc487c4ab99c6e vendor/fonts/jetbrains-mono-latin-ext-wght-normal.woff2          @fontsource-variable/jetbrains-mono@5.3.0/files/jetbrains-mono-latin-ext-wght-normal.woff2
403581b69dac5cff4079205e01c6b467e56af449ecbd7247693ddb1baafa005b vendor/fonts/LICENSE-jetbrains-mono                              @fontsource-variable/jetbrains-mono@5.3.0/LICENSE
15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894 vendor/marked/marked.min.js                                      marked@12.0.2/marked.min.js
8e3a3f82f59a60958f56ca08f445647c32a4733dc7ca6c2c46f6eb898471ab9c vendor/marked/LICENSE.md                                         marked@12.0.2/LICENSE.md
"

sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -c1-64; }

fetched=0
while read -r hash path source; do
  [[ -n "$hash" ]] || continue
  dest="$site/$path"
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
echo "vendor: $fetched fetched, $(grep -c . <<< "$files") files verified"
