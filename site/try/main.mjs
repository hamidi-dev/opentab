import { Terminal } from "./vendor/xterm/xterm.mjs";
import { FitAddon } from "./vendor/xterm/addon-fit.mjs";

const params = new URLSearchParams(location.search);
if (params.has("embed")) document.documentElement.classList.add("embed");

const step = document.querySelector("[data-step]");
const bar = document.querySelector("[data-bar]");
const err = document.querySelector("[data-err]");
const loader = document.querySelector("[data-loader]");

if (!self.crossOriginIsolated || typeof SharedArrayBuffer === "undefined") {
  // The worker blocks on shared memory for keyboard input, which browsers only allow when
  // the server sends the isolation headers (see site/_headers).
  step.textContent = "The in-browser app can't start here: this page was served without the "
    + "security headers it needs (a plain local server, or a browser that blocks them).";
  err.innerHTML = 'Open the <a href="../report.html">web report</a> instead, or run the real thing: '
    + "<code>pipx install opentab-ai</code>, then <code>opentab</code>.";
  bar.style.width = "0";
  throw new Error("not cross-origin isolated");
}

const term = new Terminal({
  fontFamily: 'ui-monospace, Menlo, "DejaVu Sans Mono", "Cascadia Mono", Consolas, monospace',
  fontSize: params.has("embed") ? 13 : 14,
  cursorBlink: true,
  allowProposedApi: true,
  theme: { background: "#1a1b26", foreground: "#c0caf5", cursor: "#c0caf5" },
  scrollback: 0,
});
const fit = new FitAddon();
term.loadAddon(fit);
term.open(document.getElementById("term"));
fit.fit();

// Input ring shared with the worker. ctrl: [0] write, [1] read, [2] rows, [3] cols, [4] wake.
const ctrlBuf = new SharedArrayBuffer(5 * 4);
const ringBuf = new SharedArrayBuffer(1 << 16);
const ctrl = new Int32Array(ctrlBuf);
const ring = new Uint8Array(ringBuf);
const enc = new TextEncoder();
function send(text) {
  const bytes = enc.encode(text);
  let w = Atomics.load(ctrl, 0);
  for (const b of bytes) { ring[w] = b; w = (w + 1) % ring.length; }
  Atomics.store(ctrl, 0, w);
  Atomics.add(ctrl, 4, 1);
  Atomics.notify(ctrl, 4);
}
function sendSize() {
  Atomics.store(ctrl, 2, term.rows);
  Atomics.store(ctrl, 3, term.cols);
}
sendSize();
term.onData(send);
term.onBinary(send);
// Refit only when the container's own size changes, once per frame, so nothing the
// terminal renders can feed back into another resize.
let observed = "", refit = 0;
new ResizeObserver(([entry]) => {
  const { width, height } = entry.contentRect;
  const size = `${Math.round(width)}x${Math.round(height)}`;
  if (size === observed) return;
  observed = size;
  cancelAnimationFrame(refit);
  refit = requestAnimationFrame(() => {
    const before = [term.rows, term.cols];
    fit.fit();
    if (before[0] !== term.rows || before[1] !== term.cols) { sendSize(); send(`\x1b[8;${term.rows};${term.cols}t`); }
  });
}).observe(document.getElementById("term"));

let painted = 0;
const worker = new Worker(new URL("./worker.mjs", import.meta.url), { type: "module" });
worker.onmessage = ({ data: m }) => {
  if (m.type === "out") {
    term.write(m.data);
    if (!loader.classList.contains("done") && painted++ > 0) { bar.style.width = "100%"; loader.classList.add("done"); term.focus(); console.log("[opentab] first paint", Math.round(performance.now()) + "ms"); }
  }
  else if (m.type === "status") { step.textContent = m.text; if (m.pct != null) bar.style.width = m.pct + "%"; }
  else if (m.type === "ready") { step.textContent = "Reading a year of history"; bar.style.width = "96%"; }
  else if (m.type === "copy") navigator.clipboard?.writeText(m.data).catch(() => {});
  else if (m.type === "log") console.log("[opentab]", m.data);
  else if (m.type === "fatal") { step.textContent = "OpenTab couldn't start."; err.textContent = m.data; }
  else if (m.type === "exited") console.log("[opentab] exited", m.code, Math.round(m.ms) + "ms");
};
worker.onerror = e => { step.textContent = "OpenTab couldn't start."; err.textContent = e.message || String(e); };
worker.postMessage({ type: "init", ctrl: ctrlBuf, ring: ringBuf, debug: params.has("debug") });
