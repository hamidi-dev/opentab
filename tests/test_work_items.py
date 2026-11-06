from __future__ import annotations

import json
import multiprocessing
import os
from unittest.mock import patch

from opentab.accounting.models import SessionRef
from opentab.persistence import work_items as p
from opentab.work_items import WorkItems, WorkRoot, aggregate_work_items

from tests._support import isolated_annotations as isolated

ON = {"version": 1, "work_items": {"enabled": True}}
A = SessionRef("box", "claude-code", "same-native-id").encode()
B = SessionRef("box", "codex", "same-native-id").encode()
C = SessionRef("other-box", "claude-code", "same-native-id").encode()


def raises(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except p.WorkItemError as exc:
        assert exc.code == code, exc.code
        return exc
    raise AssertionError(code)


def raw():
    with open(p.items_path()) as fh:
        return json.load(fh)


def write(data):
    os.makedirs(os.path.dirname(p.items_path()), exist_ok=True)
    with open(p.items_path(), "w") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


def test_lifecycle_identity_atomic_moves_and_accounting():
    with isolated(ON):
        model = WorkItems()
        model.refresh()
        first = model.create("Outcome")
        second = model.create("Other", key=A)
        assert model.snapshot.keys_of(first) == ()
        model.attach(B, first, 0)
        model.attach(C, first, 0)
        raises("move_required", model.attach, A, first, 1)
        model.move(A, first, 1)
        totals = aggregate_work_items(
            [WorkRoot(A, 5, 60), WorkRoot(A, 5, 60), WorkRoot(B, 3, None)], model.snapshot
        )
        assert totals[first].linked == 3 and totals[first].available == 2
        assert totals[first].unavailable == 1 and totals[first].time_measured == 1
        assert totals[first].cost == 8 and totals[first].worked_seconds == 60
        assert totals[second].cost is None
        model.edit(first, 1, state_id="done", next_step="Verify", archived=True)
        assert model.section(first) == "archived"
        model.edit(first, 2, archived=False)
        assert model.section(first) == "done"
        model.edit(first, 3, state_id="todo")
        assert model.section(first) == "open"
        assert aggregate_work_items([], model.snapshot)[first].unavailable == 3
        model.detach(A, 2)
        raises("stale", model.attach, A, second, 2)
        assert model.snapshot.membership(A).revision == 3
        try:
            model.snapshot.items[first] = None
        except TypeError:
            pass
        else:
            raise AssertionError("mutable snapshot")


def test_preservation_unknown_state_and_stale_edits():
    with isolated(ON):
        ident, snapshot = p.create_item("Kept", "removed")
        p.set_membership(C, ident, 0)
        data = raw()
        data["future"] = [1]
        data["items"][ident]["future"] = "item"
        data["memberships"][C]["future"] = "root"
        write(data)
        model = WorkItems()
        model.refresh()
        assert model.section(ident) == "unknown"
        model.edit(ident, 1, title="Renamed")
        raises("stale", model.edit, ident, 1, title="Stale")
        model.detach(C, 1)
        result = raw()
        assert result["future"] == [1] and result["items"][ident]["future"] == "item"
        assert result["memberships"][C]["future"] == "root"
        assert snapshot.items[ident].title == "Kept"


def test_corrupt_newer_files_and_atomic_failed_create():
    with isolated(ON):
        ident, _ = p.create_item("One", "todo", key=A)
        before = raw()
        raises("move_required", p.create_item, "Two", "todo", key=A, expected=1)
        assert raw() == before
        for data, code in (
            ("{bad", "unreadable"),
            ({"version": 2}, "newer_version"),
            ({**before, "items": {ident: {"title": 1}}}, "malformed"),
            (
                {**before, "items": {ident: {**before["items"][ident], "title": "Kept\n"}}},
                "malformed",
            ),
        ):
            write(data)
            with open(p.items_path()) as fh:
                original = fh.read()
            raises(code, p.create_item, "No overwrite", "todo")
            with open(p.items_path()) as fh:
                assert fh.read() == original


def test_disabled_demo_no_state_windows_and_stale_switch():
    for config, allowed in ((None, True), (ON, False)):
        with isolated(config):
            write("{broken")
            model = WorkItems(allowed)
            with patch(
                "opentab.work_items.load_workflow_config", side_effect=AssertionError()
            ) if not allowed else patch.object(p, "load_snapshot", side_effect=AssertionError()):
                model.refresh()
            assert not model.enabled and not model.states
            raises("disabled", model.create, "No")
            assert not os.path.exists(p.items_path() + ".lock")
    with isolated(ON):
        model = WorkItems()
        model.refresh()
        with patch.object(p, "fcntl", None):
            assert not model.editable
            raises("unavailable", model.create, "Read only")
        assert not os.path.exists(p.items_path())
        from opentab.persistence.workflow_config import config_path

        with open(config_path(), "w") as fh:
            json.dump({"version": 1}, fh)
        raises("disabled", model.create, "Stale client")


def _worker(env, key, ident, ready, start, out):
    os.environ.update(env)
    ready.put(True)
    start.wait()
    try:
        p.set_membership(key, ident, 0)
        out.put("ok")
    except p.WorkItemError as exc:
        out.put(exc.code)


def _edit_worker(env, ident, title, ready, start, out):
    os.environ.update(env)
    ready.put(True)
    start.wait()
    try:
        p.edit_item(ident, 1, title=title)
        out.put("ok")
    except p.WorkItemError as exc:
        out.put(exc.code)


def test_real_process_concurrency_merge_and_conflict():
    with isolated(ON):
        ident, _ = p.create_item("Concurrent", "todo")
        env = {name: os.environ[name] for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        ctx = multiprocessing.get_context("spawn")
        for keys, expected in (((A, B), ["ok", "ok"]), ((C, C), ["ok", "stale"])):
            ready, out, start = ctx.Queue(), ctx.Queue(), ctx.Event()
            workers = [
                ctx.Process(target=_worker, args=(env, key, ident, ready, start, out))
                for key in keys
            ]
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=15)
            start.set()
            assert sorted(out.get(timeout=15) for _ in workers) == expected
            for worker in workers:
                worker.join(15)
                assert worker.exitcode == 0
        assert set(p.load_snapshot().keys_of(ident)) == {A, B, C}


def test_real_process_item_edits_merge_and_cas_conflicts():
    with isolated(ON):
        first, _ = p.create_item("First", "todo")
        second, _ = p.create_item("Second", "todo")
        third, _ = p.create_item("Race", "todo")
        env = {name: os.environ[name] for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        ctx = multiprocessing.get_context("spawn")
        for ids, expected in (((first, second), ["ok", "ok"]), ((third, third), ["ok", "stale"])):
            ready, out, start = ctx.Queue(), ctx.Queue(), ctx.Event()
            workers = [
                ctx.Process(target=_edit_worker, args=(env, ident, f"Edit {n}", ready, start, out))
                for n, ident in enumerate(ids)
            ]
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=15)
            start.set()
            assert sorted(out.get(timeout=15) for _ in workers) == expected
            for worker in workers:
                worker.join(15)
                assert worker.exitcode == 0
        snapshot = p.load_snapshot()
        assert snapshot.items[first].title == "Edit 0" and snapshot.items[second].title == "Edit 1"
        assert snapshot.items[third].revision == 2


def test_state_removal_preserves_item_and_requires_explicit_repair():
    from opentab.persistence.workflow_config import config_path

    with isolated(ON):
        model = WorkItems()
        model.refresh()
        ident = model.create("Review", "review", key=A)
        data = raw()
        with open(config_path(), "w") as fh:
            json.dump(
                {
                    "version": 1,
                    "work_items": {
                        "enabled": True,
                        "states": [{"id": "todo", "label": "Renamed open", "done": False}],
                    },
                },
                fh,
            )
        model.refresh()
        assert model.section(ident) == "unknown" and raw() == data
        raises("unknown_state", model.edit, ident, 1, state_id="review")
        model.edit(ident, 1, state_id="todo")
        assert model.section(ident) == "open"
        assert model.snapshot.keys_of(ident) == (A,)


def test_failed_replace_and_lock_preserve_disk_and_snapshot():
    with isolated(ON):
        model = WorkItems()
        model.refresh()
        ident = model.create("Original")
        before = raw()
        with patch.object(p.os, "replace", side_effect=OSError("failed")):
            raises("unwritable", model.edit, ident, 1, title="Lost")
        assert raw() == before and model.snapshot.items[ident].title == "Original"
        assert not any(
            name.startswith(".work-items-") for name in os.listdir(os.path.dirname(p.items_path()))
        )
        with patch.object(p.fcntl, "flock", side_effect=OSError("failed")):
            raises("unwritable", model.edit, ident, 1, title="Lost")
        assert raw() == before


def test_bounded_validation_and_duplicate_root_coverage():
    with isolated(ON):
        model = WorkItems()
        model.refresh()
        for bad in ("", "x" * 201, "bad\ntext", 3):
            raises("invalid", model.create, bad)
        ident = model.create("Valid", key=A)
        raises("invalid_session", model.attach, "bare-native-id", ident, 0)
        raises("invalid", model.attach, B, ident, True)
        raises("invalid", model.edit, ident, 1, archived=1)
        with patch.object(p, "MAX_ITEMS", 1):
            raises("limit", model.create, "Too many")
        totals = aggregate_work_items([WorkRoot(A, 0, 0)], model.snapshot)[ident]
        assert totals.cost == 0 and totals.worked_seconds == 0
        totals = aggregate_work_items([WorkRoot(A, 1, 0), WorkRoot(A, 2, 0)], model.snapshot)[ident]
        assert totals.available == 0 and totals.unavailable == 1 and totals.cost is None
