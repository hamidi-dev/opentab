// Runs OpenTab's real TUI in Pyodide. The page owns xterm.js; this worker owns Python.
// Output goes out by postMessage. Input arrives through a SharedArrayBuffer ring that
// Python reads with a blocking Atomics.wait, which is why the page must be
// cross-origin isolated.
import { loadPyodide } from "./vendor/pyodide/pyodide.mjs";

const HOME = "/tmp/opentab-demo";
let ctrl, ring, sleeper;
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

// Transcripts the warm cache covers arrive as lazy files: stat sees their real size and
// mtime (so the cache still hits), and the bytes are fetched on the first read, when a
// session is opened. Emscripten's own lazy file would issue a HEAD per stat instead.
function mountLazy(py, files, version) {
  files.forEach(([rel, size, mtime], n) => {
    const path = `${HOME}/${rel}`, slash = path.lastIndexOf("/");
    py.FS.mkdirTree(path.slice(0, slash));
    const url = new URL(`./data/lazy/${n}.json?v=${version}`, import.meta.url).href;
    const node = py.FS.createLazyFile(path.slice(0, slash), path.slice(slash + 1), url, true, false);
    const lazy = node.contents;
    lazy._length = size;
    lazy._chunkSize = Math.max(size, 1);
    lazy.lengthKnown = true;
    lazy.setDataGetter(() => {
      if (!lazy.chunks[0]) {
        const bytes = fetchSync(url);
        if (bytes.length !== size) throw new Error(`${rel}: expected ${size} bytes, got ${bytes.length}`);
        lazy.chunks[0] = bytes;
      }
      return lazy.chunks[0];
    });
    py.FS.utime(path, mtime * 1000, mtime * 1000);
  });
}

async function fetchBytes(url, label, base, span) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  const total = +res.headers.get("x-ot-size") || +res.headers.get("content-length") || 0;
  const reader = res.body.getReader();
  const parts = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value); got += value.length;
    if (total) status(`${label} ${(got / 1048576).toFixed(1)} MB`, base + span * Math.min(1, got / total));
  }
  const out = new Uint8Array(got);
  let o = 0; for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

onmessage = async (e) => {
  if (e.data.type !== "init") return;
  ctrl = new Int32Array(e.data.ctrl);
  ring = new Uint8Array(e.data.ring);
  sleeper = new Int32Array(new SharedArrayBuffer(4));
  try {
    status("Starting Python", 2);
    const py = await loadPyodide({ indexURL: new URL("./vendor/pyodide/", import.meta.url).href, env: { HOME } });
    py.setStdout({ batched: s => postMessage({ type: "log", data: s }) });
    py.setStderr({ batched: s => postMessage({ type: "log", data: s }) });
    py.registerJsModule("otterm", otterm);

    const build = await (await fetch("./build.json", { cache: "no-store" })).json();
    const app = await fetchBytes(`./app.tar.gz?v=${build.app}`, "Downloading OpenTab", 20, 10);
    py.unpackArchive(app, "gztar", { extractDir: "/app" });
    const data = await fetchBytes(`./data/home.tar.gz?v=${build.data}`, "Downloading a synthetic year of agent history", 30, 50);
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
      if (e.data.debug) py.runPython(`
import json
for line in open("/tmp/opentab-debug.jsonl"):
    ev = json.loads(line)
    if any(k in json.dumps(ev) for k in ("cache", "reject", "startup", "first_paint", "phase")):
        print(json.dumps(ev)[:300])
`);
      if (!crashed) otterm.write("\x1b[0m\x1b[2J\x1b[H\r\n  OpenTab closed.\r\n");
      otterm.write("\r\n  Press any key to open it again.\r\n");
      while (!readInput(-1)) { /* wait */ }
    }
  } catch (err) {
    postMessage({ type: "fatal", data: String(err && err.stack || err) });
  }
};
