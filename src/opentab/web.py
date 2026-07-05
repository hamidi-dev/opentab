"""The web report: a self-contained HTML export (--html) and a local server (--serve).

A second frontend over the same data the TUI reads: cli builds the usual
*headless* App (which owns the rollups, worktree folding, ignored
projects/sessions, and the $ what-if snapshots), `build_payload()` serializes it
to plain JSON, and `opentab.webpage.render_html()` wraps that in one
self-contained page — inline CSS/JS, no network, no dependencies.
Drill-in/out, sorting, and the $ toggle all run client-side off the embedded JSON
(every row carries both the real and the API-equivalent cost, so the toggle is a
field swap, not a reprice).

`--serve` wraps the same page in a stdlib ThreadingHTTPServer and adds the lazy
per-session extras (Turns/Tools) as JSON endpoints — the exact per-session
drill-in trade-off the TUI makes, which is why the static export omits those two
tabs: embedding them would mean the startup-wide scan the TUI deliberately avoids.
Subagent trees are cheap per-session queries and *are* embedded (only for sessions
that have subagents).

Everything here is read-only on the data sources; the one file written is the
--html report the user asked for.
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
from opentab.pricing import api_equivalent_cost, family_label
from opentab.util import tool_namespace
from opentab.webpage import render_html

if TYPE_CHECKING:
    import argparse

    from opentab.tui.app import App

DEFAULT_REPORT = "opentab-report.html"
DEFAULT_PORT = 8321
DEFAULT_BIND = "127.0.0.1"


def _money6(value) -> float:
    # Costs travel as plain floats rounded past display precision; sub-cent spend
    # still survives (money() renders it "<$0.01").
    return round(float(value or 0), 6)


def _node_api_cost(d: dict) -> float:
    # Mirror App._priced_nodes: a $0 node is wholly unpriced, so its full token
    # columns are the unpriced part and reprice at list rates.
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
    )


def _model_row(r: dict) -> dict:
    # real_cost/api_cost are stamped by App._compute_api_costs; demo rows skip that
    # pass (their cost is already synthetic), so both sides fall back to `cost`.
    real = r.get("real_cost", r.get("cost", 0))
    return {
        "model": r.get("model_name") or "unknown",
        "runs": int(r.get("runs") or 0),
        "real": _money6(real),
        "api": _money6(r.get("api_cost", real)),
        "tokens": int(r.get("tokens_total") or 0),
        "cacheRead": int(r.get("cache_read") or 0),
        "cacheWrite": int(r.get("cache_write") or 0),
        "output": int(r.get("output") or 0),
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
        "api": _money6(_node_api_cost(d)),
        "tokens": int(d.get("tokens_total") or 0),
    }


def _price_entry(e) -> dict:
    # Serialize one PriceEntry (App.priced_model_entries). price is (in,out,cacheR,
    # cacheW) list-price $/M; a 0 cache-read is missing data (the page renders "—"),
    # never a discount. eff is the blend at the app-wide token mix; approx marks a
    # missing cache-read rate billed at input rate.
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
    }


def _prices_payload(app: App) -> dict:
    # The P overlay's data: every priced model you've used, as two row sets --
    # per-canonical-model (flat/by-vendor views) and per-(route, model) (by-provider
    # view) -- plus the app-wide token mix behind the eff $/M blend. Reuses the App's
    # own priced_model_entries so the numbers match the TUI exactly; local models are
    # dropped upstream (no API rate). App-wide, never range-scoped (like the TUI).
    app._ensure_models()
    prev_view, prev_query = app.prices_view, app.query
    app.query = ""
    try:
        app.prices_view = "flat"
        by_model = [_price_entry(e) for e in app.priced_model_entries()]
        app.prices_view = "provider"
        by_route = [_price_entry(e) for e in app.priced_model_entries()]
    finally:
        app.prices_view, app.query = prev_view, prev_query
    out = {"byModel": by_model, "byRoute": by_route}
    mix = app.price_token_mix()
    if mix:
        (inp, output, cr, cw), total = mix
        out["mix"] = [inp, output, cr, cw]
        out["mixTokens"] = int(total)
    return out


def build_payload(app: App) -> dict:
    """Serialize the App's visible dataset (active range, ignored filtered) to the
    plain-JSON shape the page's JS consumes. Mode-independent: every cost carries
    both the real and the API-equivalent figure, so the client owns the $ toggle."""
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
                "real": _money6(w.real_total_cost),
                "api": _money6(w.api_total_cost),
                "realRoot": _money6(w.real_root_cost),
                "apiRoot": _money6(w.api_root_cost),
                "subagents": w.subagents,
                "tokens": w.total_tokens,
                "unpriced": w.unpriced_tokens,
                "source": w.source,
            }
        )
        mix = app.model_mix(w.id)
        if mix:
            models[w.id] = [_model_row(r) for r in mix]
        if w.subagents:
            # Store-level call on purpose: workflow_nodes handles demo transforms
            # itself. One malformed session must not kill the whole export.
            try:
                nodes[w.id] = [_node_row(r) for r in store.workflow_nodes(w.id)]
            except Exception:  # noqa: BLE001 -- backend-specific errors, all non-fatal
                continue
    meta = {
        "version": __version__,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": getattr(store, "source_name", "") or app.source_key or "data",
        "combined": bool(getattr(store, "combined", False)),
        "recordsCost": bool(getattr(store, "records_cost", True)),
        "demo": bool(store.demo),
        "range": app.range_label(),
        "theme": getattr(app.args, "theme", "opentab") or "opentab",
        "startApi": bool(app.show_api_prices and not store.demo),
        "home": os.path.expanduser("~"),
        "serve": False,  # flipped by ReportServer so the page knows extras exist
    }
    return {
        "meta": meta,
        "workflows": workflows,
        "models": models,
        "nodes": nodes,
        "prices": _prices_payload(app),
    }


def session_extras(app: App, workflow_id: str) -> dict:
    """The lazy per-session drill-in data (Turns/Tools), served on demand exactly
    like the TUI fetches it. Unsupported backends return empty lists so the page
    hides the tab rather than showing it empty."""
    turns = []
    if app.session_supports_turns(workflow_id):
        for r in app.session_turn_rows(workflow_id):
            real = float(r.get("cost") or 0)
            api = real or api_equivalent_cost(
                r.get("model_name") or "",
                r.get("input") or 0,
                r.get("output") or 0,
                r.get("reasoning") or 0,
                r.get("cache_read") or 0,
                r.get("cache_write") or 0,
            )
            turns.append(
                {
                    "time": r.get("time") or "",
                    "agent": r.get("agent") or "-",
                    "depth": int(r.get("depth") or 0),
                    "model": r.get("model_name") or "",
                    "real": _money6(real),
                    "api": _money6(api),
                    "tokens": int(r.get("tokens_total") or 0),
                    "promptId": r.get("prompt_id") or "",
                    "promptTitle": r.get("prompt_title") or "",
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
            )
            tools.append(
                {
                    "tool": r.get("tool") or "?",
                    "ns": tool_namespace(r.get("tool") or "?"),
                    "model": r.get("model_name") or "",
                    "real": _money6(real),
                    "api": _money6(api),
                    "tokens": int(r.get("tokens_total") or 0),
                }
            )
    return {"turns": turns, "tools": tools}


def html_command(app: App, args: argparse.Namespace) -> int:
    payload = build_payload(app)
    path = os.path.expanduser(args.html or DEFAULT_REPORT)
    text = render_html(payload)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    meta = payload["meta"]
    print(
        f"report: {path} ({len(text) // 1024} kB, "
        f"{len(payload['workflows'])} sessions, {meta['range']}, {meta['source']})"
    )
    return 0


class _Handler(BaseHTTPRequestHandler):
    server_version = f"opentab/{__version__}"

    def log_message(self, format, *args):  # noqa: A002 -- BaseHTTPRequestHandler's name
        pass  # a status-line poller's access log is noise

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict) -> None:
        self._send(200, "application/json; charset=utf-8", json.dumps(data).encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        server: ReportServer = self.server  # type: ignore[assignment]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", server.page().encode("utf-8"))
        elif path == "/api/reload":
            server.reload()
            self._send_json({"ok": True})
        elif path.startswith("/api/session/"):
            workflow_id = unquote(path[len("/api/session/") :])
            self._send_json(session_extras(server.app, workflow_id))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


class ReportServer(HTTPServer):
    """Serves the rendered report page plus the per-session JSON extras. The page
    (payload included) is built once and cached; /api/reload re-reads the stores
    and invalidates it -- wired to the page's refresh button.

    Deliberately single-threaded: the stores' sqlite connections are bound to the
    thread that created them (check_same_thread), and every request is fast (the
    page is cached, the extras are the same cheap per-session queries the TUI runs
    on drill-in), so serializing a single local user's requests costs nothing."""

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
    server.page()  # build eagerly so the first request is instant and errors surface here
    host = "localhost" if bind in ("127.0.0.1", "::1") else bind
    print(f"opentab report at http://{host}:{server.server_address[1]}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
