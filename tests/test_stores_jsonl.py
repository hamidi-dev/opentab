"""Duration/event JSONL accounting and local trace ownership."""

import json
import os
import tempfile

import opentab as ot
from opentab.api.service import OpenTabService
from opentab.stores.cached import CachedStore
from opentab.web.report import build_payload, session_extras

from tests._support import _jsonl_args, _parse, _write_jsonl


def _event(**fields):
    return {
        "record_type": "event",
        "session_id": "voice",
        "request_id": "speech",
        "model": "gpt-live-1",
        "timestamp": "2026-09-27T08:00:01Z",
        "project": "Iris Live",
        "title": "Voice test",
        "prompt": "User speech (grouped)",
        "response": "private spoken words",
        "details": {"start_ms": 1000, "end_ms": 1500},
        **fields,
    }


def _usage(seconds, status="running", **fields):
    return _event(
        record_type="usage_snapshot",
        request_id="usage",
        response="",
        timestamp=f"2026-09-27T08:01:{int(seconds) % 60:02}Z",
        prompt="Voice duration",
        duration_seconds=seconds,
        rate_per_minute=0.05,
        usage_status=status,
        **fields,
    )


def test_duration_snapshots_replace_instead_of_sum_and_preserve_events():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path,
            [
                _event(),
                _event(),
                _usage(12),
                _usage(15),
                _usage(94, "confirmed"),
                _usage(15),
                dict(_usage(0, "confirmed"), duration_seconds=float("nan")),
            ],
        )
        store = ot.JsonlStore(path, _jsonl_args())
        wf = store.workflows()[0]
        assert wf.total_cost == wf.total_tokens == 0
        assert wf.usage_seconds == 94 and wf.usage_status == "confirmed"
        assert wf.worked_seconds is None and not store.supports_context_curve(wf.id)
        model = store.model_breakdown()[0]
        assert model["runs"] == 1  # speech and lifecycle events aren't model calls
        assert abs(model["estimated_cost"] - 94 * 0.05 / 60) < 1e-9
        turns = store.message_timeline(wf.id)
        assert len(turns) == 2 and store.supports_turn_content(wf.id)
        assert "private spoken words" not in repr(store._sessions)
        key = next(row["content_key"] for row in turns if row["has_text"])
        assert "private spoken words" in json.dumps(store.turn_content(wf.id, key))
        assert store.turn_content("another-session", key) == {}
        # A rewritten row cannot reuse the address of previously selected content.
        _write_jsonl(path, [_event(response="replacement")])
        assert store.turn_content(wf.id, key) == {}


def test_duration_prices_match_tui_web_service_and_cache_without_raw_leaks():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(path, [_event(), _usage(94, "confirmed")])
        args = _parse(["--no-state", "--jsonl", path])
        store = ot.JsonlStore(path, args)
        app = ot.App(store, args)
        app._ensure_models()
        wf = app.loaded[0]
        cost = 94 * 0.05 / 60
        assert abs(wf.api_total_cost - cost) < 1e-9
        assert wf.real_total_cost == wf.real_root_cost == 0
        assert abs(wf.api_root_cost - cost) < 1e-9
        rnd = ot.Renderer(app)
        assert abs(sum(rnd.turn_costs(store.message_timeline(wf.id))) - cost) < 1e-9
        assert "94s" in "\n".join(rnd.detail_overview(wf, 100))
        app.show_api_prices = False
        app._apply_price_mode()
        assert wf.total_cost == 0 and sum(rnd.turn_costs(store.message_timeline(wf.id))) == 0
        payload = build_payload(app)
        extras = session_extras(app, wf.id)
        assert "private spoken words" not in json.dumps([payload, extras])
        assert abs(sum(t["api"] for t in extras["turns"]) - cost) < 1e-6
        assert not extras.get("curve", False)
        service = OpenTabService(store, args)
        detail = service.get_session(wf.id)
        assert abs(detail["api_equivalent_cost_usd"] - cost) < 1e-9
        assert detail["usage_seconds"] == 94
        assert (
            abs(
                sum(t["api_equivalent_cost_usd"] for t in service.session_turns(wf.id)["turns"])
                - cost
            )
            < 1e-9
        )
        cached = CachedStore(ot.JsonlStore(path, args), "jsonl|" + path, args)
        cached.workflows()
        cached.model_breakdown()
        with open(cached._path) as fh:
            saved = fh.read()
        assert "private spoken words" not in saved and "start_ms" not in saved
        warm = CachedStore(ot.JsonlStore(path, args), "jsonl|" + path, args)
        assert warm.workflows()[0].usage_seconds == 94
        assert warm.model_breakdown()[0]["estimated_cost"] == cost


def test_unconfirmed_duration_and_cost_only_rows_remain_distinct():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(path, [_usage(15, "unconfirmed"), _event(request_id="metered", cost_usd=0.2)])
        store = ot.JsonlStore(path, _jsonl_args())
        wf = store.workflows()[0]
        assert wf.usage_status == "unconfirmed" and wf.total_cost == 0.2
        row = store.model_breakdown()[0]
        assert abs(OpenTabService._api_model_cost(row) - 0.2125) < 1e-9


def test_jsonl_trace_preview_limits_and_full_read_are_scoped():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "requests.jsonl")
        _write_jsonl(
            path, [_event(response="x" * 20000), _event(session_id="other", response="foreign")]
        )
        store = ot.JsonlStore(path, _jsonl_args())
        store.workflows()
        key = store.message_timeline("voice")[0]["content_key"]
        preview = store.turn_content("voice")[key][0]
        assert preview["dropped"] > 0
        assert store.turn_content("voice", key)[key][0]["text"] == "x" * 20000
        assert "foreign" not in json.dumps(store.turn_content("voice"))
