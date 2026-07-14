"""The web browser: a self-contained HTML export (--html) and a local server (--serve).

A second frontend over the same data the TUI reads: cli builds the usual
*headless* App (which owns the rollups, worktree folding, ignored
projects/sessions, and the $ what-if snapshots), `build_payload()` serializes it
to plain JSON, and `opentab.webpage.render_html()` wraps that in one
self-contained page — inline CSS/JS, no network, no dependencies.
Drill-in/out, sorting, and the $ toggle all run client-side off the embedded JSON
(every row carries both the real and the API-equivalent cost, so the toggle is a
field swap, not a reprice).

`--serve` wraps the same page in a stdlib HTTP server and adds the lazy
per-session extras (Turns/Tools) as JSON endpoints — the exact per-session
drill-in trade-off the TUI makes, which is why the static export omits those two
tabs: embedding them would mean the startup-wide scan the TUI deliberately avoids.
Subagent trees are cheap per-session queries and *are* embedded -- for every
session, since the `w` what-if has to answer for a solo one too (see build_payload).
`--web` is `--serve` plus popping the browser open in the user's default web
browser (stdlib `webbrowser`, so cross-platform).

Everything here is read-only on the data sources; the one file written is the
--html browser the user asked for.
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
from opentab.pricing import api_equivalent_cost, family_label, model_context_window, model_price
from opentab.themes import DEFAULT_THEME
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
        # `api` is the node's own-model list price for EVERY token (_node_api_cost ==
        # App._priced_nodes(always=True)), which is what makes it the `w` what-if's
        # baseline -- always, independent of the $ toggle. A $-gated baseline would
        # compare a real counterfactual against a subscription backend's unrecorded $0
        # and claim a 100% saving that never happened.
        "api": _money6(_node_api_cost(d)),
        "tokens": int(d.get("tokens_total") or 0),
        # The full token split, in api_equivalent_cost's argument order:
        # [input, output, reasoning, cacheRead, cacheWrite]. The `w` what-if cannot
        # travel precomputed (the target model is picked at view time), so the page
        # gets the ingredients and reprices each node itself.
        "tok": [
            int(d.get("tokens_input") or 0),
            int(d.get("tokens_output") or 0),
            int(d.get("tokens_reasoning") or 0),
            int(d.get("tokens_cache_read") or 0),
            int(d.get("tokens_cache_write") or 0),
        ],
    }


def _whatif_payload(app: App) -> dict:
    # The `w` picker's rows: the models you have actually used, most-used first, local
    # ones dropped (App.whatif_candidates -- they have no API rate, so substituting one
    # in would price a whole tree at $0 and call it a saving), each with its list rates
    # [in, out, cacheRead, cacheWrite] in $/M. That is everything the page needs to price
    # any node's tokens at any target's rates -- the client mirrors
    # pricing.api_equivalent_cost over the nodes' `tok` splits.
    return {
        "models": [
            {
                "model": name,
                "tokens": int(tokens),
                "price": [round(float(v), 6) for v in model_price(name)],
            }
            for name, tokens in app.whatif_candidates()
        ]
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
        "status": getattr(e, "status", ""),
    }


def _catalog_entry(e) -> dict:
    # One models.dev-view row, slimmed: ~4.6k catalog rows ride in *every* payload,
    # so only what the page can't derive travels -- m(odel), r(oute), p(rice); the
    # eff blend and the ~ approx flag are pure functions of price + mix, recomputed
    # client-side. u(se share) only when you've actually used the model, s(tatus)
    # only when models.dev flags a lifecycle stage.
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
    # The P overlay's data: every priced model you've used, as two row sets --
    # per-canonical-model (flat/by-vendor views) and per-(route, model) (by-provider
    # view) -- plus the whole models.dev catalog (slim rows, the "all" view) and the
    # app-wide token mix behind the eff $/M blend. Reuses the App's own
    # priced_model_entries so the numbers match the TUI exactly; local models are
    # dropped upstream (no API rate). App-wide, never range-scoped (like the TUI).
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
    # Seed the page's pin set with the TUI's (canonical ids); the page then keeps
    # its own copy in localStorage, so the two frontends start aligned but a
    # browser-side pin never has to write back into state.json.
    if app.pinned_models:
        out["pinned"] = sorted(app.pinned_models)
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
        # Nodes travel for EVERY session, not just the ones with a subagent tree: the
        # `w` what-if's two views (a session's Subagents tree and its Overview summary)
        # must read one source, and the Overview has to answer for a *solo* session --
        # the one case with no tree to table, and the reason the summary exists at all.
        # Every backend's workflow_nodes returns at least the root row, so a solo
        # session ships exactly that (the same rows, and therefore the same figures, the
        # TUI's whatif_session_totals reads). The alternative -- reusing models[w.id] --
        # was measured against real data and drifts: message-level model rows reprice
        # partially-priced/multi-model sessions differently from the session-level node,
        # so the page would quote what-ifs the TUI never shows. The cost is one cheap
        # per-session query per solo session at export time (~0.4 ms).
        # Store-level call on purpose: workflow_nodes handles demo transforms itself.
        # One malformed session must not kill the whole export.
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
        "theme": getattr(app.args, "theme", DEFAULT_THEME) or DEFAULT_THEME,
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
        "whatif": _whatif_payload(app),
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
                    "promptFull": r.get("prompt_full") or "",
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
    # The Context tab's data (the TUI's detail_context, serialized): measured
    # per-turn prompt sizes for the growth curve (main-thread turns only --
    # subagents run in their own windows) plus the estimated composition rows
    # for backends with the opt-in. The client derives peak/final/compactions
    # itself, mirroring how the prices overlay recomputes eff client-side.
    context = None
    if app.session_supports_context_curve(workflow_id):
        points = []
        windows = set()
        model = ""
        for r in app.session_turn_rows(workflow_id):
            if r.get("depth"):
                continue
            size = (r.get("input") or 0) + (r.get("cache_read") or 0) + (r.get("cache_write") or 0)
            if size <= 0:
                continue
            model = r.get("model_name") or model
            points.append({"t": (r.get("time") or "")[5:16], "v": int(size)})
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
                "window": model_context_window(model),  # the live (last) model's
                "mixedWindows": len(windows) > 1,
                "points": points,
                "comp": comp,
            }
    return {"turns": turns, "tools": tools, "context": context}


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
        pass  # a status-line poller's access log is noise

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page is fully self-contained -- inline JS/CSS, a data: favicon, and
        # fetches only back to this server -- so everything else can be denied.
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
        # DNS-rebinding defense: an attacker's domain can be pointed at 127.0.0.1
        # and read this server cross-origin, but the Host header still names that
        # domain -- so on a loopback bind only local names pass. An explicit
        # --bind beyond loopback opted out (and got the startup warning).
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
            # State-changing, so POST-only: a GET can be fired cross-origin by any
            # webpage (CSRF), and each reload re-parses the sources off disk.
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
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


class ReportServer(HTTPServer):
    """Serves the rendered browser page plus the per-session JSON extras. The page
    (payload included) is built once and cached; /api/reload re-reads the stores
    and invalidates it -- wired to the page's refresh button.

    Deliberately single-threaded: the stores forbid concurrent access to a
    connection, and every request is fast (the page is cached, the extras are the
    same cheap per-session queries the TUI runs on drill-in), so serializing a
    single local user's requests on one serve thread costs nothing. serve_command
    runs serve_forever on a background thread (all requests still one at a time on
    it) so the main thread can catch a Ctrl-C that Windows won't deliver to
    serve_forever's select()."""

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


def open_report(url: str) -> bool:
    """Open the browser page in the user's default web browser -- stdlib `webbrowser`, so it's
    cross-platform out of the box (`open` on macOS, `xdg-open` on Linux, the shell
    association on Windows). Best effort: a headless box with no browser returns
    False instead of raising, so `--web` never crashes serving over it."""
    import webbrowser

    try:
        return webbrowser.open(url, new=2)  # new=2 -> a new tab where the browser can
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
    server.page()  # build eagerly so the first request is instant and errors surface here
    host = "localhost" if bind in ("127.0.0.1", "::1") else bind
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"OpenTab browser at {url}  (Ctrl-C to stop)")
    import threading

    if getattr(args, "web", False):
        # --web: pop the browser now the socket is listening (bound in __init__, so a
        # request racing serve_forever just queues in the backlog). In a daemon thread
        # so a console-browser fallback that runs in the foreground can't block
        # serve_forever; it only calls webbrowser, never the sqlite-bound store, so
        # the single-threaded-request design still holds.
        threading.Thread(target=open_report, args=(url,), daemon=True).start()
    # Serve on a background thread and block the main thread on an interruptible join --
    # NOT serve_forever() on the main thread. On Windows a Ctrl-C never wakes the
    # select() inside serve_forever (Winsock select ignores the SIGINT event, so the
    # KeyboardInterrupt stays pending until some request happens to return control to the
    # eval loop), which left --serve/--web unkillable from the keyboard. Thread.join()
    # instead waits on a lock whose acquire IS wired to the SIGINT event on Windows, so
    # Ctrl-C interrupts it at once. Requests still run one at a time on the single serve
    # thread, so the store's no-concurrent-access invariant holds; daemon=True is a
    # backstop so the process can still exit even mid-request.
    server_thread = threading.Thread(target=server.serve_forever, name="opentab-serve", daemon=True)
    server_thread.start()
    try:
        while server_thread.is_alive():
            server_thread.join(0.5)  # interruptible by Ctrl-C on Windows, unlike select()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()  # off the serve thread, so it stops the loop without deadlock
        server.server_close()
        server_thread.join(timeout=2)
    return 0
