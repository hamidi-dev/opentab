import io
import os
import pickle
import shutil
import tempfile
import types
from unittest.mock import Mock, patch

import opentab as ot
from opentab import progress
from opentab.stores import loader
from opentab.stores.cached import cache_dir
from opentab.stores.combined import CombinedStore

from tests.test_stores_cached import _splice_env, _SpliceBackend


class _Probe:
    def __init__(self, size, rebuild=True):
        self.size = size
        self.rebuild = ("builder", None, "key") if rebuild else None

    def full_parse_bytes(self):
        if isinstance(self.size, Exception):
            raise self.size
        return self.size


def _build(args, key):
    # Runs in the loader process: importing tests re-isolates XDG, so restore it.
    os.environ["XDG_CACHE_HOME"] = args.cache_home
    if args.fail:
        raise RuntimeError("backend broke")
    back = _SpliceBackend(args.layout)
    if args.progress:
        progress.advance(3, 4)
    return ot.CachedStore(back, f"{key}|loader", args, persistent=not args.no_cache)


def _leaf(tmp, key, layout, **extra):
    args = types.SimpleNamespace(
        demo=False,
        no_cache=False,
        cache_home=os.environ["XDG_CACHE_HOME"],
        layout=layout,
        fail=False,
        progress=False,
    )
    vars(args).update(extra)
    back = _SpliceBackend(layout)
    back.source_name = key
    store = ot.CachedStore(back, f"{key}|loader", args, persistent=not args.no_cache)
    store.rebuild = (_build, args, key)
    return store, back


def test_loader_needs_two_cold_backends_and_keeps_the_largest_in_process():
    big = loader.MIN_PARSE_BYTES
    assert loader.heavy([_Probe(big), _Probe(0)]) == set()  # one cold backend: threads
    assert loader.heavy([_Probe(big), _Probe(big * 3), _Probe(big * 2)]) == {0, 2}
    assert loader.heavy([_Probe(big), _Probe(big, rebuild=False), _Probe(big * 2)]) == {0}
    assert loader.heavy([_Probe(big), _Probe(OSError()), _Probe(big * 2)]) == {0}
    assert loader.heavy([_Probe(big - 1), _Probe(big * 2)]) == set()
    with patch.dict(os.environ, {"OPENTAB_LOADER_PROCESSES": "0"}):
        assert loader.heavy([_Probe(big), _Probe(big * 2)]) == set()


def test_cold_backends_parse_in_a_loader_process_with_identical_rows():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            small = _splice_env(tmp, {"a.jsonl": {"s1": 100}})
            large = _splice_env(tmp, {"bb.jsonl": {"s2": 200}, "cc.jsonl": {"s3": 50}})
            left, left_back = _leaf(tmp, "left", small, progress=True)
            right, right_back = _leaf(tmp, "right", large)
            board = progress.start(_Sink())
            try:
                with patch.object(loader, "MIN_PARSE_BYTES", 1):
                    rows = CombinedStore([left, right]).workflows()
                tasks = {t.label: t for t in board._tasks}
            finally:
                progress.stop()
            assert sorted((w.id, w.total_tokens) for w in rows) == [
                ("s1", 100),
                ("s2", 200),
                ("s3", 50),
            ]
            # The smaller one parsed elsewhere; this process only read its cache.
            assert left.served_by_process is True and left_back.full_parses == 0
            assert (tasks["left"].done, tasks["left"].total) == (3, 4)  # forwarded progress
            # The largest stayed here and parsed on its thread as before.
            assert right.served_by_process is False and right_back.full_parses == 1
            assert progress.note(left) == ""  # parsed, not "cached"
            assert len(left.model_breakdown()) == 1 and left_back.full_parses == 0
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_no_cache_hands_rows_back_through_a_private_file():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            small = _splice_env(tmp, {"a.jsonl": {"s1": 100}})
            large = _splice_env(tmp, {"bb.jsonl": {"s2": 200}, "cc.jsonl": {"s3": 50}})
            left, left_back = _leaf(tmp, "left", small, no_cache=True)
            right, right_back = _leaf(tmp, "right", large, no_cache=True)
            with patch.object(loader, "MIN_PARSE_BYTES", 1):
                rows = CombinedStore([left, right]).workflows()
            assert sorted((w.id, w.total_tokens) for w in rows) == [
                ("s1", 100),
                ("s2", 200),
                ("s3", 50),
            ]
            assert left.served_by_process is True and left_back.full_parses == 0
            assert right.served_by_process is False and right_back.full_parses == 1
            assert len(left.model_breakdown()) == 1 and left_back.full_parses == 0
            # The handoff lives in a private temp directory, never the real cache.
            assert not os.path.exists(cache_dir())
            handoff = left._path
            assert handoff.startswith(loader._handoff_dir) and os.path.isfile(handoff)
            if os.name != "nt":  # Windows guards %TEMP% with ACLs, not mode bits
                assert os.stat(loader._handoff_dir).st_mode & 0o077 == 0
            # An unchanged reload reads the adopted handoff, here, without a parse.
            with patch.object(loader, "MIN_PARSE_BYTES", 1):
                CombinedStore([left, right]).workflows()
            assert left.served_from_cache is True and left_back.full_parses == 0
            # A later handoff deletes the file it replaces.
            later = loader._handoff_path("left")
            shutil.copyfile(handoff, later)
            left.load_from_disk(later)
            assert not os.path.exists(handoff) and left.workflows()[0].id == "s1"
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


def test_a_failed_loader_process_falls_back_to_parsing_here():
    with tempfile.TemporaryDirectory() as tmp:
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            small = _splice_env(tmp, {"a.jsonl": {"s1": 100}})
            large = _splice_env(tmp, {"bb.jsonl": {"s2": 200}})
            left, left_back = _leaf(tmp, "left", small, fail=True)
            right, _ = _leaf(tmp, "right", large)
            with patch.object(loader, "MIN_PARSE_BYTES", 1):
                rows = CombinedStore([left, right]).workflows()
            assert sorted(w.id for w in rows) == ["s1", "s2"]
            assert left.served_by_process is False and left_back.full_parses == 1
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg


class _Sink:
    def write(self, _text):
        pass

    def flush(self):
        pass


def test_loader_spawn_failures_leave_the_parent_to_parse():
    for error in (OSError("no interpreter"), TypeError("bad argv"), ValueError("bad fd")):
        with patch("subprocess.Popen", side_effect=error):
            assert loader._run_child(_build, None, "fixture", None) is False


def test_loader_protocol_forwards_progress_and_closes_pipes():
    for messages, success, tracked in (
        ([("progress", 2, 7), ("progress", 7, 7), ("done",)], True, True),
        ([("progress", 2, 7), ("done",)], True, False),
        ([("error", "RuntimeError")], False, True),
        ([], False, True),
    ):
        output = io.BytesIO()
        for message in messages:
            pickle.dump(message, output)
        output.seek(0)
        proc = Mock(stdin=io.BytesIO(), stdout=output)
        proc.poll.return_value = None
        task = types.SimpleNamespace(done=0, total=0) if tracked else None
        with patch("subprocess.Popen", return_value=proc):
            assert loader._run_child(_build, None, "fixture", task) is success
        assert proc.stdin.closed and proc.stdout.closed
        proc.wait.assert_called_once_with()
        assert proc.kill.call_count == (0 if success else 1)
        if tracked and success:
            assert (task.done, task.total) == (7, 7)


def test_loader_broken_input_and_corrupt_output_are_reaped():
    for broken_input in (False, True):
        stdin = Mock() if broken_input else io.BytesIO()
        if broken_input:
            stdin.write.side_effect = BrokenPipeError("child exited")
        proc = Mock(stdin=stdin, stdout=io.BytesIO(b"not a pickle"))
        proc.poll.return_value = None
        with patch("subprocess.Popen", return_value=proc):
            assert loader._run_child(_build, None, "fixture", None) is False
        proc.kill.assert_called_once_with()
        proc.wait.assert_called_once_with()
        assert proc.stdout.closed


def test_loader_does_not_kill_a_child_that_has_already_exited():
    proc = Mock(stdin=io.BytesIO(), stdout=io.BytesIO())
    proc.poll.return_value = 1
    with patch("subprocess.Popen", return_value=proc):
        assert loader._run_child(_build, None, "fixture", None) is False
    proc.kill.assert_not_called()
    proc.wait.assert_called_once_with()


def test_loader_handoff_creation_failure_never_spawns_or_adopts():
    store = Mock(persistent=False, rebuild=(_build, None, "fixture"))
    with patch.object(loader, "_handoff_path", side_effect=OSError("disk full")), patch.object(
        loader, "_run_child"
    ) as child:
        assert loader.warm(store) is False
    child.assert_not_called()
    store.load_from_disk.assert_not_called()


def test_loader_child_protocol_redirects_backend_prints_and_sends_terminal_status():
    class Capture(io.BytesIO):
        def close(self):
            self.saved = self.getvalue()
            super().close()

    for tracked, handoff, failed in (
        (False, None, False),
        (True, "/handoff", False),
        (True, None, True),
    ):
        store = Mock()
        store.writing_to.return_value = store
        if failed:
            store.workflows.side_effect = RuntimeError("private backend error")
        builder = Mock(return_value=store)
        args = types.SimpleNamespace(fixture=True)
        payload = pickle.dumps(("builder", args, "fixture", tracked, handoff))
        output = Capture()
        event = Mock()
        event.wait.side_effect = [False, False, True]

        def make_thread(*, target, **kwargs):
            return Mock(start=target, join=Mock())

        with patch.object(
            loader.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(payload))
        ), patch.object(loader.sys, "stdout", io.StringIO()), patch.object(
            loader.sys, "stderr", io.StringIO()
        ), patch.object(loader.os, "dup", return_value=99), patch.object(
            loader.os, "dup2"
        ) as redirect, patch.object(loader.os, "fdopen", return_value=output), patch.object(
            loader.threading, "Event", return_value=event
        ), patch.object(loader.threading, "Thread", side_effect=make_thread), patch.object(
            loader.progress, "track"
        ) as track, patch.object(
            pickle,
            "load",
            return_value=(builder, args, "fixture", tracked, handoff),
        ):
            loader._child_main()
        redirect.assert_called_once_with(2, 1)
        builder.assert_called_once_with(args, "fixture")
        assert track.call_count == int(tracked)
        if handoff is None:
            store.writing_to.assert_not_called()
        else:
            store.writing_to.assert_called_once_with(handoff)
        assert store.model_breakdown.call_count == int(not failed)
        messages = io.BytesIO(output.saved)
        received = []
        while messages.tell() < len(output.saved):
            received.append(pickle.load(messages))
        assert received == [
            ("progress", 0, None),
            ("progress", 0, None),
            ("error", "RuntimeError") if failed else ("done",),
        ]
        assert b"private backend error" not in output.saved
        assert output.closed
