"""The Copilot-Chat-in-VS-Code backend (stores/vscode.py)."""

import json
import os
import tempfile

import opentab as ot

from tests._support import _write_jsonl

VSCODE_SID = "a66d5e72-2c39-48c0-8514-8eecb3cdbabc"


def _vscode_args(vscode_dir):
    return type("Args", (), {"demo": False, "vscode_dir": vscode_dir})()


def _vscode_request(
    rid="request_1",
    ts=1781122800000,
    completion=490,
    md_prompt=32543,
    md_output=60,
    resolved="claude-sonnet-4-6",
    model_id="copilot/claude-sonnet-4.6",
    message=None,
):
    # The serialized shape VS Code's chatModel.ts writes: response data (tokens, result)
    # is flattened onto the request; completionTokens is the turn total across tool-call
    # rounds while result.metadata carries the extension's single-round figures.
    return {
        "requestId": rid,
        "timestamp": ts,
        "modelId": model_id,
        "message": {"text": "fix the flaky test"} if message is None else message,
        "completionTokens": completion,
        "result": {
            "metadata": {
                "promptTokens": md_prompt,
                "outputTokens": md_output,
                "resolvedModel": resolved,
            }
        },
    }


def _vscode_user_dir(tmp, journal_entries, hash_name="h1", folder_name="myrepo", name=VSCODE_SID):
    # Build <User>/workspaceStorage/<hash>/chatSessions/<sid>.jsonl plus the
    # workspace.json that names the workspace folder (as a file:// URI).
    user = os.path.join(tmp, "Code", "User")
    hash_dir = os.path.join(user, "workspaceStorage", hash_name)
    chat = os.path.join(hash_dir, "chatSessions")
    os.makedirs(chat, exist_ok=True)
    folder = os.path.join(tmp, folder_name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(hash_dir, "workspace.json"), "w") as fh:
        json.dump({"folder": "file://" + folder}, fh)
    _write_jsonl(os.path.join(chat, f"{name}.jsonl"), journal_entries)
    return user, folder


def test_vscode_store_replays_journal_and_prefers_cumulative_output():
    with tempfile.TemporaryDirectory() as tmp:
        user, folder = _vscode_user_dir(
            tmp,
            [
                {
                    "kind": 0,
                    "v": {
                        "version": 3,
                        "sessionId": VSCODE_SID,
                        "creationDate": 1781122762688,
                        "requests": [],
                    },
                },
                {"kind": 2, "k": ["requests"], "v": [_vscode_request()]},
                # A canceled request records nothing and must not count as a run.
                {
                    "kind": 2,
                    "v": [
                        {
                            "requestId": "request_2",
                            "timestamp": 1781122900000,
                            "modelId": "copilot/gpt-4.1",
                            "message": {"text": "never mind"},
                        }
                    ],
                },
                {"kind": 1, "k": ["customTitle"], "v": "Fix the flaky test"},
            ],
        )
        store = ot.VscodeStore([user], _vscode_args(user))
        assert store.records_cost is False  # subscription-style: $0 until "$" reprices
        workflows = store.workflows()
        assert len(workflows) == 1
        w = workflows[0]
        assert w.id == VSCODE_SID
        assert w.title == "Fix the flaky test"  # customTitle wins over the first prompt
        assert w.directory == folder  # workspace.json folder URI, git-root folded
        assert w.total_cost == 0.0 and w.root_cost == 0.0
        assert w.total_tokens == 32543 + 490
        assert w.unpriced_tokens == w.total_tokens  # every token is unpriced
        rows = store.model_breakdown()
        assert len(rows) == 1
        row = rows[0]
        assert row["model_name"] == "anthropic/claude-sonnet-4-6"  # resolvedModel, prefixed
        assert row["runs"] == 1  # the canceled request did not count
        assert row["input"] == 32543  # metadata.promptTokens (the fuller figure)
        # chatModel.ts accumulates completionTokens across tool-call rounds (490);
        # metadata.outputTokens is a single round (60) and must not win.
        assert row["output"] == 490
        assert row["unpriced_input"] == 32543 and row["root_unpriced_output"] == 490
        nodes = store.workflow_nodes(VSCODE_SID)
        assert len(nodes) == 1 and nodes[0]["depth"] == 0
        assert nodes[0]["tokens_total"] == 32543 + 490


def test_vscode_store_reads_legacy_json_and_dedupes_against_journal():
    with tempfile.TemporaryDirectory() as tmp:
        user, _ = _vscode_user_dir(
            tmp,
            [
                {
                    "kind": 0,
                    "v": {
                        "version": 3,
                        "sessionId": VSCODE_SID,
                        "creationDate": 1781122762688,
                        "requests": [],
                    },
                },
                {"kind": 2, "k": ["requests"], "v": [_vscode_request()]},
            ],
        )
        chat = os.path.join(user, "workspaceStorage", "h1", "chatSessions")
        # The same session also in the pre-journal plain-JSON shape (a migrated
        # session): identical requestId -> counted once, journal first.
        with open(os.path.join(chat, f"{VSCODE_SID}.json"), "w") as fh:
            json.dump(
                {
                    "version": 3,
                    "sessionId": VSCODE_SID,
                    "creationDate": 1781122762688,
                    "requests": [_vscode_request()],
                },
                fh,
            )
        # Plus a legacy-only session: message as a plain string (the old format),
        # top-level promptTokens, bare modelId -> provider-prefixed by family.
        with open(os.path.join(chat, "22222222-2222-2222-2222-222222222222.json"), "w") as fh:
            json.dump(
                {
                    "version": 2,
                    "sessionId": "22222222-2222-2222-2222-222222222222",
                    "creationDate": 1781100000000,
                    "requests": [
                        {
                            "requestId": "request_9",
                            "timestamp": 1781100060000,
                            "modelId": "gpt-4.1",
                            "message": "explain this regex",
                            "completionTokens": 200,
                            "promptTokens": 1500,
                        }
                    ],
                },
                fh,
            )
        store = ot.VscodeStore([user], _vscode_args(user))
        workflows = {w.id: w for w in store.workflows()}
        assert len(workflows) == 2
        merged = workflows[VSCODE_SID]
        assert merged.total_tokens == 32543 + 490  # once, not twice
        legacy = workflows["22222222-2222-2222-2222-222222222222"]
        assert legacy.title == "explain this regex"  # first prompt (no customTitle)
        assert legacy.total_tokens == 1500 + 200
        legacy_rows = [r for r in store.model_breakdown() if r["root_id"] == legacy.id]
        assert legacy_rows[0]["model_name"] == "openai/gpt-4.1"


def test_vscode_store_turns_empty_window_and_source_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        user = os.path.join(tmp, "Code", "User")
        empty = os.path.join(user, "globalStorage", "emptyWindowChatSessions")
        os.makedirs(empty, exist_ok=True)
        sid = "33333333-3333-3333-3333-333333333333"
        _write_jsonl(
            os.path.join(empty, f"{sid}.jsonl"),
            [
                {
                    "kind": 0,
                    "v": {
                        "version": 3,
                        "sessionId": sid,
                        "creationDate": 1781122762688,
                        "requests": [],
                    },
                },
                {
                    "kind": 2,
                    "k": ["requests"],
                    "v": [
                        _vscode_request(
                            rid="request_a", ts=1781122800000, message={"text": "first prompt"}
                        ),
                        _vscode_request(
                            rid="request_b",
                            ts=1781126400000,
                            completion=80,
                            md_prompt=900,
                            md_output=0,
                            resolved="gpt-4.1",
                            model_id="copilot/gpt-4.1",
                            message={"text": "second prompt"},
                        ),
                    ],
                },
            ],
        )
        store = ot.VscodeStore([user], _vscode_args(user))
        w = store.workflows()[0]
        assert w.directory == "(no workspace)"  # empty-window sessions have no folder
        assert store.supports_turns(sid)
        turns = store.message_timeline(sid)
        assert [t["prompt_title"] for t in turns] == ["first prompt", "second prompt"]
        assert turns[0]["time"] < turns[1]["time"]  # chronological, never cost-sorted
        assert turns[0]["output"] == 490 and turns[1]["output"] == 80
        assert turns[0]["prompt_id"] == "request_a"  # one request == one prompt group
        assert all(t["cost"] == 0.0 for t in turns)  # nothing recorded; "$" reprices

        # Source plumbing: tokens present -> available; make_store builds the backend.
        args = type(
            "Args",
            (),
            {
                "since": None,
                "until": None,
                "days": None,
                "source": "auto",
                "db": os.path.join(tmp, "no.db"),
                "claude_dir": os.path.join(tmp, "no-claude"),
                "codex_dir": os.path.join(tmp, "no-codex"),
                "hermes_db": os.path.join(tmp, "no-hermes.db"),
                "csv": os.path.join(tmp, "no.csv"),
                "jsonl": os.path.join(tmp, "no.jsonl"),
                "vscode_dir": user,
                "demo": False,
            },
        )()
        assert ot.available_sources(args) == ["vscode"]
        built, _ = ot.sources.make_store(args, "vscode")
        assert isinstance(getattr(built, "_store", built), ot.VscodeStore)  # unwrap CachedStore

        # An opened-but-never-used chat panel (no tokens anywhere) must NOT surface
        # the source -- that is every VS Code install on earth.
        bare = os.path.join(tmp, "Bare", "User")
        chat = os.path.join(bare, "workspaceStorage", "h9", "chatSessions")
        os.makedirs(chat)
        _write_jsonl(
            os.path.join(chat, "9b593653-875b-4309-8cba-e8719e139426.jsonl"),
            [{"kind": 0, "v": {"version": 3, "sessionId": "9b593653", "requests": []}}],
        )
        args.vscode_dir = bare
        assert ot.available_sources(args) == []


def test_vscode_resolves_remote_and_windows_workspace_uris():
    # vscode-remote:// URIs (Remote-WSL / SSH / container workspaces) yield their path
    # segment; Windows file URIs keep the drive-path label when no WSL mount matches.
    to_path = ot.VscodeStore._uri_to_path
    assert to_path("vscode-remote://wsl%2BUbuntu/home/mo/proj") == "/home/mo/proj"
    assert to_path("vscode-remote://ssh-remote%2Bbox/srv/app") == "/srv/app"
    assert to_path("vscode-remote://wsl%2BUbuntu") == ""  # authority only, no path
    assert to_path("file:///c%3A/Users/nosuch-opentab/proj") == "c:/Users/nosuch-opentab/proj"
    assert to_path("untitled:Untitled-1") == ""

    # End to end: a Windows-side session store whose workspace.json points into this
    # distro via Remote-WSL resolves to the local (reachable) directory.
    with tempfile.TemporaryDirectory() as tmp:
        user = os.path.join(tmp, "Code", "User")
        hash_dir = os.path.join(user, "workspaceStorage", "hwsl")
        chat = os.path.join(hash_dir, "chatSessions")
        os.makedirs(chat)
        folder = os.path.join(tmp, "wslrepo")
        os.makedirs(folder)
        with open(os.path.join(hash_dir, "workspace.json"), "w") as fh:
            json.dump({"folder": "vscode-remote://wsl%2BUbuntu" + folder}, fh)
        _write_jsonl(
            os.path.join(chat, f"{VSCODE_SID}.jsonl"),
            [
                {"kind": 0, "v": {"version": 3, "sessionId": VSCODE_SID, "requests": []}},
                {"kind": 2, "k": ["requests"], "v": [_vscode_request()]},
            ],
        )
        store = ot.VscodeStore([user], _vscode_args(user))
        assert store.workflows()[0].directory == folder
