"""Per-session notes: their own file, locked read-modify-write, never clobbered (notes.py)."""

import json
import os

import opentab as ot

from tests._support import FakeScreen, _app_on_session, app_with, workflow


def test_note_saves_to_its_own_file_and_survives_a_restart():
    # A note is the one thing opentab persists that it cannot rebuild, so it gets
    # its own file (not state.json) and is written on the edit, not at quit.
    app = _app_on_session(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1),
            workflow("b", "2026-06-01 13:00:00", cost=5),
        ],
        "b",
    )
    session = app.current_session()
    app.set_note(session, "  ran the migration twice — my fault, not the model's  ")

    # Stripped, in memory, and on disk under its session id.
    assert app.note_for("b") == "ran the migration twice — my fault, not the model's"
    assert "note saved" in app.notice
    assert ot.load_notes() == {"b": "ran the migration twice — my fault, not the model's"}
    assert os.path.basename(ot.notes_path()) == "notes.json"

    # A second edit updates rather than duplicates; an empty note clears it.
    app.set_note(session, "worth it")
    assert ot.load_notes() == {"b": "worth it"}
    assert "note updated" in app.notice
    app.set_note(session, "   ")
    assert app.note_for("b") == ""
    assert ot.load_notes() == {}
    assert "note cleared" in app.notice


def test_note_keeps_an_orphaned_entry():
    # Ids vanish for boring reasons (a rotated transcript, a source not merged into
    # this run). Neither is a reason to delete what the user wrote, so a note whose
    # session isn't in view survives the next save.
    assert ot.save_notes({"gone": "billed to the Filou job", "a": "keep"})
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.notes = ot.load_notes()
    app.set_note(app.current_session(), "still keep")
    assert ot.load_notes() == {"gone": "billed to the Filou job", "a": "still keep"}
    ot.save_notes({})


def test_note_needs_a_selected_session_and_stays_out_of_demo():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])
    # Browsing the time panels selects no session, so `n` explains itself rather
    # than opening a prompt against nothing (and never reaches curses -- stdscr=None).
    assert app.handle_key(None, ord("n"))
    assert "select a session" in app.notice
    assert app.notes == {}

    # --no-state turns notes off for the run; the key refuses outright.
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.notes_enabled = False
    assert not app.allow_notes
    assert app.handle_key(None, ord("n"))
    assert "demo" in app.notice  # "notes are off in demo / --no-state"
    assert app.notes == {}


def test_second_opentab_does_not_erase_the_first_ones_notes():
    # Two opentabs open at once is a normal thing. Each holds the whole map in memory,
    # so a naive "write my map" save lets the second one silently delete every note the
    # first one made. Each edit re-reads the file and merges into it instead.
    a = _app_on_session([workflow("s1", "2026-06-01 12:00:00")], "s1")
    b = _app_on_session([workflow("s1", "2026-06-01 12:00:00")], "s1")
    assert a.notes == b.notes == {}  # both started before either wrote anything

    a.set_note(a.current_session(), "written in the first window")
    b.set_note(b.current_session(), "written in the second window")

    on_disk = ot.load_notes()
    assert on_disk["s1"] == "written in the second window"  # last write wins the key...
    assert b.notes == on_disk  # ... and the writer adopts the merged truth
    ot.save_notes({})

    # The same holds across different sessions: B must not drop A's note.
    a = _app_on_session([workflow("s1", "2026-06-01 12:00:00")], "s1")
    b = _app_on_session([workflow("s2", "2026-06-01 13:00:00")], "s2")
    a.set_note(a.current_session(), "note A")
    b.set_note(b.current_session(), "note B")
    assert ot.load_notes() == {"s1": "note A", "s2": "note B"}
    ot.save_notes({})


def test_concurrent_note_writers_do_not_lose_an_update():
    # The merge alone only fixes the SLOW collision (two windows, minutes apart). Two
    # processes writing at the same instant would still interleave read-modify-write and
    # drop one note, so the whole cycle takes a lock. Fork 8 writers at once, each adding
    # its own note: all 8 must survive.
    ot.save_notes({})
    kids = []
    for i in range(8):
        pid = os.fork()
        if pid == 0:  # child: write one note, then leave without touching the harness
            try:
                ot.update_note(f"s{i}", f"note {i}")
            finally:
                os._exit(0)
        kids.append(pid)
    for pid in kids:
        os.waitpid(pid, 0)

    assert ot.load_notes() == {f"s{i}": f"note {i}" for i in range(8)}
    ot.save_notes({})


def test_note_save_keeps_entries_it_does_not_understand():
    # A hand-edited (or newer-opentab) entry this version can't display must survive the
    # next save. Dropping what we don't understand from a file of authored data is still
    # deleting someone's writing — and "falsy" is not a licence to delete either, so the
    # null/0/{} entries below have to come back too. Only "" goes, because an empty note
    # is exactly what clearing one leaves behind.
    foreign = {"weird": {"shape": "from v2"}, "nulled": None, "zero": 0, "empty": ""}
    with open(ot.notes_path(), "w") as fh:
        json.dump({"version": 1, "notes": dict(foreign, a="mine")}, fh)
    assert ot.load_notes() == {"a": "mine"}  # the UI shows only what it can render

    app = _app_on_session([workflow("b", "2026-06-01 12:00:00")], "b")
    app.refresh_notes()
    app.set_note(app.current_session(), "new note")

    with open(ot.notes_path()) as fh:
        stored = json.load(fh)["notes"]
    assert stored == {
        "a": "mine",
        "b": "new note",
        "weird": {"shape": "from v2"},
        "nulled": None,
        "zero": 0,
    }
    assert app.notes == {"a": "mine", "b": "new note"}  # ... but memory stays displayable
    os.unlink(ot.notes_path())


def test_note_save_fails_cleanly_on_content_json_cannot_write():
    # JSON will happily LOAD an escaped lone surrogate that a UTF-8 stream then cannot
    # write. The save must fail as a save (the caller says so), leave the existing file
    # intact, and not litter a half-written temp file next to it.
    with open(ot.notes_path(), "w") as fh:
        fh.write('{"version": 1, "notes": {"a": "\\ud800"}}')  # valid JSON, unwritable text

    app = _app_on_session([workflow("b", "2026-06-01 12:00:00")], "b")
    app.set_note(app.current_session(), "new note")

    assert "not saved" in app.notice
    assert app.notes == {}  # nothing pretends to be saved
    with open(ot.notes_path()) as fh:
        assert fh.read() == '{"version": 1, "notes": {"a": "\\ud800"}}'  # left alone
    stray = [n for n in os.listdir(os.path.dirname(ot.notes_path())) if n.endswith(".tmp")]
    assert stray == []
    os.unlink(ot.notes_path())


def test_reload_with_a_broken_file_keeps_the_loaded_notes():
    # A broken notes.json must not make the loaded notes LOOK deleted: blanking the ✎
    # marks on reload is indistinguishable from having lost them.
    assert ot.save_notes({"a": "still here"})
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.refresh_notes()
    assert app.note_for("a") == "still here"

    with open(ot.notes_path(), "w") as fh:
        fh.write("{ truncated")
    app.reload()

    assert app.note_for("a") == "still here"  # kept in memory, marks still painted
    # ... and it says so: the warning rides alongside reload's own "reloaded" toast.
    assert any("unreadable" in toast.text for toast in app.toasts)
    assert any(toast.kind == "error" for toast in app.toasts)
    os.unlink(ot.notes_path())


def test_unreadable_notes_file_is_never_overwritten():
    # A corrupt (or unreadable) notes.json used to read as "{}", and the next `n` would
    # replace a file full of notes with a file holding exactly one. Refuse instead, and
    # say which file is in the way — losing a written note is the worst bug here.
    with open(ot.notes_path(), "w") as fh:
        fh.write('{"notes": {"a": "precious"')  # truncated mid-write
    notes, readable = ot.read_notes()
    assert (notes, readable) == ({}, False)

    app = _app_on_session([workflow("b", "2026-06-01 12:00:00")], "b")
    app.set_note(app.current_session(), "new note")

    assert app.notes == {}  # nothing pretends to be saved
    assert "unreadable" in app.notice
    with open(ot.notes_path()) as fh:
        assert fh.read() == '{"notes": {"a": "precious"'  # the broken file is left alone
    os.unlink(ot.notes_path())


def test_note_prompt_and_overview_measure_wide_characters_in_cells():
    # A note can hold CJK or an emoji, each two terminal cells wide. Measuring the field
    # (or wrapping the Overview) in codepoints overflows the line: the text runs under
    # the hint, the cursor lands mid-glyph, and the pane clips half of every wrapped
    # line away. Everything here counts cells. (The layout is asserted directly —
    # FakeScreen indexes by codepoint, so a painted grid cannot show this bug at all.)
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    wide = "世界" * 30  # 60 codepoints, 120 cells — wider than the terminal itself
    _prompt_note(app, wide)
    assert app.note_for("a") == wide  # typed in full, nothing dropped

    head, hint = " note: ", "Enter saves · ^U clears · Esc cancels"
    shown, hx, max_len = ot.App.prompt_layout(wide, 80, head, hint)
    assert shown.startswith("…") and shown.endswith("世界")  # scrolled to the cursor end
    assert ot.display_width(shown) <= max_len  # the field stays inside its budget
    assert hx == ot.display_width(head + shown)  # hint/cursor sit past the text, in cells
    assert hx + ot.display_width("   " + hint) <= 80  # ... and the hint still fits

    for line in app.renderer.note_lines(app.current_session(), 60):
        assert ot.display_width(line) <= 60  # every wrapped line fits its pane
    ot.save_notes({})


def test_note_marks_the_session_in_lists_and_shows_in_the_overview():
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00", title="Refactor")], "a")
    session = app.current_session()
    renderer = app.renderer
    assert renderer.session_marks(session) == ""

    app.notes = {"a": "the expensive one: let it loop on a flaky test"}
    assert renderer.note_tag(session) == "✎ "
    app.bookmarks = {"a"}
    assert renderer.session_marks(session) == "★ ✎ "  # both marks, in front of the title
    assert any("✎ Refactor" in line for line in renderer.month_workflows(app.months[0], 80))

    lines = renderer.detail_overview(session, 80)
    assert any(line.startswith("Note:     the expensive one") for line in lines)
    # It sits in the Session block, above Money -- it says what the money was for.
    assert next(i for i, ln in enumerate(lines) if ln.startswith("Note:")) < lines.index("# Money")


def test_note_wraps_to_the_pane_with_a_hanging_indent():
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.notes = {"a": "word " * 40}
    lines = app.renderer.note_lines(app.current_session(), 60)
    assert len(lines) > 1
    assert lines[0].startswith("Note:     word")
    assert all(line.startswith("          ") for line in lines[1:])
    assert all(len(line) <= 60 for line in lines)


class PromptScreen(FakeScreen):
    # What a real terminal hands prompt_text: characters via get_wch (str), one per
    # call, special keys as ints. FakeScreen has no get_wch, so the prompt would fall
    # back to getch — this double is what exercises the wide-character path.
    def __init__(self, keys, height=24, width=80):
        super().__init__(height, width)
        self.keys = list(keys)

    def get_wch(self):
        return self.keys.pop(0)

    def move(self, y, x):
        pass

    def refresh(self):
        pass


def _prompt_note(app, keys):
    orig_cp, orig_cs = ot.curses.color_pair, ot.curses.curs_set
    ot.curses.color_pair = lambda n: 0
    ot.curses.curs_set = lambda n: 0
    screen = PromptScreen(list(keys) + ["\n"])
    try:
        app.handle_key(screen, ord("n"))
    finally:
        ot.curses.color_pair, ot.curses.curs_set = orig_cp, orig_cs
    return screen


def test_note_prompt_takes_prose_not_just_short_ascii():
    # Two bugs the prompt inherited from the `/` filter it's modelled on, and which a
    # note (unlike a query) actually trips: input was capped at the *visible field*
    # (~26 chars on an 80-column terminal, silently truncating), and only ASCII 32..126
    # was accepted — so an em-dash or an umlaut just vanished as you typed it.
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    long_note = (
        "let it loop on a flaky test suite for 40 min — the model was fine, the tëst was not"
    )
    assert len(long_note) > 80  # longer than the whole terminal, let alone the field

    screen = _prompt_note(app, long_note)
    assert app.note_for("a") == long_note  # every character, none dropped
    assert ot.load_notes() == {"a": long_note}
    # The field scrolls to the cursor end, "…"-marked where the head is hidden.
    painted = "".join(screen.cells.get((23, x), " ") for x in range(80))
    assert "…" in painted and "tëst was not" in painted
    ot.save_notes({})


def test_note_prompt_seeds_the_existing_note_and_takes_readline_keys():
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.notes = {"a": "worth every cent"}

    # `n` on an annotated session opens the prompt with the note in it (edit, not
    # overwrite): Ctrl-W kills the last word, then the rest is typed on the end.
    _prompt_note(app, ["\x17", *"penny"])
    assert app.note_for("a") == "worth every penny"

    # Esc leaves it alone; Ctrl-U + Enter is how you clear a note you can't backspace
    # your way out of.
    _prompt_note(app, ["x", "\x1b"])
    assert app.note_for("a") == "worth every penny"
    _prompt_note(app, ["\x15"])
    assert app.note_for("a") == ""
    assert "note cleared" in app.notice
    ot.save_notes({})


def test_filter_takes_the_characters_notes_are_written_in():
    # A note can hold ä or 界 (the prompt reads wide characters), so the filter that
    # searches notes has to accept them too — otherwise you can write a note you can
    # never find. _read_key keeps a non-ASCII character as a str; ASCII stays an int so
    # every existing `key == ord("x")` binding is untouched.
    app = _app_on_session(
        [
            workflow("a", "2026-06-01 12:00:00", title="Session one"),
            workflow("b", "2026-06-01 13:00:00", title="Session two"),
        ],
        "b",
    )
    app.notes = {"b": "größere Umstellung"}
    app.filter_active = True
    for ch in "größ":
        assert app.handle_key(None, ch if not ch.isascii() else ord(ch))
    assert app.query == "größ"
    assert [w.id for w in app.current_sessions()] == ["b"]

    # A non-ASCII key outside a text field is ignored, never crashed on (the int
    # comparisons in handle_key would raise on a str).
    app.filter_active = False
    assert app.handle_key(None, "界")
    assert app.query == "größ"


def test_note_text_is_searchable_by_the_filter():
    # The note is a search field: it's what you wrote *because* the title wouldn't
    # lead you back here.
    app = _app_on_session(
        [
            workflow("a", "2026-06-01 12:00:00", title="Session one"),
            workflow("b", "2026-06-01 13:00:00", title="Session two"),
        ],
        "b",
    )
    app.notes = {"b": "kubernetes migration gone wrong"}
    app.query = "kubernetes"
    assert [w.id for w in app.current_sessions()] == ["b"]
    app.query = "nothing-matches-this"
    assert app.current_sessions() == []


def test_note_rides_along_in_the_sessions_export():
    app = _app_on_session([workflow("a", "2026-06-01 12:00:00")], "a")
    app.notes = {"a": "billable: Filou"}
    scope, header, rows = app._export_dataset()
    assert scope == "sessions"
    assert header[-1] == "note"
    assert rows[0][-1] == "billable: Filou"
