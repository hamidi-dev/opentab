"""Serialize the headless App for a self-contained export or local web server.

Each cost travels as recorded and API-equivalent values, so `$` remains a
client-side field swap. Static exports omit lazy session extras; `--serve`
fetches them on drill-in, matching the TUI's startup boundary. Data sources stay
read-only; only an explicitly requested HTML report is written.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import unquote

from opentab import __version__
from opentab.pricing import (
    api_equivalent_cost,
    cache_misses,
    cache_write_1h_price,
    family_label,
    has_known_price,
    is_local_provider,
    model_context_window,
    model_price,
)
from opentab.themes import DEFAULT_THEME
from opentab.util import (
    cached_share,
    context_size,
    model_row_1h_write,
    model_row_split,
    tool_names,
    tool_namespace,
)
from opentab.webpage import render_html

if TYPE_CHECKING:
    import argparse

    from opentab.tui.app import App

DEFAULT_REPORT = "opentab-report.html"
DEFAULT_PORT = 8321
DEFAULT_BIND = "127.0.0.1"


def _money6(value) -> float:
    # Keep sub-cent spend after payload rounding; the UI distinguishes it from zero.
    return round(float(value or 0), 6)


def _node_api_cost(d: dict) -> float:
    # Match App._priced_nodes: a zero-cost node is wholly repriced at list rates.
    real = float(d.get("cost") or 0)
    if real:
        return real
    return api_equivalent_cost(
        d.get("model_name") or "",
        d.get("tokens_input") or 0,
        d.get("tokens_output") or 0,
        d.get("tokens_reasoning") or 0,
        d.get("tokens_cache_read") or 0,
        d.get("tokens_cache_write") or 0,
        d.get("tokens_cache_write_1h") or 0,
    )


def _model_row(r: dict) -> dict:
    # Demo rows lack snapshots because their synthetic cost needs no repricing.
    real = r.get("real_cost", r.get("cost", 0))
    inp, out, reasoning, cr, cw = model_row_split(r)
    return {
        "model": r.get("model_name") or "unknown",
        "runs": int(r.get("runs") or 0),
        "real": _money6(real),
        "api": _money6(r.get("api_cost", real)),
        "tokens": int(r.get("tokens_total") or 0),
        "cacheRead": int(r.get("cache_read") or 0),
        "cacheWrite": int(r.get("cache_write") or 0),
        "output": int(r.get("output") or 0),
        # Full pricing split; the sixth value is a subset of cacheWrite, not extra tokens.
        # Per-model rows are the only exact baseline for sessions that switched models.
        "tok": [int(inp), int(out), int(reasoning), int(cr), int(cw), int(model_row_1h_write(r))],
    }


def _node_row(row) -> dict:
    d = dict(row)
    return {
        "title": d.get("title") or "(untitled)",
        "agent": d.get("agent") or "-",
        "depth": int(d.get("depth") or 0),
        "model": d.get("model_name") or "",
        "date": d.get("created_at") or "",
        "real": _money6(d.get("cost")),
        # This mirrors App._priced_nodes, not the per-model what-if baseline.
        "api": _money6(_node_api_cost(d)),
        "tokens": int(d.get("tokens_total") or 0),
        # The client chooses the target later, so nodes carry the full pricing split.
        "tok": [
            int(d.get("tokens_input") or 0),
            int(d.get("tokens_output") or 0),
            int(d.get("tokens_reasoning") or 0),
            int(d.get("tokens_cache_read") or 0),
            int(d.get("tokens_cache_write") or 0),
            int(d.get("tokens_cache_write_1h") or 0),
        ],
    }


def _whatif_payload(app: App) -> dict:
    # Targets are chosen client-side. Armable models exclude guessed/local rates, while
    # `rates` must cover every used model so a mixed-model baseline drops no tokens.
    rates = {}
    for rows in app._model_by_root.values():
        for m in rows:
            name = str(m.get("model_name") or "")
            if name and name not in rates:
                # The fifth rate prices the token split's 1h cache-write subset.
                rates[name] = [round(float(v), 6) for v in model_price(name)] + [
                    round(cache_write_1h_price(name), 6)
                ]
    # `unpriced` marks fallback-priced baselines as approximate. `catalog` reuses the
    # TUI's canonical ordering; `local` is excluded from both token-economics measures.
    return {
        "models": [
            {"model": name, "tokens": int(tokens), "price": rates[name]}
            for name, tokens in app.whatif_candidates()
        ],
        "local": sorted(name for name in rates if is_local_provider(name)),
        "catalog": [
            {
                "m": name,
                "p": [round(float(v), 6) for v in model_price(name)]
                + [round(cache_write_1h_price(name), 6)],
            }
            for name, _eff, _approx in app.whatif_catalog_candidates()
        ],
        "rates": rates,
        "unpriced": sorted(
            name for name in rates if not has_known_price(name) and not is_local_provider(name)
        ),
    }


def _price_entry(e) -> dict:
    # A zero cache-read rate means missing data, never a discount.
    return {
        "model": e.bare,
        "canon": e.canon,
        "family": e.family,
        "familyLabel": family_label(e.family),
        "routes": list(e.routes),
        "spend": _money6(e.spend),
        "share": e.share,
        "price": [round(float(v), 6) for v in e.price],
        "eff": round(float(e.eff), 6),
        "approx": bool(e.approx),
        "status": getattr(e, "status", ""),
    }


def _catalog_entry(e) -> dict:
    # Thousands of rows travel, so derived/default fields stay client-side.
    out = {
        "m": e.bare,
        "r": e.routes[0] if e.routes else "",
        "p": [round(float(v), 6) for v in e.price],
    }
    if e.share > 0:
        out["u"] = round(e.share, 6)
    if e.status:
        out["s"] = e.status
    return out


def _prices_payload(app: App) -> dict:
    # Reuse App rows and app-wide mix so web pricing remains range-independent and
    # numerically identical to the TUI.
    app._ensure_models()
    prev_view, prev_query = app.prices_view, app.query
    app.query = ""
    try:
        app.prices_view = "flat"
        by_model = [_price_entry(e) for e in app.priced_model_entries()]
        app.prices_view = "provider"
        by_route = [_price_entry(e) for e in app.priced_model_entries()]
        app.prices_view = "all"
        catalog = [_catalog_entry(e) for e in app.priced_model_entries()]
    finally:
        app.prices_view, app.query = prev_view, prev_query
    out = {"byModel": by_model, "byRoute": by_route, "catalog": catalog}
    # Browser pins start from TUI state but persist only in localStorage.
    if app.pinned_models:
        out["pinned"] = sorted(app.pinned_models)
    mix = app.price_token_mix()
    if mix:
        (inp, output, cr, cw), total = mix
        out["mix"] = [inp, output, cr, cw]
        out["mixTokens"] = int(total)
    return out


def build_payload(app: App) -> dict:
    """Serialize the visible App dataset, including both cost modes."""
    app._ensure_models()
    store = app.store
    rows = app.all_workflows
    workflows = []
    models: dict[str, list[dict]] = {}
    nodes: dict[str, list[dict]] = {}
    for w in rows:
        workflows.append(
            {
                "id": w.id,
                "title": w.title,
                "project": app.project_root(w.directory),
                "date": w.created_at,
                # Active work only; null means the backend cannot separate idle time.
                "dur": w.worked_seconds,
                "real": _money6(w.real_total_cost),
                "api": _money6(w.api_total_cost),
                "realRoot": _money6(w.real_root_cost),
                "apiRoot": _money6(w.api_root_cost),
                "subagents": w.subagents,
                "tokens": w.total_tokens,
                "unpriced": w.unpriced_tokens,
                "source": w.source,
                # app.machine_of also assigns untagged local sessions to the live box.
                "machine": app.machine_of(w),
            }
        )
        # What-if totals require per-model rows even when no subagent nodes exist.
        mix = app.model_mix(w.id)
        if mix:
            models[w.id] = [_model_row(r) for r in mix]
        if w.subagents:
            # workflow_nodes applies store-specific demo transforms; isolate failures.
            try:
                nodes[w.id] = [_node_row(r) for r in store.workflow_nodes(w.id)]
            except Exception:  # noqa: BLE001 -- backend-specific errors, all non-fatal
                continue
    meta = {
        "version": __version__,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": getattr(store, "source_name", "") or app.source_key or "data",
        "combined": bool(getattr(store, "combined", False)),
        # Breakdown tabs need a fleet; Machines browse mode does not.
        "machines": bool(app.machines_present),
        "recordsCost": bool(getattr(store, "records_cost", True)),
        "demo": bool(store.demo),
        "range": app.range_label(),
        "theme": getattr(app.args, "theme", DEFAULT_THEME) or DEFAULT_THEME,
        "startApi": bool(app.show_api_prices and not store.demo),
        "home": os.path.expanduser("~"),
        "serve": False,
    }
    return {
        "meta": meta,
        "warnings": app.startup_warnings(),
        "workflows": workflows,
        "models": models,
        "nodes": nodes,
        "prices": _prices_payload(app),
        "whatif": _whatif_payload(app),
        "machineMeta": _machine_meta_payload(app),
    }


def _machine_meta_payload(app: App) -> dict:
    meta_by_name = app.machine_meta()
    if not meta_by_name:
        # Absence of metadata means one live box; a single pulled box still has metadata.
        return {
            app.local_machine_name: {
                "live": True,
                "exportedAt": "",
                "version": "",
                "refreshable": False,
            }
        }
    out = {}
    for name, meta in meta_by_name.items():
        out[name] = {
            "live": bool((meta or {}).get("live")),
            "exportedAt": str((meta or {}).get("exported_at") or ""),
            "version": str((meta or {}).get("opentab_version") or ""),
            "refreshable": app._refresh_backend is not None and bool((meta or {}).get("key")),
        }
    return out


def session_extras(app: App, workflow_id: str) -> dict:
    """Return lazy drill-in data; empty capabilities remain hidden in the page."""
    turns = []
    if app.session_supports_turns(workflow_id):
        # Cumulative-delta backends cannot expose per-request context safely.
        curve = app.session_supports_context_curve(workflow_id)
        for r in app.session_turn_rows(workflow_id):
            real = float(r.get("cost") or 0)
            api = real or api_equivalent_cost(
                r.get("model_name") or "",
                r.get("input") or 0,
                r.get("output") or 0,
                r.get("reasoning") or 0,
                r.get("cache_read") or 0,
                r.get("cache_write") or 0,
                # Long-TTL writes replace the same subset in the 5m bucket.
                r.get("cache_write_1h") or 0,
            )
            turns.append(
                {
                    "time": r.get("time") or "",
                    "agent": r.get("agent") or "-",
                    "depth": int(r.get("depth") or 0),
                    "model": r.get("model_name") or "",
                    "effort": str(r.get("effort") or ""),
                    "real": _money6(real),
                    "api": _money6(api),
                    "tokens": int(r.get("tokens_total") or 0),
                    # Subagent contexts are separate and must not break the main chain.
                    "ctx": 0 if (int(r.get("depth") or 0) or not curve) else context_size(r),
                    "cached": None if not curve else cached_share(r),
                    # Preserve call order/repeats, but reject malformed shapes at the boundary.
                    "tools": tool_names(r.get("tools")),
                    "promptId": r.get("prompt_id") or "",
                    "promptTitle": r.get("prompt_title") or "",
                    "promptFull": r.get("prompt_full") or "",
                }
            )
    # Cache-expiry pricing stays server-side with the canonical TTL rules. Expose only
    # the causes the TUI renders; the client merely labels them.
    expiries = []
    if turns and curve:
        for m in cache_misses(app.session_turn_rows(workflow_id)):
            if m.cause in ("waited", "reasoning"):
                expiries.append(
                    {
                        "i": m.index,
                        "cause": m.cause,
                        "detail": m.detail,
                        "idle": int(m.idle),
                        "ttl": int(m.ttl),
                        "repaid": int(m.repaid),
                        "cost": _money6(m.cost),
                    }
                )
    tools = []
    if app.session_supports_tools(workflow_id):
        for r in app.session_tool_rows(workflow_id):
            real = float(r.get("cost") or 0)
            api = real or api_equivalent_cost(
                r.get("model_name") or "",
                r.get("input") or 0,
                r.get("output") or 0,
                r.get("reasoning") or 0,
                r.get("cache_read") or 0,
                r.get("cache_write") or 0,
                r.get("cache_write_1h") or 0,
            )
            tools.append(
                {
                    "tool": r.get("tool") or "?",
                    "ns": tool_namespace(r.get("tool") or "?"),
                    "calls": int(r.get("calls") or 0),
                    "model": r.get("model_name") or "",
                    "real": _money6(real),
                    "api": _money6(api),
                    "tokens": int(r.get("tokens_total") or 0),
                }
            )
    # Ship measurements; derive presentation-only context stats client-side.
    context = None
    if app.session_supports_context_curve(workflow_id):
        points = []
        windows = set()
        model = ""
        for r in app.session_turn_rows(workflow_id):
            if r.get("depth"):
                continue
            size = context_size(r)
            if size <= 0:
                continue
            model = r.get("model_name") or model
            # Model switches require each point's own window for an honest peak percentage.
            points.append(
                {"t": (r.get("time") or "")[5:16], "v": int(size), "w": model_context_window(model)}
            )
            windows.add(model_context_window(model))
        if points:
            comp = []
            if app.session_supports_context(workflow_id):
                comp = [
                    {
                        "cat": r["category"],
                        "kind": r["kind"],
                        "count": r["count"],
                        "est": r["est_tokens"],
                    }
                    for r in app.session_context_rows(workflow_id)
                ]
            context = {
                "model": model,
                "window": model_context_window(model),
                "mixedWindows": len(windows) > 1,
                "points": points,
                "comp": comp,
            }
    return {"turns": turns, "tools": tools, "context": context, "expiries": expiries}


def html_command(app: App, args: argparse.Namespace) -> int:
    payload = build_payload(app)
    path = os.path.expanduser(args.html or DEFAULT_REPORT)
    text = render_html(payload)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    meta = payload["meta"]
    print(
        f"OpenTab browser: {path} ({len(text) // 1024} kB, "
        f"{len(payload['workflows'])} sessions, {meta['range']}, {meta['source']})"
    )
    return 0


class _Handler(BaseHTTPRequestHandler):
    server_version = f"opentab/{__version__}"

    def log_message(self, format, *args):  # noqa: A002 -- BaseHTTPRequestHandler's name
        pass

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The self-contained page needs only inline assets, its data favicon, and this API.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict) -> None:
        self._send(200, "application/json; charset=utf-8", json.dumps(data).encode("utf-8"))

    def _check_host(self) -> bool:
        # Reject DNS rebinding on loopback; an explicit non-loopback bind opts out.
        if self.server.server_address[0] not in ("127.0.0.1", "::1"):
            return True
        raw = (self.headers.get("Host") or "").strip().lower()
        host = raw[1:].partition("]")[0] if raw.startswith("[") else raw.partition(":")[0]
        if host in ("localhost", "127.0.0.1", "::1") or raw == "::1":
            return True
        self._send(403, "text/plain; charset=utf-8", b"forbidden host")
        return False

    def do_GET(self):
        if not self._check_host():
            return
        path = self.path.split("?", 1)[0]
        server: ReportServer = self.server  # type: ignore[assignment]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", server.page().encode("utf-8"))
        elif path == "/api/reload":
            # GET must remain side-effect free and cross-origin safe.
            self._send(405, "text/plain; charset=utf-8", b"reload is POST-only")
        elif path.startswith("/api/session/"):
            workflow_id = unquote(path[len("/api/session/") :])
            self._send_json(session_extras(server.app, workflow_id))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):
        if not self._check_host():
            return
        path = self.path.split("?", 1)[0]
        server: ReportServer = self.server  # type: ignore[assignment]
        if path == "/api/reload":
            server.reload()
            self._send_json({"ok": True})
        elif path == "/api/refresh":
            # Refresh is POST-only and requires one string name; malformed input must
            # never fall through to the "refresh every machine" API.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (ValueError, TypeError):
                length = 0
            parsed = None
            if length > 0:
                try:
                    parsed = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, OSError):
                    parsed = None
            name = parsed.get("machine") if isinstance(parsed, dict) else None
            if not isinstance(name, str) or not name:
                self._send_json({"ok": True, "results": []})
                return
            results = server.refresh_machine(name)
            self._send_json({"ok": True, "results": [[n, c, e] for n, c, e in results]})
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


class ReportServer(HTTPServer):
    """Serve a cached page and lazy session extras.

    Requests remain single-threaded because store connections forbid concurrent
    access. The serve loop uses one background thread only so the main thread can
    receive Ctrl-C on Windows.
    """

    def __init__(self, address: tuple[str, int], app: App):
        super().__init__(address, _Handler)
        self.app = app
        self._page: str | None = None

    def page(self) -> str:
        if self._page is None:
            payload = build_payload(self.app)
            payload["meta"]["serve"] = True
            self._page = render_html(payload)
        return self._page

    def reload(self) -> None:
        self.app.reload()
        self._page = None

    def refresh_machine(self, name: str | None) -> list:
        results = self.app.refresh_machines_now(name)
        if results:
            self._page = None
        return results


def open_report(url: str) -> bool:
    """Best-effort launch in the default browser; headless hosts return False."""
    import webbrowser

    try:
        return webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001 -- any browser-launch failure is non-fatal
        return False


def serve_command(app: App, args: argparse.Namespace) -> int:
    bind = getattr(args, "bind", DEFAULT_BIND) or DEFAULT_BIND
    port = getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT
    if bind not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write(
            "warning: serving beyond localhost exposes prompt titles, project paths, "
            "and spend to anyone who can reach the port; prefer a VPN/Tailscale "
            "address and never a public interface\n"
        )
    try:
        server = ReportServer((bind, port), app)
    except OSError as exc:
        raise SystemExit(f"cannot bind {bind}:{port}: {exc}") from exc
    server.page()
    host = "localhost" if bind in ("127.0.0.1", "::1") else bind
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"OpenTab browser at {url}  (Ctrl-C to stop)")
    import threading

    if getattr(args, "web", False):
        # Browser launch may block, but this thread never touches a store connection.
        threading.Thread(target=open_report, args=(url,), daemon=True).start()
    # On Windows, Ctrl-C does not wake serve_forever's select(); joining its sole daemon
    # thread keeps the main thread interruptible without making requests concurrent.
    server_thread = threading.Thread(target=server.serve_forever, name="opentab-serve", daemon=True)
    server_thread.start()
    try:
        while server_thread.is_alive():
            server_thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()  # Must run off the serve thread to avoid HTTPServer deadlock.
        server.server_close()
        server_thread.join(timeout=2)
    return 0
