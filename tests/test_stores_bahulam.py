import os
import tempfile

import opentab as ot

from tests._support import _jsonl_args, _write_jsonl


def _bahulam_event(event_type, data=None, *, cwd="/work/repo", ts=None):
    return {
        "type": "bahulam_event",
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


def test_bahulam_store_reads_current_event_shape_for_usage_and_tools():
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
        assert workflow.total_tokens == 1460
        assert workflow.unpriced_tokens == 0

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
        assert by_model["xiaomi/mimo-v2.5"]["input"] == 700
        assert by_model["xiaomi/mimo-v2.5"]["cache_read"] == 250
        assert by_model["xiaomi/mimo-v2.5"]["cost"] == 0.20
        assert by_model["xiaomi/mimo-v2.5"]["unpriced_input"] == 0
        assert by_model["deepseek/deepseek-v4-flash"]["reasoning"] == 3
        assert by_model["deepseek/deepseek-v4-flash"]["input"] == 300
        assert by_model["deepseek/deepseek-v4-flash"]["cost"] == 0.05


def test_bahulam_store_counts_subagents_and_splits_root_cost():
    sid = "0198fca0-34b4-7285-b3f0-3b8fb0489e62"
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(cwd, ".git"))
        rows = [
            _bahulam_user("plan this", cwd=cwd),
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
                "sub_agent_start",
                {
                    "type": "plan",
                    "model": "deepseek/deepseek-v4-pro",
                    "query": "create the implementation plan",
                    "task_id": "task-plan-1",
                },
                cwd=cwd,
            ),
            _bahulam_event(
                "sub_agent_complete",
                {
                    "type": "plan",
                    "model": "deepseek/deepseek-v4-pro",
                    "success": True,
                    "duration_s": 12.5,
                    "tool_calls": 3,
                    "task_id": "task-plan-1",
                    "usage": {
                        "model": "deepseek/deepseek-v4-pro",
                        "role": "plan",
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_tokens": 30,
                        "cache_creation_tokens": 0,
                    },
                },
                cwd=cwd,
            ),
            _bahulam_event(
                "complete",
                {
                    "usage": {
                        "total_input_tokens": 300,
                        "total_output_tokens": 30,
                        "cache_read_input_tokens": 70,
                        "total_cost_usd": 0.05,
                        "models": [
                            {
                                "model": "xiaomi/mimo-v2.5",
                                "role": "coder",
                                "input_tokens": 200,
                                "output_tokens": 10,
                                "cache_read_tokens": 40,
                                "cost_usd": 0.02,
                            },
                            {
                                "model": "deepseek/deepseek-v4-pro",
                                "role": "plan",
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "cache_read_tokens": 30,
                                "cost_usd": 0.03,
                            },
                        ],
                    },
                    "primary_tool_calls": 1,
                    "sub_agent_tool_calls": 3,
                },
                cwd=cwd,
            ),
        ]
        _write_bahulam(tmp, sid, rows)

        store = ot.BahulamStore(os.path.join(tmp, "projects"), _jsonl_args())
        workflow = store.workflows()[0]
        assert workflow.subagents == 1
        assert workflow.root_cost == 0.02
        assert workflow.total_cost == 0.05
        assert workflow.total_tokens == 400

        nodes = store.workflow_nodes(sid)
        assert len(nodes) == 2
        assert nodes[0]["depth"] == 0
        assert nodes[0]["cost"] == 0.02
        assert nodes[0]["tokens_total"] == 250
        assert nodes[1]["depth"] == 1
        assert nodes[1]["agent"] == "plan"
        assert nodes[1]["model_name"] == "deepseek/deepseek-v4-pro"
        assert nodes[1]["cost"] == 0.03
        assert nodes[1]["tokens_total"] == 150


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


def test_bahulam_missing_cost_stays_unpriced_and_bare_unknown_model_stays_bare():
    sid = "0198fca0-34b4-7285-b3f0-3b8fb0489e61"
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.path.join(tmp, "repo")
        os.makedirs(cwd)
        rows = [
            _bahulam_user("hello", cwd=cwd),
            _bahulam_event(
                "session_info",
                {"models": {"coder": "local-vision-test"}},
                cwd=cwd,
            ),
            _bahulam_event(
                "complete",
                {
                    "usage": {
                        "total_input_tokens": 10,
                        "total_output_tokens": 5,
                        "cache_read_input_tokens": 2,
                        "models": [
                            {
                                "model": "local-vision-test",
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "cache_read_tokens": 2,
                            }
                        ],
                    }
                },
                cwd=cwd,
            ),
        ]
        _write_bahulam(tmp, sid, rows)

        store = ot.BahulamStore(os.path.join(tmp, "projects"), _jsonl_args())
        workflow = store.workflows()[0]
        assert workflow.total_cost == 0
        assert workflow.total_tokens == workflow.unpriced_tokens == 17

        by_model = {row["model_name"]: row for row in store.model_breakdown()}
        assert "local-vision-test" in by_model
        assert "anthropic/local-vision-test" not in by_model
        assert by_model["local-vision-test"]["unpriced_input"] == 10
        assert by_model["local-vision-test"]["unpriced_cache_read"] == 2
