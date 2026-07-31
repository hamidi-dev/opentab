"""The configurable key bindings (tui/bindings.py): parsing, composition, dispatch."""

import os
import tempfile

import opentab as ot
from opentab.tui import bindings

from tests._support import app_with, workflow

# --- key-spec parsing ---------------------------------------------------------------


def test_parse_key_reads_every_spelling():
    assert bindings.parse_key("j") == (ord("j"),)
    assert bindings.parse_key("S") == (ord("S"),)  # case-sensitive: shift-s
    assert bindings.parse_key("$") == (ord("$"),)
    assert set(bindings.parse_key("enter")) == {10, 13, ot.curses.KEY_ENTER}
    assert bindings.parse_key("Esc") == (27,)  # names are case-insensitive
    assert bindings.parse_key("escape") == (27,)
    assert bindings.parse_key("space") == (32,)
    assert bindings.parse_key("tab") == (9,)
    assert bindings.parse_key("shift-tab") == (ot.curses.KEY_BTAB,)
    assert bindings.parse_key("backtab") == (ot.curses.KEY_BTAB,)
    assert set(bindings.parse_key("backspace")) == {ot.curses.KEY_BACKSPACE, 127, 8}
    assert bindings.parse_key("pgdn") == (ot.curses.KEY_NPAGE,)
    assert bindings.parse_key("pagedown") == (ot.curses.KEY_NPAGE,)
    assert bindings.parse_key("f5") == (ot.curses.KEY_F0 + 5,)
    assert bindings.parse_key("ctrl-u") == (21,)
    assert bindings.parse_key("^U") == (21,)
    assert bindings.parse_key("comma") == (44,)
    assert bindings.parse_key("ö") == ("ö",)  # non-ASCII arrives as the char itself


def test_parse_key_rejects_what_it_cannot_read():
    for bad in ("", "ctrl-", "ctrl-uu", "ctrl-1", "banana", "  "):
        try:
            bindings.parse_key(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_key accepted {bad!r}")
    # Ctrl-C is the hardwired panic quit: never bindable, and the error says why.
    try:
        bindings.parse_key("ctrl-c")
    except ValueError as exc:
        assert "quit" in str(exc)
    else:
        raise AssertionError("ctrl-c parsed")


def test_pretty_key_is_the_display_form():
    assert bindings.pretty_key("enter") == "Enter"
    assert bindings.pretty_key("esc") == "Esc"
    assert bindings.pretty_key("shift-tab") == "S-Tab"
    assert bindings.pretty_key("ctrl-u") == "^U"
    assert bindings.pretty_key("down") == "↓"
    assert bindings.pretty_key("f5") == "F5"
    assert bindings.pretty_key("j") == "j"
    assert bindings.pretty_key("comma") == ","


# --- the default table --------------------------------------------------------------


def test_defaults_compose_without_warnings_and_dispatch_the_classics():
    km = bindings.Keymap()
    assert km.warnings == []
    assert km.action("main", ord("q")) == "quit"
    assert km.action("main", ord("j")) == "down"
    assert km.action("main", ot.curses.KEY_DOWN) == "down"
    assert km.action("main", 10) == "select"
    assert km.action("main", 27) == "back"
    assert km.action("main", ord("K")) == "edit_keymap"
    assert km.action("trends", ord("[")) == "older"
    # The sub-context deliberately shadows the family keys: a focused chart's ←
    # is the cursor, not the tab switch it inherits from [trends]...
    assert km.action("trends.chart", ot.curses.KEY_LEFT) == "cursor_left"
    assert km.action("trends", ot.curses.KEY_LEFT) == "tab_prev"
    # ...while everything the chart doesn't bind falls through to [trends].
    assert km.action("trends.chart", ord("j")) == "down"
    assert km.action("trends.chart", ord("q")) == "close"
    assert km.action("prices.sessions", ord("q")) == "close"
    assert km.action("menu.sort", ord("s")) == "advance"
    assert km.action("menu.sort", ord("j")) == "down"  # from [menu]
    assert km.action("main", ord("ü")) is None  # unbound, int or str alike


def test_every_default_conf_roundtrip_is_silent_and_identical():
    # ensure_user_keymap writes the generated file; loading it back must change
    # nothing and warn about nothing -- the shipped defaults are self-consistent.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keymap.conf")
        assert bindings.ensure_user_keymap(path) == path
        assert os.path.exists(path)
        km = bindings.load_user_keymap(path)
        assert km.warnings == []
        pristine = bindings.Keymap()
        for ctx in bindings.BY_NAME:
            assert km._table[ctx] == pristine._table[ctx], ctx
        # Never overwrite an edited file: a second ensure is a no-op.
        with open(path, "a") as fh:
            fh.write("\n[main]\nquit = Q\n")
        bindings.ensure_user_keymap(path)
        with open(path) as fh:
            assert "quit = Q" in fh.read()


def test_shipped_data_file_matches_the_generator():
    # src/opentab/data/keymap.conf is the visible copy of default_conf_text(); a
    # registry change must regenerate it (scripts in the repo do; this test insists).
    shipped = os.path.join(os.path.dirname(ot.__file__), "data", "keymap.conf")
    with open(shipped, encoding="utf-8") as fh:
        assert fh.read() == bindings.default_conf_text()


# --- user overrides -----------------------------------------------------------------


def _load(text: str) -> bindings.Keymap:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keymap.conf")
        with open(path, "w") as fh:
            fh.write(text)
        return bindings.load_user_keymap(path)


def test_a_remap_takes_the_key_away_from_its_default_owner():
    km = _load("[main]\ndown = x, j\n")
    assert km.action("main", ord("x")) == "down"
    assert km.action("main", ord("j")) == "down"
    # x used to clear the filter; that action is now unreachable and the load says so.
    assert any("clear_filter" in w for w in km.warnings)


def test_an_empty_value_unbinds_deliberately_and_silently():
    km = _load("[main]\nexport =\n")
    assert km.action("main", ord("e")) is None
    assert km.warnings == []
    assert km.labels("main", "export") == []


def test_a_family_override_reaches_every_picker():
    km = _load("[menu]\ndown = n\n")
    for menu in ("menu.sort", "menu.source", "menu.machine", "menu.theme", "menu.launch"):
        assert km.action(menu, ord("n")) == "down", menu
        assert km.action(menu, ord("j")) is None
    # ...unless one picker insists otherwise.
    km = _load("[menu]\ndown = n\n[menu.sort]\ndown = j\n")
    assert km.action("menu.sort", ord("j")) == "down"
    assert km.action("menu.sort", ord("n")) is None
    assert km.action("menu.source", ord("n")) == "down"


def test_a_subcontext_line_overrides_an_inherited_action():
    km = _load("[trends.drill]\nclose = X\n")
    assert km.action("trends.drill", ord("X")) == "close"
    assert km.action("trends.drill", ord("q")) is None  # its close is X now
    assert km.action("trends", ord("q")) == "close"  # the family root keeps q


def test_conflicts_warn_and_the_load_never_breaks():
    km = _load("[main]\nquit = Q\nhelp = Q\n")
    assert km.action("main", ord("Q")) in ("quit", "help")
    assert any("bound to both" in w for w in km.warnings)
    km = _load("[main]\nquit = banana\n")
    assert any("banana" in w for w in km.warnings)
    assert km.action("main", ord("q")) == "quit"  # all tokens bad -> default stands
    km = _load("[nonsense]\nquit = x\n[main]\nfrobnicate = y\n")
    assert any("unknown section" in w for w in km.warnings)
    assert any("unknown action" in w for w in km.warnings)
    km = _load("not an ini file at all [\n")
    assert km.action("main", ord("q")) == "quit"
    assert km.warnings  # unreadable -> defaults + a warning, never a crash


def test_a_missing_file_is_pure_defaults():
    km = bindings.load_user_keymap(os.path.join(tempfile.gettempdir(), "nope-keymap.conf"))
    assert km.warnings == []
    assert km.action("main", ord("q")) == "quit"


def test_non_ascii_keys_are_bindable():
    km = _load("[main]\nquit = ö\n")
    assert km.action("main", "ö") == "quit"
    assert km.action("main", ord("q")) is None
    assert km.label("main", "quit") == "ö"


# --- labels -------------------------------------------------------------------------


def test_labels_never_advertise_a_stolen_key():
    # Bind x to down and clear_filter's default x is gone from dispatch -- so it
    # must be gone from the labels too, or the help would advertise a key that
    # moves the cursor instead.
    km = _load("[main]\ndown = x\n")
    assert km.action("main", ord("x")) == "down"
    assert km.labels("main", "clear_filter") == []
    assert km.labels("main", "down") == ["x"]
    # A multi-code spec survives as long as ANY of its codes still dispatches.
    km = bindings.Keymap()
    assert "Enter" in km.labels("main", "select")
    # The inherited [trends] down keeps j on a focused chart but loses the arrow
    # to cursor_down -- the chart's label list must say j, not ↓.
    assert km.labels("trends.chart", "down") == ["j"]
    assert km.labels("trends.chart", "cursor_down") == ["↓"]


def test_footer_drops_the_chip_of_an_unbound_action():
    # The ? overlay drops an unbound entry; the footer must agree instead of
    # offering a bare word nobody can press.
    app = _app_with_keymap("[main]\nquit =\n")
    app.can_switch_source = lambda: False  # the bare test Args carries no source flags
    parts = ot.keymap.footer_parts(app)
    texts = ["".join(seg for seg, _on in part) for part in parts]
    assert not any(t.strip() == "quit" for t in texts)
    assert not any(t.endswith(" quit") for t in texts)


def test_labels_follow_the_effective_binding():
    km = bindings.Keymap()
    assert km.label("main", "select") == "Enter"
    assert km.labels("main", "filter") == ["f", "/"]
    assert km.label("main", "cycle_panel_back") == "S-Tab"
    assert km.chip("main", "mode_time", "mode_projects") == "t,p"
    km = _load("[main]\nsort = o\n")
    assert km.labels("main", "sort") == ["o"]
    # An action the section never names keeps its inherited label.
    km = _load("[menu]\ndown = n\n")
    assert km.label("menu.sort", "down") == "n"


# --- dispatch through the App -------------------------------------------------------


def _app_with_keymap(text: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keymap.conf")
        with open(path, "w") as fh:
            fh.write(text)
        km = bindings.load_user_keymap(path)
    app = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x")])
    app.keymap = km
    return app


def test_handle_key_obeys_a_remap_end_to_end():
    app = _app_with_keymap("[main]\nquit = Q\ntrends = F5\n")
    assert app.handle_key(None, ord("q")) is True  # q is free now: swallowed
    assert not app.trends
    assert app.handle_key(None, ot.curses.KEY_F0 + 5) is True
    assert app.trends  # F5 opened Trends
    app.trends = False
    assert app.handle_key(None, ord("Q")) is False  # Q quits


def test_ctrl_c_still_quits_with_a_hostile_keymap():
    # Even a file that rebinds everything can't take the panic quit away.
    app = _app_with_keymap("[main]\nquit = Q\n")
    assert app.handle_key(None, 3) is False
    app.help = True
    assert app.handle_key(None, 3) is False
    app.help = False
    app.trends = True
    assert app.handle_key(None, 3) is False


def test_remapped_help_overlay_keys_scroll_and_close():
    app = _app_with_keymap("[help]\ndown = n\nclose = x\n")
    app.handle_key(None, ord("?"))
    assert app.help
    app.handle_key(None, ord("n"))
    assert app.help_scroll == 1
    app.handle_key(None, ord("j"))  # j is unbound here now: swallowed
    assert app.help_scroll == 1 and app.help
    app.handle_key(None, ord("x"))
    assert not app.help


def test_the_filter_line_still_types_a_remapped_free_key():
    # Rebinding main keys never leaks into the filter: typing stays typing.
    app = _app_with_keymap("[main]\nquit = x\n")
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    app.filter_active = True
    app._filter_before = ""
    app.handle_key(None, ord("x"))  # x types into the query, not quit
    assert app.query == "x"
    app.handle_key(None, "ä")  # non-ASCII arrives as a str and types too
    assert app.query.endswith("ä")
    assert app.handle_key(None, 3) is False  # Ctrl-C still quits from the filter


def test_prompt_step_reads_the_input_context():
    km = bindings.Keymap()
    value, done, cancelled = ot.App.filter_prompt_step("abc", 27, 20, km)
    assert cancelled and value == "abc"
    value, done, cancelled = ot.App.filter_prompt_step("abc", 23, 20, km)  # Ctrl-W
    assert value == ""
    remap = _load("[input]\nkill_word = ctrl-g\n")
    value, _done, _c = ot.App.filter_prompt_step("two words", 7, 20, remap)  # Ctrl-G
    assert value == "two "
    value, _done, _c = ot.App.filter_prompt_step("two words", 23, 20, remap)  # Ctrl-W types nothing
    assert value == "two words"


def test_edit_keymap_headless_points_at_the_file():
    # Without a real screen there is no editor to run -- K still tells you where
    # the file lives (and installs it), instead of crashing curses-less tests.
    with tempfile.TemporaryDirectory() as tmp:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            app = app_with([workflow("a", "2026-06-01 12:00:00")])
            app.handle_key(None, ord("K"))
            assert os.path.exists(os.path.join(tmp, "opentab", "keymap.conf"))
            assert any("keymap" in t.text for t in app.toasts)
        finally:
            if xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = xdg


def test_reload_keymap_toasts_every_problem_into_the_scrollback():
    with tempfile.TemporaryDirectory() as tmp:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            os.makedirs(os.path.join(tmp, "opentab"))
            with open(os.path.join(tmp, "opentab", "keymap.conf"), "w") as fh:
                fh.write("[main]\nquit = banana\nsort = enter\n")
            app = app_with([workflow("a", "2026-06-01 12:00:00")])
            app.reload_keymap()
            # Each problem is its own line in the N scrollback, plus the summary.
            texts = [t.text for t in app.toast_log]
            assert any("banana" in t for t in texts)
            assert any("problem" in t for t in texts)
            # The valid line took effect; the broken one fell back to the default.
            assert app.keymap.action("main", 10) == "sort"
            assert app.keymap.action("main", ord("q")) == "quit"
        finally:
            if xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = xdg


def test_a_binding_for_a_retired_action_is_dropped_without_complaining():
    # keymap.conf is user-authored config, and a user who pinned a key to a command a
    # later release removed did nothing wrong. Warning them turns every launch after an
    # upgrade into a chore over a line they can only fix by deleting it -- so a RETIRED
    # action's binding is dropped in silence, while a genuine typo still warns.
    km = _load("[main]\nfold_turns = z\nbogus_action = q\n")
    assert km.warnings == ["[main] unknown action 'bogus_action' ignored"]
    # ...and the key it claimed is genuinely free again, not bound to a ghost.
    assert km.action("main", ord("z")) != "fold_turns"

    # Every retired name must be gone from the live registry: a name that is both
    # retired and current would silence a real warning for a real action.
    live = {a.name for ctx in bindings.BY_NAME.values() for a in ctx.actions}
    assert not (bindings.RETIRED_ACTIONS & live)
