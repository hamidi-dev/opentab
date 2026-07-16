"""Saved prefs: what persists to state.json, and how it is restored (state.py)."""

import os
import tempfile

import opentab as ot

from tests._support import _claude_msg, _price_sort_app, _usage, _write_jsonl, app_with, workflow


def test_prices_sort_is_persisted_in_state():
    app = _price_sort_app()
    app.prices_sort, app.prices_sort_reverse = "cache_write", True
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = _price_sort_app()
            assert restored.prices_sort == "eff"  # fresh app starts on the eff default
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
    assert restored.prices_sort == "cache_write" and restored.prices_sort_reverse


def test_zoom_maximized_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.zoom_maximized = True
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert not restored.zoom_maximized  # the split is the fresh default
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
    assert restored.zoom_maximized


def test_ignored_projects_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/repo/a")])
    app.ignored_projects = {"/repo/a", "/repo/b"}
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00", directory="/repo/a")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.ignored_projects == {"/repo/a", "/repo/b"}
    assert restored.all_workflows == []


def test_ignored_sessions_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.ignored_sessions = {"a", "missing"}
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.ignored_sessions == {"a", "missing"}
    assert restored.all_workflows == []


def test_bookmarks_are_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.bookmarks = {"a", "gone-session"}  # a stale id survives too (source may return)
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.bookmarks == {"a", "gone-session"}
    assert not restored.show_bookmarks_only  # the B view itself always starts off


def test_what_if_price_view_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.show_api_prices = True
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert not restored.show_api_prices
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.show_api_prices


def test_calendar_granularity_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.cal_levels = ot.HEAT_MAX_LEVELS
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            assert restored.cal_levels == ot.HEAT_DEFAULT_LEVELS  # the default until restored
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    assert restored.cal_levels == ot.HEAT_MAX_LEVELS


def test_source_is_persisted_and_restored():
    with tempfile.TemporaryDirectory() as tmp:
        # make both sources "present" so the cycle is opencode / claude / all
        db = os.path.join(tmp, "opencode.db")
        open(db, "w").close()
        cdir = os.path.join(tmp, "projects", "slug")
        os.makedirs(cdir)
        _write_jsonl(
            os.path.join(cdir, "s.jsonl"),
            [_claude_msg("s", "claude-opus-4-8", _usage(1, 1, 0, 0), uuid="u", cwd=tmp)],
        )
        args = type(
            "Args",
            (),
            {
                "source": "auto",
                "db": db,
                "claude_dir": os.path.join(tmp, "projects"),
                "demo": False,
            },
        )()
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            app = app_with([workflow("a", "2026-06-01 12:00:00")])
            app.source_key = "all"
            ot.save_state(app)
            state = ot.load_state()
            assert state["source"] == "all"
            # auto restores the saved source when it's still available
            assert ot.resolve_source(args, state) == "all"
            # an explicit --source overrides the saved one
            args.source = "claude"
            assert ot.resolve_source(args, state) == "claude"
            # a saved source that's no longer available falls back to the default, which
            # merges every present source so you never need --source to see them together
            args.source = "auto"
            assert ot.resolve_source(args, {"source": "bogus"}) == "all"
            # demo merges too, and `c` can reach the merged view in demo
            args.demo = True
            assert "all" in ot.sources.source_cycle(args)
            assert ot.resolve_source(args, {}) == "all"
            args.demo = False
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_legacy_subagent_sort_state_routes_home_and_direction_stays_safe():
    # A pre-split state.json could hold a subagent-only key in sort_by (the lists
    # used to share it); it must land on subagent_sort_by, and the saved direction
    # must not flip the cost fallback (sessions would start cheapest-first).
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    ot.apply_state(app, app.args, {"sort_by": "depth", "sort_reverse": True})
    assert app.sort_by == "cost" and app.sort_reverse is False
    assert app.subagent_sort_by == "depth"


def test_subagent_sort_is_persisted_in_state():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    app.subagent_sort_by, app.subagent_sort_reverse = "agent", True
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            ot.save_state(app)
            restored = app_with([workflow("a", "2026-06-01 12:00:00")])
            ot.apply_state(restored, restored.args, ot.load_state())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg
    assert restored.subagent_sort_by == "agent"
    assert restored.subagent_sort_reverse is True
