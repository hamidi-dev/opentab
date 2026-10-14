// Runs OpenTab's real TUI in Pyodide. The page owns xterm.js; this worker owns Python.
// Output goes out by postMessage. Input arrives through a SharedArrayBuffer ring that
// Python reads with a blocking Atomics.wait, which is why the page must be
// cross-origin isolated.
import { loadPyodide } from "./vendor/pyodide/pyodide.mjs";

const HOME = "/tmp/opentab-demo";
let ctrl, ring, sleeper, pyodide;
const decoder = new TextDecoder();

// ctrl: [0] write index, [1] read index, [2] rows, [3] cols, [4] wake counter
function readInput(timeoutMs) {
  const w = Atomics.load(ctrl, 0);
  let r = Atomics.load(ctrl, 1);
  if (w === r && timeoutMs !== 0) {
    const seen = Atomics.load(ctrl, 4);
    Atomics.wait(ctrl, 4, seen, timeoutMs < 0 ? Infinity : timeoutMs);
  }
  const end = Atomics.load(ctrl, 0);
  if (end === r) return "";
  const size = ring.length, bytes = [];
  while (r !== end) { bytes.push(ring[r]); r = (r + 1) % size; }
  Atomics.store(ctrl, 1, r);
  return decoder.decode(new Uint8Array(bytes), { stream: true });
}

const otterm = {
  write: s => postMessage({ type: "out", data: s }),
  read: ms => readInput(ms),
  size: () => [Atomics.load(ctrl, 2) || 24, Atomics.load(ctrl, 3) || 80],
  sleep: ms => { if (ms > 0) Atomics.wait(sleeper, 0, 0, ms); },
  clipboard: text => postMessage({ type: "copy", data: String(text) }),
  // otboot's stdlib fallback: the modules /try/ leaves out of its first download.
  fetch_to: (url, path) => pyodide.FS.writeFile(path, fetchSync(new URL(url, import.meta.url).href)),
};

const status = (text, pct) => postMessage({ type: "status", text, pct });

// Synchronous whole-file read; only workers may block like this.
function fetchSync(url) {
  const xhr = new XMLHttpRequest();
  xhr.open("GET", url, false);
  xhr.responseType = "arraybuffer";
  xhr.send(null);
  if (xhr.status < 200 || xhr.status >= 300) throw new Error(`${url}: HTTP ${xhr.status}`);
  return new Uint8Array(xhr.response);
}

// Files the warm cache covers arrive as lazy files: stat sees their real size and mtime
// (so the cache still hits), and the bytes are fetched on the first read, when a session
// is opened. Emscripten's own lazy file would issue a HEAD per stat instead. A transcript
// is one file; a database (chunk > 0) is split so a query fetches only the pages it reads.
function mountLazy(py, files, version) {
  files.forEach(([rel, size, mtime, chunk = 0], n) => {
    const path = `${HOME}/${rel}`, slash = path.lastIndexOf("/");
    py.FS.mkdirTree(path.slice(0, slash));
    const part = i => new URL(chunk ? `./data/lazy/${n}-${i}.json?v=${version}` : `./data/lazy/${n}.json?v=${version}`, import.meta.url).href;
    const node = py.FS.createLazyFile(path.slice(0, slash), path.slice(slash + 1), part(0), true, false);
    const lazy = node.contents;
    const span = chunk || Math.max(size, 1);
    lazy._length = size;
    lazy._chunkSize = span;
    lazy.lengthKnown = true;
    lazy.setDataGetter(i => {
      if (!lazy.chunks[i]) {
        const bytes = fetchSync(part(i));
        const want = Math.min(span, size - i * span);
        if (bytes.length !== want) throw new Error(`${rel}: expected ${want} bytes, got ${bytes.length}`);
        lazy.chunks[i] = bytes;
      }
      return lazy.chunks[i];
    });
    py.FS.utime(path, mtime * 1000, mtime * 1000);
  });
}

// Downloads several files at once under one progress label. Sizes come from build.json:
// compressed responses carry no Content-Length, and the reader counts decoded bytes.
async function fetchAll(urls, sizes, label, base, span) {
  const total = sizes.reduce((a, b) => a + b, 0);
  let got = 0;
  return Promise.all(urls.map(async url => {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    const reader = res.body.getReader();
    const parts = [];
    let own = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value); own += value.length; got += value.length;
      if (total) status(`${label} ${(got / 1048576).toFixed(1)} MB`, base + span * Math.min(1, got / total));
    }
    const out = new Uint8Array(own);
    let o = 0; for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }));
}

// Pyodide fetches its wasm and stdlib itself, without progress. Downloading them first
// moves the progress bar through the largest part of the wait; this hands Pyodide the bytes.
const preloaded = new Map();
const networkFetch = self.fetch.bind(self);
self.fetch = (input, init) => {
  const url = input instanceof Request ? input.url : String(input);
  const bytes = preloaded.get(url);
  if (!bytes) return networkFetch(input, init);
  preloaded.delete(url);
  const type = url.endsWith(".wasm") ? "application/wasm" : "application/zip";
  return Promise.resolve(new Response(bytes, { headers: { "Content-Type": type } }));
};

onmessage = async (e) => {
  if (e.data.type !== "init") return;
  ctrl = new Int32Array(e.data.ctrl);
  ring = new Uint8Array(e.data.ring);
  sleeper = new Int32Array(new SharedArrayBuffer(4));
  try {
    const build = await (await fetch("./build.json", { cache: "no-store" })).json();
    const indexURL = new URL("./vendor/pyodide/", import.meta.url).href;
    const wasmURL = indexURL + "pyodide.asm.wasm";
    const stdLibURL = new URL(`./stdlib/core.zip?v=${build.stdlib}`, import.meta.url).href;
    status("Downloading Python", 2);
    const [wasm, stdlib] = await fetchAll([wasmURL, stdLibURL], [build.sizes.wasm, build.sizes.stdlib], "Downloading Python", 2, 38);
    preloaded.set(wasmURL, wasm);
    preloaded.set(stdLibURL, stdlib);
    status("Starting Python", 42);
    const py = pyodide = await loadPyodide({ indexURL, stdLibURL, env: { HOME } });
    py.setStdout({ batched: s => postMessage({ type: "log", data: s }) });
    py.setStderr({ batched: s => postMessage({ type: "log", data: s }) });
    py.registerJsModule("otterm", otterm);

    const [app] = await fetchAll([`./app.tar.gz?v=${build.app}`], [build.sizes.app], "Downloading OpenTab", 46, 6);
    py.unpackArchive(app, "gztar", { extractDir: "/app" });
    const [data] = await fetchAll([`./data/home.tar.gz?v=${build.data}`], [build.sizes.data], "Downloading a synthetic year of agent history", 52, 30);
    status("Unpacking history", 82);
    py.unpackArchive(data, "gztar", { extractDir: HOME });
    const lazy = await (await fetch(`./data/lazy/manifest.json?v=${build.lazy}`)).json();
    mountLazy(py, lazy, build.lazy);

    status("Opening OpenTab", 92);
    py.runPython(`
import os, sys
sys.path[:0] = ["/app/py", "/app/src"]
home = ${JSON.stringify(HOME)}
os.environ.update({
    "HOME": home, "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8",
    "XDG_CONFIG_HOME": home + "/.config", "XDG_STATE_HOME": home + "/.local/state",
    "XDG_DATA_HOME": home + "/.local/share", "XDG_CACHE_HOME": home + "/.cache",
    "OPENTAB_BROWSER_DEMO": "1",
})
os.chdir(home)
import otboot
otboot.rebind_unpacked_sources(home + "/.cache/opentab/cache")
otboot.install_stdlib_fallback(${JSON.stringify(`./stdlib/rest.zip?v=${build.rest}`)})
`);
    postMessage({ type: "ready" });
    const run = py.globals.get("otboot").main;
    for (;;) {
      const t0 = performance.now();
      let code, crashed = false;
      try { code = run(py.toPy(e.data.debug ? ["--debug-log", "/tmp/opentab-debug.jsonl"] : [])); }
      catch (err) {
        crashed = true;
        postMessage({ type: "log", data: String(err.message || err) });
        otterm.write(`\x1b[0m\x1b[2J\x1b[H\x1b[?25h\r\n\x1b[31m  OpenTab stopped with an error:\x1b[0m\r\n\r\n${String(err.message || err).split("\n").slice(-12).join("\r\n")}\r\n`);
      }
      postMessage({ type: "exited", code, ms: performance.now() - t0 });
      // The stdlib line is how site/try/stdlib-core.txt is refreshed (see build-try.sh).
      if (e.data.debug) py.runPython(`
import json, sys
for line in open("/tmp/opentab-debug.jsonl"):
    ev = json.loads(line)
    if any(k in json.dumps(ev) for k in ("cache", "reject", "startup", "first_paint", "phase")):
        print(json.dumps(ev)[:300])
print("stdlib " + json.dumps(sorted(n for n, m in list(sys.modules.items()) if str(getattr(m, "__file__", "")).startswith("/lib/python"))))
`);
      if (!crashed) otterm.write("\x1b[0m\x1b[2J\x1b[H\r\n  OpenTab closed.\r\n");
      otterm.write("\r\n  Press any key to open it again.\r\n");
      while (!readInput(-1)) { /* wait */ }
    }
  } catch (err) {
    postMessage({ type: "fatal", data: String(err && err.stack || err) });
  }
};
