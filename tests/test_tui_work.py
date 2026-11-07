import os

import opentab as ot
from opentab.tui import keymap

from tests._support import (
    FakeScreen,
    _select_session,
    _type,
    app_with,
    isolated_annotations,
    screen_text,
    workflow,
)

CTRL_N, CTRL_W, ENTER, ESC = 14, 23, 10, 27
WORK_ON = {"version": 1, "work_items": {"enabled": True}}


def _rows():
    rows = [
        workflow("a", "2026-06-01 12:00:00", title="Investigate cache", cost=5),
        workflow("b", "2026-06-01 13:00:00", title="Implement cache fix", cost=3),
        workflow("c", "2026-06-02 14:00:00", title="Unrelated", cost=1),
    ]
    rows[0].source, rows[1].source, rows[2].source = "Claude Code", "Codex", "Mystery"
    return rows


def _key(app, session_id):
    return app.session_tag_key(next(w for w in app.loaded if w.id == session_id))


def _choose(app, value):
    ws = app.work_workspace
    ws.dialog_index = [v for v, _label in ws.options()].index(value)
    app.handle_key(None, ENTER)


def _draw(app, height=24, width=80):
    screen = FakeScreen(height, width)
    real_pair = ot.curses.color_pair
    ot.curses.color_pair = lambda _n: 0
    try:
        app.renderer.draw_work(screen, height, width)
    finally:
        ot.curses.color_pair = real_pair
    return screen_text(screen)


def test_work_stays_invisible_until_enabled():
    with isolated_annotations({"version": 1, "tags": {"enabled": True}}) as tmp:
        app = _select_session(app_with(_rows()), "a")
        assert not app.work_items_active
        for key in (CTRL_W, CTRL_N, ord("A")):
            app.handle_key(None, key)
        assert app.work_workspace is None
        assert not keymap.BY_ID["work"].shown(app)
        assert not os.path.exists(os.path.join(tmp, "data", "opentab", "work-items.json"))


def test_work_item_lifecycle_from_session_to_reopen():
    with isolated_annotations(WORK_ON):
        app = _select_session(app_with(_rows()), "a")
        assert not keymap.BY_ID["work"].shown(app)  # Compatibility chord; advertised in Org mode.

        # Create from the session: the title is prefilled and the session attached on save.
        app.handle_key(None, CTRL_N)
        ws = app.work_workspace
        assert ws.dialog == "create" and ws.draft == "Investigate cache"
        assert not keymap.in_main(app)  # main chips hide while Work owns the keyboard
        app.handle_key(None, ENTER)
        item_id = ws.item_id
        assert ws.dialog is None and ws.roots() == [_key(app, "a")]

        app.handle_key(None, ord("e"))
        _type(app, "verify windows")
        app.handle_key(None, ENTER)
        assert ws.item().next_step == "verify windows"

        # Attach a second session: an explicit pick, then a confirmation.
        app.handle_key(None, ord("A"))
        _choose(app, _key(app, "b"))
        assert ws.dialog == "confirm" and ws.pending == ("attach", item_id)
        _choose(app, "confirm")
        totals = app.work_totals()[item_id]
        assert (totals.linked, totals.available, totals.cost) == (2, 2, 8)

        app.handle_key(None, ord("s"))
        _choose(app, "review")
        assert ws.item().state_id == "review"
        text = _draw(app)
        assert "REVIEW" in text and "verify windows" in text and "2 linked" in text

        # Resume never guesses between linked sessions; Codex/Claude both resumable.
        ws.root_key = None
        app.handle_key(None, ord("L"))
        assert ws.dialog == "resume" and app.launch_menu is None
        _choose(app, _key(app, "a"))
        assert app.launch_menu is not None and ws.root_key == _key(app, "a")
        app.handle_key(None, ESC)
        assert app.launch_menu is None and app.work_workspace is ws

        # Done leaves the Open list but the open detail keeps its identity.
        app.handle_key(None, ord("s"))
        _choose(app, "done")
        assert ws.item_id == item_id and ws.item().state_id == "done"
        app.handle_key(None, ESC)
        assert ws.items() == [] and "No open items" in _draw(app)
        app.handle_key(None, ord("l"))
        assert ws.section == "done" and [i.id for i in ws.items()] == [item_id]

        app.handle_key(None, ENTER)
        app.handle_key(None, ord("o"))
        assert "done" not in [v for v, _label in ws.options()]
        _choose(app, "next")
        assert app.work_items.section(item_id) == "open"
        app.handle_key(None, ESC)
        app.handle_key(None, ESC)
        assert app.work_workspace is None


def test_work_ignores_the_accounting_range_and_explains_unresumable_roots():
    with isolated_annotations(WORK_ON):
        app = _select_session(app_with(_rows()), "c")
        app.handle_key(None, CTRL_N)
        app.handle_key(None, ENTER)
        ws = app.work_workspace
        assert "no resume command" in ws.root_reason(ws.root_key)
        app.handle_key(None, ord("L"))
        assert app.launch_menu is None and "no resume command" in ws.error

        # A narrow accounting range hides the June session but not its open work.
        narrow = app_with(_rows(), days=7)
        assert "c" not in [w.id for w in narrow.all_workflows]
        narrow.handle_key(None, CTRL_W)
        items = narrow.work_workspace.items()
        assert [i.title for i in items] == ["Unrelated"]
        assert narrow.work_totals()[items[0].id].cost == 1
        assert "all dates" in _draw(narrow, 24, 80)


def test_stale_work_edit_is_refused_and_reported():
    with isolated_annotations(WORK_ON):
        first = _select_session(app_with(_rows()), "a")
        second = _select_session(app_with(_rows()), "a")
        first.handle_key(None, CTRL_W)
        first.handle_key(None, ord("c"))
        _type(first, "Release")
        first.handle_key(None, ENTER)
        second.refresh_work_items()
        second.handle_key(None, CTRL_W)
        second.handle_key(None, ENTER)  # open the only item
        second.handle_key(None, ord("t"))
        first.handle_key(None, ord("t"))
        _type(first, " v2")
        first.handle_key(None, ENTER)
        _type(second, " v3")
        second.handle_key(None, ENTER)
        assert second.work_workspace.error.startswith("Not saved")
        second.refresh_work_items()
        assert second.work_items.snapshot.items[first.work_workspace.item_id].title == "Release v2"


def test_session_rows_lead_with_the_colored_state_keyword():
    from tests._support import AttrScreen

    with isolated_annotations(WORK_ON):
        app = _select_session(app_with(_rows()), "a")
        app.handle_key(None, CTRL_N)
        app.handle_key(None, ENTER)
        app.handle_key(None, ord("s"))
        _choose(app, "next")
        app.handle_key(None, ESC)
        app.handle_key(None, ESC)
        alpha = next(w for w in app.loaded if w.id == "a")
        assert app.renderer.titled(alpha) == "NEXT Investigate cache"
        assert (
            app.renderer.titled(next(w for w in app.loaded if w.id == "b")) == "Implement cache fix"
        )
        assert any(
            "Work:     NEXT · Investigate cache" in r
            for r in app.renderer.detail_overview(alpha, 80)
        )
        # The unfocused (text-only) Sessions table remembers which line to recolour.
        lines = app.renderer.session_table(app.current_sessions(), 100)
        owned = [line for line in lines if "NEXT Investigate cache" in line]
        assert owned and app.renderer._work_lines[owned[0]].id == "a"

        # Configured colours map to their own pairs; default/white use bold ink.
        renderer = app.renderer
        renderer._state_colors_ok = True
        real_pair = ot.curses.color_pair
        ot.curses.color_pair = lambda n: n << 8
        try:
            state = app.work_items.state("next")  # cyan by default
            assert renderer.state_attr(state) == (renderer._STATE_PAIR + 5) << 8 | ot.curses.A_BOLD
            assert renderer.state_attr(app.work_items.state("todo")) == ot.curses.A_BOLD
            screen = AttrScreen(24, 100)
            content = "  2026-06-01  NEXT Investigate cache"
            renderer.paint_work_keyword(screen, 3, 0, content, alpha)
            assert (
                screen.attrs[(3, content.index("NEXT"))]
                == (renderer._STATE_PAIR + 5) << 8 | ot.curses.A_BOLD
            )
        finally:
            ot.curses.color_pair = real_pair


def test_startup_warnings_keep_work_errors_and_rank_notes_last():
    from opentab.persistence import work_items

    with isolated_annotations(WORK_ON):
        os.makedirs(os.path.dirname(work_items.items_path()), exist_ok=True)
        with open(work_items.items_path(), "w") as fh:
            fh.write("{broken")
        app = app_with(_rows())
        app.toasts.clear()
        assert not app.refresh_authored()
        assert app.toasts[-1].text.startswith("work: ")
        with open(ot.notes_path(), "w") as fh:
            fh.write("{broken")
        app.toasts.clear()
        assert not app.refresh_authored()
        assert app.toasts[-1].text.startswith("notes: ")


def test_state_colors_leave_the_button_colors_alone():
    # Both inits run every frame; sharing a slot or pair painted every overlay
    # button in the work-state red/green/yellow.
    from opentab.presentation.themes import STATE_COLOR_HEX
    from opentab.tui.renderer import Renderer

    states = len(STATE_COLOR_HEX)
    state_slots = set(range(Renderer._STATE_COLOR_BASE, Renderer._STATE_COLOR_BASE + states))
    state_pairs = set(range(Renderer._STATE_PAIR, Renderer._STATE_PAIR + states))
    button_slots = {Renderer._BUTTON_COLOR_BASE, Renderer._BUTTON_COLOR_BASE + 1}
    button_pairs = {
        Renderer._BUTTON_KEY_PAIR,
        Renderer._BUTTON_LABEL_PAIR,
        Renderer._BUTTON_PRIMARY_PAIR,
        Renderer._BUTTON_SHADOW_PAIR,
        Renderer._BUTTON_OFF_PAIR,
    }
    assert not state_slots & button_slots
    assert not state_pairs & button_pairs
