import os
import tempfile

import opentab as ot

from tests._support import _jsonl_args, _write_jsonl


def _bahulam_event(event_type, data=None, *, record_type="bahulam_event", cwd="/work/repo", ts=None):
    return {
        "type": record_type,
        "timestamp": ts or "2026-08-29T10:00:00.000Z",
        "cwd": cwd,
        "event": {"type": event_type, "data": data or {}},
    }


def _bahulam_user(text, *, cwd="/work/repo", ts="2026-08-29T09:59:00.000Z"):
    return {
        "type": "user",
        "timestamp": ts,
        "cwd": cwd,
        "message": {"role": "user", "content": text},
    }


def _write_bahulam(root, sid, rows):
    project = os.path.join(root, "projects", "-work-repo")
    os.makedirs(project)
    _write_jsonl(os.path.join(project, f"{sid}.jsonl"), rows)


def test_bahulam_store_reads_current_and_legacy_event_shapes_for_usage_and_tools():
    sid = "0198fca0-34b4-7285-b3f0-3b8fb0489e5f"
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(cwd, ".git"))
        rows = [
            _bahulam_user("inspect this", cwd=cwd),
            _bahulam_event(
                "session_info",
                {
                    "models": {
                        "planning": "deepseek/deepseek-v4-pro",
                        "coder": "xiaomi/mimo-v2.5",
                    }
                },
                record_type="kepler_event",
                cwd=cwd,
            ),
            _bahulam_event("tool_request", {"name": "read_file"}, cwd=cwd),
            _bahulam_event(
                "complete",
                {
                    "usage": {
                        "total_input_tokens": 1000,
                        "total_output_tokens": 100,
                        "cache_read_input_tokens": 300,
                        "cache_creation_input_tokens": 50,
                        "reasoning_tokens": 20,
                        "total_cost_usd": 0.25,
                        "models": [
                            {
                                "model": "xiaomi/mimo-v2.5",
                                "input_tokens": 700,
                                "output_tokens": 80,
                                "cache_read_tokens": 250,
                                "cache_creation_tokens": 50,
                                "reasoning_tokens": 7,
                                "cost_usd": 0.20,
                            },
                            {
                                "model": "deepseek/deepseek-v4-flash",
                                "input_tokens": 300,
                                "output_tokens": 20,
                                "cache_read_tokens": 50,
                                "reasoning_tokens": 3,
                                "cost": 0.05,
                            },
                        ],
                    }
                },
                cwd=cwd,
            ),
        ]
        _write_bahulam(tmp, sid, rows)

        store = ot.BahulamStore(os.path.join(tmp, "projects"), _jsonl_args())
        workflow = store.workflows()[0]
        assert workflow.id == sid
        assert workflow.title == "inspect this"
        assert workflow.total_cost == 0.25
        assert workflow.total_tokens == 1110

        timeline = store.message_timeline(sid)
        assert len(timeline) == 1
        turn = timeline[0]
        assert turn["model_name"] == "xiaomi/mimo-v2.5"
        assert turn["tools"] == ["read_file"]
        assert turn["cost"] == 0.25
        assert turn["reasoning"] == 20

        tools = store.tool_breakdown(sid)
        assert len(tools) == 1
        assert tools[0]["tool"] == "read_file"
        assert tools[0]["model_name"] == "xiaomi/mimo-v2.5"
        assert tools[0]["calls"] == 1
        assert tools[0]["tokens_total"] == 1120

        by_model = {row["model_name"]: row for row in store.model_breakdown()}
        assert by_model["xiaomi/mimo-v2.5"]["reasoning"] == 7
        assert by_model["xiaomi/mimo-v2.5"]["cost"] == 0.20
        assert by_model["deepseek/deepseek-v4-flash"]["reasoning"] == 3
        assert by_model["deepseek/deepseek-v4-flash"]["cost"] == 0.05


def test_bahulam_store_uses_coder_model_when_usage_has_no_model_breakdown():
    sid = "0198fca0-34b4-7285-b3f0-3b8fb0489e60"
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _bahulam_user("hello", cwd=cwd),
            _bahulam_event(
                "session_info",
                {
                    "models": {
                        "planning": "deepseek/deepseek-v4-pro",
                        "coder": "xiaomi/mimo-v2.5",
                    }
                },
                cwd=cwd,
            ),
            _bahulam_event(
                "complete",
                {
                    "usage": {
                        "total_input_tokens": 100,
                        "total_output_tokens": 40,
                        "cache_read_input_tokens": 10,
                        "reasoning_tokens": 5,
                        "total_cost_usd": 0.03,
                    }
                },
                cwd=cwd,
            ),
        ]
        _write_bahulam(tmp, sid, rows)

        store = ot.BahulamStore(os.path.join(tmp, "projects"), _jsonl_args())
        timeline = store.message_timeline(sid)
        assert timeline[0]["model_name"] == "xiaomi/mimo-v2.5"

        by_model = {row["model_name"]: row for row in store.model_breakdown()}
        assert by_model["xiaomi/mimo-v2.5"]["runs"] == 1
        assert by_model["xiaomi/mimo-v2.5"]["cost"] == 0.03
        assert by_model["xiaomi/mimo-v2.5"]["tokens_total"] == 145
        assert by_model["deepseek/deepseek-v4-pro"]["runs"] == 0
