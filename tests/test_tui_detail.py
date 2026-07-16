"""The detail tabs: sessions/projects tables, models, subagents, turns, tools, context (tui/renderer.py)."""

import os
import re
import sqlite3
import tempfile

import opentab as ot

from tests._support import (
    FakeScreen,
    FakeStore,
    _claude_msg,
    _usage,
    _write_jsonl,
    _write_opencode_db_with_tools,
    _write_opencode_db_with_turns,
    app_with,
    screen_text,
    workflow,
)


def test_top_models_has_full_model_columns():
    # The "Top Models" overview section reuses the Models-tab table, so it carries
    # the cache/output columns too (name, runs, cost, tokens, cacheR, cacheW, output).
    app = app_with([])
    rows = [("m", 3648, 1.0, 205_600_000, 1_000_000, 2_000_000, 5_000_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert lines[0] == "# Top Models"
    assert lines[1].split() == [
        "Model",
        "Msgs",
        "Cost",
        "Share",
        "Tokens",
        "CacheR",
        "CacheW",
        "Output",
    ]
    assert "$1.00" in lines[2]
    assert "205.6M" in lines[2]
    assert "3648" in lines[2]


def test_model_table_splits_cost_across_token_categories_in_wide_panes():
    # The cacheR/cacheW/Output cells carry their attributed share of the Cost
    # column: fable lists at $10/M in, $50/M out, $1/M cacheR, $12.50/M cacheW,
    # so 100k cache-write tokens cost more than 800k cache reads -- the skew the
    # plain token counts hide.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert "800.0k ($0.80)" in lines[2]
    assert "100.0k ($1.25)" in lines[2]
    assert "50.0k ($2.50)" in lines[2]


def test_model_table_split_scales_to_the_recorded_cost():
    # A recorded cost that differs from today's list-price total is attributed
    # proportionally, so the split (with the implicit input remainder) always
    # sums to the Cost column.
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 10.10, 1_000_000, 800_000, 100_000, 50_000)]
    lines = app.renderer._model_table(rows, "# Top Models", 120)
    assert "800.0k ($1.60)" in lines[2]
    assert "100.0k ($2.50)" in lines[2]
    assert "50.0k ($5.00)" in lines[2]


def test_model_table_split_cells_align_under_their_labels():
    # Fixed sub-columns: the token count right-aligns under the header label and
    # the "($13)" groups end flush at the same column on every row, the parens
    # hugging the amount (no inner gap), whatever the magnitudes.
    app = app_with([])
    rows = [
        ("anthropic/claude-fable-5", 92, 20.60, 13_400_000, 13_100_000, 194_700, 99_200),
        ("anthropic/claude-opus-4-8", 1, 0.05, 23_500, 15_000, 1_900, 57),
    ]
    lines = app.renderer._model_table(rows, "# Model Mix", 120)
    header, first, second = lines[1], lines[2], lines[3]
    for label in ("CacheR", "CacheW", "Output"):
        i = header.index(label)
        assert first[i + 5] != " " and second[i + 5] != " "  # tokens end under the label
        assert first[i + 13] == ")" and second[i + 13] == ")"
    assert "( " not in first and "( " not in second  # parens hug the amount


def test_model_table_split_needs_width_dollars_and_models():
    app = app_with([])
    rows = [("anthropic/claude-fable-5", 10, 5.05, 1_000_000, 800_000, 100_000, 50_000)]
    # Narrow pane: plain token counts, exactly the classic layout.
    narrow = app.renderer._model_table(rows, "# Top Models", 80)
    assert not any("(" in ln for ln in narrow[1:])
    assert "800.0k" in narrow[2]
    # Unpriced rows ($0.00): nothing to attribute even in a wide pane.
    unpriced = app.renderer._model_table(
        [("anthropic/claude-fable-5", 10, 0.0, 1_000_000, 800_000, 100_000, 50_000)],
        "# Top Models",
        120,
    )
    assert not any("(" in ln for ln in unpriced[1:])
    # The Tools tab reuse: tool names aren't models, so no split there either.
    tools = app.renderer._model_table(
        rows, "# Tools — this session", 120, "Tool", "Calls", price_split=False
    )
    assert not any("(" in ln for ln in tools[1:])


def test_projects_merge_across_windows_slash_styles():
    # Pi records the cwd with backslashes; OpenCode records the same directory with
    # forward slashes. They must group as ONE project, not two (issue #4).
    app = app_with(
        [
            workflow("pi", "2026-06-01 12:00:00", cost=2, directory=r"C:\DEV\examples\okf"),
            workflow("oc", "2026-06-02 12:00:00", cost=3, directory="C:/DEV/examples/okf"),
        ]
    )
    projects = app.projects
    assert [p.directory for p in projects] == [r"C:\DEV\examples\okf"]
    assert projects[0].workflows == 2 and projects[0].cost == 5
    assert {w.id for w in app.workflows_for_project(r"C:\DEV\examples\okf")} == {"pi", "oc"}


def test_projects_group_worktrees_under_root():
    app = app_with(
        [
            workflow("m", "2026-06-01 12:00:00", cost=1, directory="/repo/app"),
            workflow("w", "2026-06-02 12:00:00", cost=2, directory="/repo/app-feat"),
        ]
    )
    app._root_by_dir = {"/repo/app-feat": "/repo/app"}  # feat is a worktree of app
    assert [p.directory for p in app.projects] == ["/repo/app"]
    assert app.projects[0].workflows == 2 and app.projects[0].cost == 3
    assert {w.id for w in app.workflows_for_project("/repo/app")} == {"m", "w"}


def _paint_sessions_picker(app, width=100):
    screen = FakeScreen(24, width)
    orig_cp, orig_ip = ot.curses.color_pair, ot.curses.init_pair
    ot.curses.color_pair = lambda n: 0
    ot.curses.init_pair = lambda *a: None
    try:
        app.renderer.draw_sessions_picker(screen, 0, 0, 24, width)
    finally:
        ot.curses.color_pair, ot.curses.init_pair = orig_cp, orig_ip
    return screen_text(screen).splitlines()


def test_sessions_picker_shows_a_project_column_in_time_mode():
    app = app_with(
        [
            workflow("s1", "2026-06-01 12:00:00", title="first", directory="/tmp/alpha"),
            workflow("s2", "2026-06-02 12:00:00", title="second", directory="/tmp/beta"),
        ]
    )
    app.focus = "months"
    app.view = "zoom"
    app.tab = app.month_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Project" in header  # the column header sits between Subagents and Title
    assert header.index("Subagents") < header.index("Project") < header.index("Title")
    assert any("alpha" in ln and "first" in ln for ln in lines)  # each row names its project
    assert any("beta" in ln and "second" in ln for ln in lines)


def test_sessions_picker_shows_the_date_beyond_day_scope():
    # A year (or "All years") scope spans months, so a bare clock time is useless --
    # the picker must show the date there, like the month scope already does. Only a
    # zoomed day (every row shares that day) keeps the time-only column.
    app = app_with(
        [
            workflow("s1", "2026-06-01 12:15:00", title="june"),
            workflow("s2", "2025-11-02 08:34:00", title="november"),
        ]
    )
    app.view = "zoom"
    app.focus = "years"  # defaults to the "All years" row -> both years listed
    app.tab = app.year_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Started" in header and "Time" not in header
    assert any("2026-06-01" in ln and "june" in ln for ln in lines)
    assert any("2025-11-02" in ln and "november" in ln for ln in lines)
    app.focus = "months"  # scoped to one month, but it still spans days -> date column
    app.tab = app.month_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Started" in header and "Time" not in header
    assert any("2026-06-01" in ln and "june" in ln for ln in lines)
    app.focus = "days"
    app.tab = app.day_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Time" in header and "Started" not in header
    assert any("12:15" in ln and "june" in ln for ln in lines)  # clock, not the date
    assert not any("2026-06-01" in ln for ln in lines)


def test_sessions_picker_hides_the_project_column_when_project_scoped():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha"),
            workflow("b", "2026-06-02 12:00:00", directory="/tmp/alpha"),
        ]
    )
    # A zoomed project in projects mode: every session is that project's already.
    app.browse_mode = "projects"
    app.view = "zoom"
    app.tab = app.project_tabs.index("Sessions")
    lines = _paint_sessions_picker(app)
    header = next(ln for ln in lines if "Title" in ln)
    assert "Project" not in header
    # Same for a Projects-tab drill-in on a zoomed month (time mode + zoom_project).
    app2 = app_with([workflow("a", "2026-06-01 12:00:00", directory="/tmp/alpha")])
    app2.focus = "months"
    app2.view = "zoom"
    app2.tab = app2.month_tabs.index("Sessions")
    app2.zoom_project = "/tmp/alpha"
    lines2 = _paint_sessions_picker(app2)
    header2 = next(ln for ln in lines2 if "Title" in ln)
    assert "Project" not in header2


def test_browse_preview_and_zoom_picker_are_the_same_session_table():
    # Enter (browse -> zoom) must light up a row, never re-shape the table. The
    # preview and the picker were two hand-written tables and had drifted: the
    # preview had Models + Src columns and a "# Monthly Sessions" heading, the
    # picker had a Project column, an inline [oc] tag and a 2-column indent. They
    # build from one set of helpers now, so the frames can't diverge again.
    app = app_with(
        [
            workflow("s1", "2026-06-01 12:00:00", title="first", directory="/tmp/alpha"),
            workflow("s2", "2026-06-02 12:00:00", title="second", directory="/tmp/beta"),
        ]
    )
    app.focus = "months"
    app.tab = app.month_tabs.index("Sessions")
    # The picker draws into a 100-wide pane, i.e. a content width of w - 4.
    preview = app.renderer.month_workflows(app.selected_month_summary, 96)
    assert not preview[0].startswith("#")  # no heading line to shift the rows down

    app.view = "zoom"
    painted = _paint_sessions_picker(app, 100)

    header = next(ln for ln in painted if "Title" in ln)
    assert header.strip() == preview[0].strip()  # same columns, same sort arrows
    rows = [ln.strip() for ln in painted if "first" in ln or "second" in ln]
    assert rows and len(rows) == len(preview) - 1
    # The cursor is the only difference: strip it and the rows are identical.
    assert [r.lstrip(">").strip() for r in rows] == [p.strip() for p in preview[1:]]


def test_detail_tools_reprices_unpriced_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_tools(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        normal = rnd.detail_tools(wf, 92)
        joined = "\n".join(normal)
        assert "# Tools" in joined
        assert "# By server / namespace" in joined
        assert "(built-in)" in joined  # the server rollup labels built-in vs MCP
        # The subscription session records $0; under "$" the wholly-unpriced serena
        # row picks up its list-price estimate (1M Haiku input @ $1/M = $1.00).
        app.show_api_prices = True
        app._ensure_models()
        serena_line = next(
            line for line in rnd.detail_tools(wf, 92) if line.startswith("serena_read_file")
        )
        assert "$1.00" in serena_line


def test_detail_turns_cumulative_and_reprices_under_dollar():
    args = type("Args", (), {"since": None, "until": None, "days": None})
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        _write_opencode_db_with_turns(db)
        app = ot.App(ot.Store(db, type("A", (), {"demo": False})()), args())
        rnd = ot.Renderer(app)
        wf = app.loaded[0]
        # Normal mode: only the priced step counts -> $3.00 total, ends at 100%.
        normal = rnd.detail_turns(wf, 96)
        joined = "\n".join(normal)
        assert normal[0] == "# Turns — 3 turns, $3.00 total"
        assert "· Grouped by the user prompt" in joined
        assert "$3.00 · 100%" in joined  # last turn's cumulative cell
        # turns are grouped under their owning user prompt (▸ header), m2 under u2
        assert "▸ Add feature X" in joined and "▸ Fix the bug" in joined
        # each row shows the date + clock ("MM-DD HH:MM:SS"), not just the time
        assert any(re.search(r"\d\d-\d\d \d\d:\d\d:\d\d", ln) for ln in normal)
        # Under "$" the two $0 haiku turns estimate at list price (1M+2M @ $1/M),
        # so the total grows to $1 + $2 + $3 = $6.00 and each shows its estimate.
        app.show_api_prices = True
        priced = rnd.detail_turns(wf, 96)
        assert priced[0] == "# Turns — 3 turns, $6.00 total"
        pjoined = "\n".join(priced)
        assert "$1.00" in pjoined and "$2.00" in pjoined and "$6.00 · 100%" in pjoined
        # the per-prompt subtotal sits on the group header (u1 = $1+$2 estimate = $3.00)
        assert "▸ Add feature X" in pjoined


def test_subagents_tab_reprices_unpriced_node_in_api_mode():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            create table session (
              id text primary key,
              parent_id text,
              title text,
              directory text,
              time_created integer,
              cost real default 0 not null,
              tokens_input integer default 0 not null,
              tokens_output integer default 0 not null,
              tokens_reasoning integer default 0 not null,
              tokens_cache_read integer default 0 not null,
              tokens_cache_write integer default 0 not null
            );
            create table message (session_id text, data text);
            """
        )
        conn.executemany(
            "insert into session values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("root", None, "Root", "/tmp/project", 1760000000000, 0.2, 0, 1000, 0, 0, 0),
                (
                    "child",
                    "root",
                    "Child",
                    "/tmp/project",
                    1760000001000,
                    0.0,
                    1_000_000,
                    0,
                    0,
                    0,
                    0,
                ),
            ],
        )
        conn.executemany(
            "insert into message values (?, ?)",
            [
                (
                    "root",
                    '{"role":"assistant","providerID":"anthropic","modelID":"claude-opus-4.5","cost":0.2,"tokens":{"input":0,"output":1000}}',
                ),
                # Unpriced Copilot/Opus subagent: $0 in OpenCode, real token usage.
                (
                    "child",
                    '{"role":"assistant","providerID":"github-copilot","modelID":"claude-opus-4.5","cost":0,"tokens":{"input":1000000,"output":0}}',
                ),
            ],
        )
        conn.commit()
        conn.close()

        store = ot.Store(db, type("Args", (), {"demo": False})())
        app = ot.App(store, type("Args", (), {"since": None, "until": None, "days": None})())

        expected = ot.api_equivalent_cost("github-copilot/claude-opus-4.5", 1_000_000, 0, 0, 0, 0)
        assert expected > 0  # guard: model must resolve to a real list price

        # Real mode: the unpriced subagent reads as $0.00.
        real = app._priced_nodes([r for r in store.workflow_nodes("root") if r["depth"] > 0])
        assert real[0]["cost"] == 0.0
        assert "$0.00" in app.renderer.detail_subagents(app.loaded[0], 200)[-1]

        # API mode: it is repriced to the Opus API-equivalent. _priced_nodes feeds
        # both the rendered tab and the CSV export, so asserting it covers both.
        app.toggle_api_prices()
        priced = app._priced_nodes([r for r in store.workflow_nodes("root") if r["depth"] > 0])
        assert round(priced[0]["cost"], 6) == round(expected, 6)
        sub_line = app.renderer.detail_subagents(app.loaded[0], 200)[-1]
        assert ot.money(expected) in sub_line
        assert "$0.00" not in sub_line


def _subagent_rows():
    return [
        {
            "depth": 1,
            "agent": "b",
            "model_name": "m",
            "cost": 2.0,
            "tokens_total": 10,
            "title": "b",
            "created_at": "2026-06-01 13:00:00",
        },
        {
            "depth": 1,
            "agent": "a",
            "model_name": "m",
            "cost": 1.0,
            "tokens_total": 20,
            "title": "a",
            "created_at": "2026-06-01 12:00:00",
        },
    ]


def test_subagents_tab_is_sortable_by_tokens():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")
    app.subagent_sort_by = "tokens"

    assert app.current_sort_options() == app.subagent_sort_options
    assert app.sorted_subagent_rows(_subagent_rows())[0]["title"] == "a"


def test_subagents_tab_is_sortable_by_date():
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")
    app.subagent_sort_by = "date"  # newest first by default
    assert [r["title"] for r in app.sorted_subagent_rows(_subagent_rows())] == ["b", "a"]
    app.subagent_sort_reverse = True  # flipped: chronological
    assert [r["title"] for r in app.sorted_subagent_rows(_subagent_rows())] == ["a", "b"]


def test_subagent_sort_is_independent_of_session_sort():
    # Sorting the Subagents tab must not clobber the sessions-list preference
    # (they used to share sort_by, so picking "depth" here reset sessions to cost).
    app = app_with([workflow("june", "2026-06-01 12:00:00")])
    app.sort_by = "date"
    app.view = "session"
    app.tab = app.workflow_tabs.index("Subagents")

    assert app.handle_key(None, ord("s"))  # opens the subagent sort picker
    assert app.sort_menu and app.sort_menu_options() == app.subagent_sort_options
    app.handle_key(None, ord("G"))  # jump to the last option (depth)
    app.handle_key(None, 10)  # Enter applies
    assert app.subagent_sort_by == "depth"
    assert app.sort_by == "date"  # the sessions sort survived

    # A header click on the Subagents tab targets the subagent pair only.
    app.apply_header_sort("model", "subagent")
    assert app.subagent_sort_by == "model" and app.sort_by == "date"
    app.apply_header_sort("model", "subagent")  # re-click flips direction
    assert app.subagent_sort_reverse is True


def test_projects_are_grouped_and_sorted_by_cost():
    app = app_with(
        [
            workflow("cheap", "2026-06-01 12:00:00", cost=1, directory="/tmp/a"),
            workflow("expensive", "2026-06-02 12:00:00", cost=5, directory="/tmp/b"),
            workflow("more", "2026-06-03 12:00:00", cost=2, directory="/tmp/a"),
        ]
    )

    assert [p.directory for p in app.projects] == ["/tmp/b", "/tmp/a"]
    assert app.projects[1].workflows == 2
    assert app.projects[1].cost == 3


def test_projects_sort_by_tokens_and_name():
    app = app_with(
        [
            workflow("costly", "2026-06-01 12:00:00", cost=10, tokens=1, directory="/tmp/b"),
            workflow("tokeny", "2026-06-02 12:00:00", cost=1, tokens=100, directory="/tmp/a"),
        ]
    )

    app.project_sort_by = "tokens"
    assert [p.directory for p in app.projects] == ["/tmp/a", "/tmp/b"]

    app.project_sort_by = "project"
    assert [p.directory for p in app.projects] == ["/tmp/a", "/tmp/b"]


def test_projects_sort_by_recency():
    app = app_with(
        [
            # /tmp/old's newest session predates /tmp/new's, despite costing more
            workflow("o1", "2026-06-01 09:00:00", cost=99, directory="/tmp/old"),
            workflow("n1", "2026-06-10 09:00:00", cost=1, directory="/tmp/new"),
            workflow("o2", "2026-06-05 09:00:00", cost=50, directory="/tmp/old"),
        ]
    )
    app.project_sort_by = "recency"
    assert [p.directory for p in app.projects] == ["/tmp/new", "/tmp/old"]
    # last_active reflects each project's most recent session
    by_dir = {p.directory: p for p in app.projects}
    assert by_dir["/tmp/old"].last_active == "2026-06-05 09:00:00"
    assert by_dir["/tmp/new"].last_active == "2026-06-10 09:00:00"


def test_project_header_aligns_with_project_rows():
    app = app_with(
        [workflow("a", "2026-06-01 12:00:00", cost=12.34, tokens=1500, directory="/tmp/project")]
    )
    app.set_browse_mode("projects")
    project = app.projects[0]
    header = app.renderer.project_header_text(80)
    row = app.renderer.project_row_text(project, ">", 80)

    assert header.index("Cost") + len("Cost v") == row.index("$12.34") + len("$12.34")
    assert header.index("Tokens") + len("Tokens") == row.index("1.5k") + len("1.5k")
    assert header.index("Ses") + len("Ses") == row.index("  1 ses") + len("  1 ses")
    assert header.index("Subagents") + len("Subagents") == row.index("     0 subs") + len(
        "     0 subs"
    )
    assert len(header) <= 80
    assert len(row) <= 80


def test_subagents_tab_header_is_click_sortable_and_shows_started():
    class NodeStore(FakeStore):
        def workflow_nodes(self, wid):
            return [
                {
                    "depth": d,
                    "agent": "task",
                    "model_name": "anthropic/x",
                    "cost": 1.0,
                    "tokens_total": 10,
                    "title": t,
                    "created_at": ts,
                    "tokens_input": 5,
                    "tokens_output": 5,
                    "tokens_reasoning": 0,
                    "tokens_cache_read": 0,
                    "tokens_cache_write": 0,
                }
                for d, t, ts in (
                    (0, "root", "2026-06-01 12:00:00"),
                    (1, "sub", "2026-06-01 12:30:00"),
                )
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(NodeStore([workflow("s1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    rnd = app.renderer
    rnd._line_sort_headers = {}
    lines = rnd.detail_subagents(app.loaded[0], 120)
    assert lines[1].startswith("Started")
    assert "2026-06-01 12:30" in lines[2]  # the subagent row carries its start time
    cols, target = rnd._line_sort_headers[1]
    assert target == "subagent" and cols == rnd.SUBAGENT_SORT_COLUMNS


def test_month_and_day_views_have_projects_tab():
    app = app_with([workflow("a", "2026-06-01 12:00:00")])

    app.focus = "months"
    assert "Projects" in app.current_tabs()

    app.focus = "days"
    assert "Projects" in app.current_tabs()


def test_month_projects_are_scoped_and_sortable():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", cost=1, tokens=100, directory="/tmp/a"),
            workflow("b", "2026-06-02 12:00:00", cost=2, tokens=10, directory="/tmp/b"),
            workflow("old", "2026-05-01 12:00:00", cost=99, tokens=999, directory="/tmp/old"),
        ]
    )
    app.focus = "months"
    app.tab = app.month_tabs.index("Projects")
    app.project_sort_by = "tokens"

    lines = app.renderer.month_projects(app.selected_month_summary, 100)

    assert "/tmp/a" in lines[1]  # lines[0] is the column header (no heading above it)
    assert "/tmp/b" in lines[2]
    assert all("/tmp/old" not in line for line in lines)
    assert app.handle_key(None, ord("s"))  # opens the project-sort picker
    assert app.sort_menu and app.sort_menu_index == 1  # current is tokens
    app.handle_key(None, ord("j"))  # -> sessions
    app.handle_key(None, 10)  # Enter applies
    assert app.project_sort_by == "sessions"
    assert app.sort_by == "cost"


def test_day_projects_are_scoped():
    app = app_with(
        [
            workflow("a", "2026-06-01 12:00:00", directory="/tmp/a"),
            workflow("b", "2026-06-02 12:00:00", directory="/tmp/b"),
        ]
    )
    app.focus = "days"
    app.tab = app.day_tabs.index("Projects")

    lines = app.renderer.day_projects(app.selected_day_summary, 100)

    assert any("/tmp/b" in line for line in lines)
    assert all("/tmp/a" not in line for line in lines)


def test_projects_panel_width_is_content_aware_and_bounded():
    longpath = "/Users/x/deeply/nested/repo/with/a/very/long/path/indeed/and/more/sub"
    wide = app_with([workflow("a", "2026-06-01 12:00:00", directory=longpath)])
    wide.set_browse_mode("projects")
    narrow = app_with([workflow("a", "2026-06-01 12:00:00", directory="/x/y")])
    narrow.set_browse_mode("projects")

    # A long path widens the panel, but never past half the screen.
    w = wide.renderer.projects_left_width(160)
    assert w <= 160 // 2
    assert w < 160 - 44  # not maxed to the screen
    # A short-path list sizes down to its own (smaller) needs.
    assert narrow.renderer.projects_left_width(160) < w


def test_pager_lines_dispatch_session_tabs_by_name():
    # current_pager_lines feeds G / max_scroll / page scrolling; it must dispatch
    # the session tabs by NAME like draw_detail does -- current_tabs() appends
    # Turns/Tools per session, so a fixed index would clamp e.g. the Turns tab
    # against the Subagents line count.
    class RichStore(FakeStore):
        def workflow_nodes(self, wid):
            return [
                {
                    "depth": 1,
                    "agent": "build",
                    "model_name": "anthropic/claude",
                    "cost": 0.5,
                    "tokens_total": 1234,
                    "title": "do the thing",
                    "tokens_input": 1000,
                    "tokens_output": 200,
                    "tokens_reasoning": 0,
                    "tokens_cache_read": 34,
                    "tokens_cache_write": 0,
                }
            ]

        def supports_turns(self, wid):
            return True

        def supports_tools(self, wid):
            return True

        def message_timeline(self, wid):
            return [
                {
                    "time": "2026-06-01 12:00:01",
                    "agent": "main",
                    "depth": 0,
                    "model_name": "anthropic/claude",
                    "cost": 0.25,
                    "tokens_total": 800,
                    "input": 600,
                    "output": 200,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "prompt_id": "p1",
                    "prompt_title": "first prompt",
                }
            ]

        def tool_breakdown(self, wid):
            return [
                {
                    "tool": "bash",
                    "model_name": "anthropic/claude",
                    "calls": 3,
                    "cost": 0.1,
                    "tokens_total": 500,
                    "input": 400,
                    "output": 100,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                }
            ]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(RichStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    wf = app.current_session()
    assert app.current_tabs() == ("Overview", "Models", "Subagents", "Turns", "Tools", "Context")
    for name, table in (
        ("Overview", app.renderer.detail_overview),
        ("Models", app.renderer.detail_models),
        ("Subagents", app.renderer.detail_subagents),
        ("Turns", app.renderer.detail_turns),
        ("Tools", app.renderer.detail_tools),
        ("Context", app.renderer.detail_context),
    ):
        app.tab = app.current_tabs().index(name)
        assert app.renderer.current_pager_lines(100) == table(wf, 96)  # content = width - 4


def test_subagent_nodes_memoized_per_session():
    def node(workflow_id, depth, agent, title):
        return {
            "id": f"{workflow_id}:{depth}",
            "depth": depth,
            "agent": agent,
            "title": title,
            "created_at": "",
            "cost": 1.0,
            "model_name": "anthropic/x",
            "tokens_input": 1,
            "tokens_output": 1,
            "tokens_reasoning": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "tokens_total": 2,
        }

    class NodeStore(FakeStore):
        node_calls = 0

        def workflow_nodes(self, workflow_id):
            self.node_calls += 1
            return [node(workflow_id, 0, "-", "root"), node(workflow_id, 1, "task", "sub")]

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(NodeStore([workflow("s1", "2026-06-01 12:00:00")]), args)
    rows1 = app.session_node_rows("s1")
    rows2 = app.session_node_rows("s1")
    assert app.store.node_calls == 1  # every repaint after the first is memo-served
    assert rows1 is rows2 and [r["depth"] for r in rows1] == [0, 1]
    # The Subagents export dataset reads through the same memo (no new store call).
    kind, header, rows = app._subagents_dataset(app.loaded[0])
    assert kind == "subagents" and app.store.node_calls == 1
    assert [r[1] for r in rows] == [1]  # depth-0 root filtered out, subagent kept
    # Reload drops the memo -- the underlying data may have changed.
    app.reload()
    app.session_node_rows("s1")
    assert app.store.node_calls == 2


def test_session_data_ready_flips_after_prefetch():
    # The TUI's drill-in loading frame: a session whose lazy fetches aren't
    # memoized isn't "ready" (draw_detail paints the loading placeholder instead
    # of blocking mid-draw), and one prefetch_session_data satisfies every gate
    # so the next frame renders real data -- the prefetch must never leave
    # ready() False (that would be a loading-frame loop).
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        cwd = os.path.join(tmp, "repo")
        rows_in = [
            _claude_msg(
                "s1", "claude-opus-4-8", _usage(100, 50), uuid="a1", cwd=cwd, tools=["Bash"]
            ),
        ]
        _write_jsonl(os.path.join(root, "s1.jsonl"), rows_in)
        store = ot.ClaudeStore(os.path.join(tmp, "projects"), type("A", (), {"demo": False})())
        app = ot.App(store, type("Args", (), {"since": None, "until": None, "days": None})())
        assert not app.session_data_ready("s1")
        app.prefetch_session_data("s1")
        assert app.session_data_ready("s1")
        assert app.session_tool_rows("s1")[0]["tool"] == "Bash"


class _ContextStore(FakeStore):
    # Turn rows whose recorded prompts grow, get compacted once, then regrow; the
    # oversized subagent turn must never bend the main-thread curve.
    SIZES = (40_000, 80_000, 120_000, 160_000, 60_000, 90_000)

    def supports_turns(self, wid):
        return True

    def message_timeline(self, wid):
        rows = []
        for i, v in enumerate(self.SIZES):
            rows.append(
                {
                    "time": f"2026-06-01 12:00:{i:02d}",
                    "agent": "-",
                    "depth": 0,
                    "model_name": "anthropic/claude-testmodel",
                    "cost": 0.0,
                    "input": 1000,
                    "output": 50,
                    "reasoning": 0,
                    "cache_read": v - 1000,
                    "cache_write": 0,
                    "tokens_total": v + 50,
                    "prompt_id": "p1",
                    "prompt_title": "hi",
                }
            )
        rows.append(
            {
                "time": "2026-06-01 12:00:99",
                "agent": "subagent",
                "depth": 1,
                "model_name": "anthropic/claude-testmodel",
                "cost": 0.0,
                "input": 900_000,
                "output": 10,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "tokens_total": 900_010,
                "prompt_id": "p1",
                "prompt_title": "hi",
            }
        )
        return rows


def test_context_tab_charts_measured_growth_and_marks_compaction():
    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(_ContextStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    assert "Context" in app.current_tabs()
    lines = app.renderer.detail_context(app.current_session(), 90)
    joined = "\n".join(lines)
    # measured stats: the peak is the biggest main-thread turn (not the huge
    # subagent turn), the window comes from the family fallback (claude -> 200k)
    assert joined.startswith("# Context — anthropic/claude-testmodel · 200.0k window")
    assert "160.0k" in joined and "of the window" in joined
    assert "6 turns" in joined and "900.0k" not in joined
    # the 160k -> 60k drop is a compaction: counted, marked and itemized
    assert "compacted 1×" in joined and "freed ~100.0k" in joined
    assert "▼" in joined and "160.0k → 60.0k" in joined
    # chart rows carry plain heat levels (colors resolve only at paint time)
    heat = app.renderer._ctx_line_heat
    assert heat and all(isinstance(lvl, int) for lvl in heat.values())
    # one color grammar: the peak line wears the heat of the height it describes
    # (160k of the 200k window -> the hottest band), and every ▼ compaction line
    # shares the marker row's amber
    peak_idx = next(i for i, ln in enumerate(lines) if ln.startswith("  peak"))
    assert heat[peak_idx] == 4  # int(160/200 * 5)
    for i, ln in enumerate(lines):
        if ln.startswith(("  compacted", "  ▼")):
            assert heat[i] == app.renderer._CTX_MARK
    # no composition opt-in on this store -> the estimated section stays absent
    assert "What filled it" not in joined


def test_context_tab_no_usage_message():
    args = type("Args", (), {"since": None, "until": None, "days": None})()

    class NoUsage(FakeStore):
        def supports_turns(self, wid):
            return True

        def message_timeline(self, wid):
            return []

    app = ot.App(NoUsage([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    lines = app.renderer.detail_context(app.current_session(), 80)
    assert lines[0] == "# Context"
    assert "No per-turn context usage" in lines[1]


def test_context_tab_hidden_when_curve_unsupported():
    # A backend whose turn rows are cumulative deltas, not per-request prompt
    # sizes (Codex), opts out of the curve -- the whole tab disappears rather
    # than charting per-turn consumption as context. Turns/Tools stay.
    class DeltaStore(_ContextStore):
        def supports_context_curve(self, wid):
            return False

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(DeltaStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    tabs = app.current_tabs()
    assert "Turns" in tabs and "Context" not in tabs
    # CodexStore itself is the real opt-out
    codex = ot.CodexStore("/nonexistent", type("A", (), {"demo": False})())
    assert codex.supports_context_curve("any") is False


def test_context_tab_flags_mixed_model_windows():
    # After a mid-session model switch the chart still scales to the last model's
    # window (declared in the header), but the peak %% must use the window the
    # peak turn actually ran in, and a "!" caveat calls out the mixed windows.
    class SwitchStore(_ContextStore):
        def message_timeline(self, wid):
            rows = super().message_timeline(wid)
            rows[0]["model_name"] = "openai/gpt-5-early"  # 400k fallback window
            return rows

    args = type("Args", (), {"since": None, "until": None, "days": None})()
    app = ot.App(SwitchStore([workflow("ses_1", "2026-06-01 12:00:00")]), args)
    app.view = "session"
    joined = "\n".join(app.renderer.detail_context(app.current_session(), 90))
    # peak turn (160k) ran on the claude model -> 200k window -> 80%
    assert "(80%)" in joined
    assert "! this session switched between models" in joined
    assert "200.0k window" in joined  # header still names the live window
