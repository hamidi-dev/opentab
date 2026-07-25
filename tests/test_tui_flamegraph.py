"""The session flamegraph: a session's spend as a hierarchy, width = dollars."""

import random

import opentab as ot
from opentab.heatmap import TOKEN_SERIES_BASE_PAIR
from opentab.pricing import api_equivalent_cost
from opentab.tui.app import FLAME_SELF_SLOT

from tests._support import AttrScreen, app_with, workflow

MODEL = "anthropic/claude-opus-4.5"


def _node(
    depth, agent, cost, tokens, title="a session title", at="2026-06-01 12:00:00", model=MODEL
):
    # One workflow_nodes row. `agent` leads because a segment names the AGENT, never the
    # session's title -- the title is here only so the tests that exercise the fallbacks
    # (the "(@name)" tag, the tie-break) have one to work against. Tokens are all "input"
    # so the list-price estimate of an unrecorded node is a single multiplication a test
    # can restate.
    return {
        "depth": depth,
        "title": title,
        "agent": agent,
        "created_at": at,
        "model_name": model,
        "cost": cost,
        "tokens_total": tokens,
        "tokens_input": tokens,
        "tokens_output": 0,
        "tokens_reasoning": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
    }


def _app(nodes, api=False):
    # Seed the node memo directly: session_node_rows reads it before touching the store,
    # which is what lets a flamegraph test state its tree instead of building a backend.
    app = app_with([workflow("s1", "2026-06-01 12:00:00")])
    app._nodes_by_session["s1"] = nodes
    app.view = "session"
    app.show_api_prices = api
    return app


def _unbox(lines):
    # The box's content rows, with the "│ … │" gutters and padding stripped.
    return [ln[2:].rstrip(" │|") for ln in lines if ln[:1] in ("│", "|")]


GLYPHS = "█▓▒░▚"


def _band(lines):
    # The one bar: filled edge to edge with block glyphs (only the share rides inside it).
    # The colour key also opens with one, but ends on a label.
    bars = [ln for ln in _unbox(lines) if ln and ln[0] in GLYPHS and ln[-1] in GLYPHS]
    return bars[0] if bars else ""


def _labels(lines):
    # The positioned rows under the band: content lines that are neither the band, the
    # caption, the key, nor the headline.
    out = []
    for ln in _unbox(lines):
        if not ln or ln[0] in GLYPHS or ln.startswith(("session ·", "root kept")):
            continue
        out.append(ln)
    return out


def _tree(nodes, root_cost=6.0):
    return [_node(0, "root", root_cost, 1000)] + nodes


def test_segment_widths_are_the_cost_columns_own_numbers():
    # The one contract that keeps the chart and the table under it honest: a segment IS
    # its row's Cost cell. If they ever diverge, the same screen shows a bar claiming one
    # share and a table row claiming another, and neither can be trusted.
    nodes = _tree([_node(1, "docs", 3.0, 500), _node(1, "tests", 1.0, 200)], root_cost=6.0)
    app = _app(nodes)
    flame = app.session_flame(app.loaded[0])
    assert flame.unit == "cost"
    assert flame.total == 10.0
    assert [s.value for s in flame.segments] == [6.0, 3.0, 1.0]  # self first, then cost-desc
    assert [round(s.share, 3) for s in flame.segments] == [0.6, 0.3, 0.1]
    assert abs(sum(s.share for s in flame.segments) - 1.0) < 1e-9
    # ...and each one is exactly what the table's Cost column prints for that node.
    by_agent = {r["agent"]: r["cost"] for r in app._priced_nodes(nodes)}
    assert by_agent["docs"] == flame.segments[1].value
    assert by_agent["tests"] == flame.segments[2].value


def test_the_root_owns_a_slot_no_subagent_can_wear():
    # Root-vs-delegated is the ONE distinction the chart makes, so it cannot rest on a
    # colour a sixth subagent also gets. Children cycle the other four.
    nodes = _tree([_node(1, f"a{i}", 1.0, 10) for i in range(6)])
    flame = _app(nodes).session_flame(_app(nodes).loaded[0])
    assert flame.segments[0].label == "root (self)"
    assert flame.segments[0].slot == FLAME_SELF_SLOT == 0
    child_slots = [s.slot for s in flame.segments if s.depth]
    assert 0 not in child_slots
    assert child_slots == [1, 2, 3, 4, 1, 2]  # cycles, and never lands next to its twin


def test_a_session_that_recorded_nothing_divides_tokens_and_names_the_key():
    # A subscription backend with "$" off records $0 on every node, and a hierarchy of
    # zeros is a blank frame. Tokens answer the same question one price list away -- but
    # the unit has to be stated, and the way out of it named off the LIVE keymap (the
    # user may have rebound "$").
    nodes = _tree([_node(1, "docs", 0.0, 400)], root_cost=0.0)
    app = _app(nodes)
    flame = app.session_flame(app.loaded[0])
    assert flame.unit == "tokens" and flame.total == 1400
    assert [s.value for s in flame.segments] == [1000, 400]
    assert not flame.estimated  # nothing was estimated -- nothing was priced at all
    lines = app.renderer._flamegraph_box(app.loaded[0], 90)
    assert any("width = tokens" in ln for ln in _unbox(lines))
    key = app.keymap.label("main", "api_prices")
    assert any(f"width is TOKENS — press {key}" in ln for ln in lines)


def test_the_dollar_view_estimates_the_widths_and_marks_them():
    # With "$" on, _priced_nodes reprices the unrecorded nodes at list rates -- so the
    # chart gains dollars, and has to say they are estimates rather than pass a
    # counterfactual off as a bill.
    nodes = _tree([_node(1, "docs", 0.0, 2_000_000)], root_cost=0.0)
    app = _app(nodes, api=True)
    flame = app.session_flame(app.loaded[0])
    assert flame.unit == "cost" and flame.estimated
    assert round(flame.total, 9) == round(api_equivalent_cost(MODEL, 2_001_000, 0, 0, 0, 0), 9)
    lines = app.renderer._flamegraph_box(app.loaded[0], 90)
    assert lines[0].startswith("┌ Where the money went · ~$")  # the "~" rides the title
    assert any("include list-price estimates" in ln for ln in lines)


def test_a_fully_recorded_session_is_never_marked_estimated():
    app = _app(_tree([_node(1, "docs", 3.0, 500)]), api=True)
    flame = app.session_flame(app.loaded[0])
    assert not flame.estimated
    assert "~" not in app.renderer._flamegraph_box(app.loaded[0], 90)[0]


def test_the_agent_is_mined_out_of_the_title_when_the_column_is_empty():
    # A segment names the AGENT, never the session title. OpenCode records the name in
    # its own column for only some sessions and leaves it in the title as "(@explore)"
    # for the rest -- on real data mining it back lifts the share of nodes that can name
    # their agent from 15% to 85%, and the names it recovers are exactly the ones the
    # column holds when it is populated. With neither, "subagent" is the honest answer:
    # Claude Code names no Task and titles them all "subagent run", so falling through to
    # the title there would put a session name on the chart to say nothing.
    nodes = _tree(
        [
            _node(1, "code-reviewer", 4.0, 40, title="Review the browse mode"),
            _node(1, "-", 3.0, 30, title="Find the config loader (@explore subagent)"),
            _node(1, "-", 2.0, 20, title="Fix the CLI flags (@general)"),
            _node(1, "subagent", 1.0, 10, title="subagent run"),
        ]
    )
    agents = [g.agent for g in _app(nodes).session_flame(_app(nodes).loaded[0]).children]
    assert agents == ["code-reviewer", "explore", "general", "subagent"]


def test_the_segment_carries_its_model_in_its_short_spelling():
    # Route prefix dropped and the release-date suffix stripped: a segment has tens of
    # cells, not eighty.
    nodes = _tree(
        [_node(1, "explore", 3.0, 30, model="anthropic/claude-haiku-4-5-20251001")],
        root_cost=6.0,
    )
    flame = _app(nodes).session_flame(_app(nodes).loaded[0])
    assert [g.model for g in flame.segments] == ["claude-opus-4.5", "claude-haiku-4-5"]
    assert not flame.one_model  # they differ, so there is no single model to caption
    same = _tree([_node(1, "explore", 3.0, 30)])
    assert _app(same).session_flame(_app(same).loaded[0]).one_model == "claude-opus-4.5"


def test_repeated_agents_keep_a_unique_key_handle_but_a_bare_name_below():
    # Agents repeat by nature -- five "code-reviewer" runs in one session is normal --
    # so the two names diverge. Under the band position says which slice is which, so the
    # bare agent is right there; in the key there is no position to lean on, so the ladder
    # adds the start clock (findable in the table's Started column), then seconds, then a
    # cost rank.
    spread = _tree(
        [
            _node(1, "code-reviewer", 3.0, 30, at="2026-06-01 12:05:00"),
            _node(1, "code-reviewer", 2.0, 20, at="2026-06-01 12:09:00"),
        ]
    )
    flame = _app(spread).session_flame(_app(spread).loaded[0])
    assert [g.agent for g in flame.children] == ["code-reviewer", "code-reviewer"]
    assert [g.label for g in flame.children] == ["code-reviewer 12:05", "code-reviewer 12:09"]

    # Same minute: seconds break the tie.
    burst = _tree(
        [
            _node(1, "explore", 3.0, 30, at="2026-06-01 12:05:01"),
            _node(1, "explore", 2.0, 20, at="2026-06-01 12:05:44"),
        ]
    )
    labels = [g.label for g in _app(burst).session_flame(_app(burst).loaded[0]).children]
    assert labels == ["explore 12:05:01", "explore 12:05:44"]

    # ClaudeStore stamps every sidechain with the session's own start, so even seconds
    # tie: fall through to the cost rank, which is the table's default ordering.
    same = _tree([_node(1, "subagent", 3.0, 30), _node(1, "subagent", 2.0, 20)])
    labels = [g.label for g in _app(same).session_flame(_app(same).loaded[0]).children]
    assert labels == ["subagent #1", "subagent #2"]


def test_a_nested_execution_joins_the_band_as_a_marked_sibling():
    # workflow_nodes records a node's depth but not its PARENT, so a depth-2 node cannot
    # be drawn under the depth-1 node it ran below. It joins the band as a sibling --
    # marked, counted, and explained -- rather than being nested under a guess.
    nodes = _tree([_node(1, "docs", 3.0, 300), _node(2, "deep", 1.0, 100)])
    app = _app(nodes)
    flame = app.session_flame(app.loaded[0])
    assert flame.deep == 1
    deep = next(g for g in flame.segments if g.depth == 2)
    assert deep.agent == "↳ deep"
    # Still an exact partition: nothing is dropped and nothing is double-counted.
    assert abs(sum(g.share for g in flame.segments) - 1.0) < 1e-9
    assert any(
        "records depth but not parents" in ln
        for ln in app.renderer._flamegraph_box(app.loaded[0], 90)
    )


def test_a_subagent_that_recorded_nothing_is_counted_not_drawn():
    # A zero-width segment is not a segment -- but a subagent that ran and recorded
    # nothing is a fact about the data, so it is counted and pointed at the table.
    nodes = _tree([_node(1, "docs", 3.0, 300), _node(1, "aborted", 0.0, 0)])
    app = _app(nodes)
    flame = app.session_flame(app.loaded[0])
    assert flame.silent == 1
    assert [g.label for g in flame.segments] == ["root (self)", "docs"]
    assert any("recorded no spend" in ln for ln in app.renderer._flamegraph_box(app.loaded[0], 90))


def test_the_headline_reports_the_split_in_one_sentence():
    nodes = _tree([_node(1, "docs", 3.0, 300), _node(1, "tests", 1.0, 100)], root_cost=6.0)
    app = _app(nodes)
    head = _unbox(app.renderer._flamegraph_box(app.loaded[0], 90))[0]
    assert head.startswith("root kept 60% ($6.00)")
    assert "2 subagents split $4.00" in head
    assert "biggest docs 30%" in head


def test_a_narrow_pane_keeps_the_sentence_and_drops_the_bands():
    # Below ~30 usable cells the segments stop being distinguishable, and five ambiguous
    # cells say less than the sentence does.
    app = _app(_tree([_node(1, "docs", 3.0, 300), _node(1, "tests", 1.0, 100)]))
    wide = app.renderer._flamegraph_box(app.loaded[0], 90)
    narrow = app.renderer._flamegraph_box(app.loaded[0], 26)
    assert _band(wide) and not _band(narrow)
    assert _unbox(narrow)[0].startswith("root kept")


def test_the_names_sit_under_their_own_segment_not_inside_it():
    # Writing a name into the fill made the chart harder to read: the text fought the
    # colour it was punched through, and it only ever fit the segments that least needed
    # a label. Below the band each name starts at its own segment's column, so position
    # does the pointing; a name wider than its slice is dropped rather than shifted,
    # because a label sitting over the wrong slice is worse than no label.
    nodes = _tree([_node(1, "explore", 3.0, 300)] + [_node(1, f"t{i}", 0.05, 5) for i in range(6)])
    app = _app(nodes)
    lines = app.renderer._flamegraph_box(app.loaded[0], 100)
    band, labels = _band(lines), _labels(lines)
    assert "root (self)" not in band and "explore" not in band  # only the share rides in
    assert "65%" in band and "32%" in band
    names = next(ln for ln in labels if "root (self)" in ln)
    # Each name begins exactly where its own segment begins.
    widths = app.renderer._stack_widths(
        [(g.label, g.value, g.slot) for g in app.session_flame(app.loaded[0]).segments],
        app.session_flame(app.loaded[0]).total,
        96,
    )
    assert names.index("root (self)") == 0
    assert names.index("explore") == widths[0]
    assert "t0" not in names  # a 0.5% sliver is too narrow for a name of any length


def test_the_models_get_their_own_positioned_row_only_when_they_differ():
    # 85 of 135 real delegating sessions run one model end to end; there it belongs in
    # the caption once, and a row repeating it under every segment would spend a line to
    # say nothing. When they differ, each segment names its own -- which is exactly what
    # the caption's restraint buys the room for.
    same = _app(_tree([_node(1, "explore", 4.0, 40)]))
    lines = same.renderer._flamegraph_box(same.loaded[0], 100)
    assert any("all on claude-opus-4.5" in ln for ln in _unbox(lines))
    assert not any(ln.startswith("claude-opus-4.5") for ln in _labels(lines))

    mixed = _app(_tree([_node(1, "explore", 4.0, 40, model="openai/gpt-5.4")]))
    lines = mixed.renderer._flamegraph_box(mixed.loaded[0], 100)
    assert not any("all on" in ln for ln in _unbox(lines))
    models = next(ln for ln in _labels(lines) if "gpt-5.4" in ln)
    assert models.startswith("claude-opus-4.5")  # the root's, under the root's segment


def test_only_the_top_segments_reach_the_key_and_the_rest_are_named():
    # 21 subagents is a real session. A key that long is noise, so it is capped -- and
    # the cut is stated rather than leaving unexplained unlabelled segments.
    nodes = _tree([_node(1, f"sub-{i}", 1.0, 10) for i in range(20)])
    app = _app(nodes)
    lines = app.renderer._flamegraph_box(app.loaded[0], 100)
    key = [ln for ln in _unbox(lines) if ln.startswith("█ ")]
    assert sum(ln.count("█ ") for ln in key) == app.renderer._FLAME_LEGEND_MAX
    assert any("thinner segments left out of the key" in ln for ln in lines)


def test_the_band_and_its_name_row_paint_in_the_same_per_segment_colours():
    # The chart paints through chart 1's two-pass side channel: a line is laid down once,
    # then its runs are overpainted. The names row records runs of its own, on the same
    # slots as the band, so a name is in the colour of the slice above it -- which is what
    # lets the key disappear for the segments that got named.
    nodes = _tree([_node(1, "docs", 3.0, 300), _node(1, "tests", 1.0, 100)])
    app = _app(nodes)
    r = app.renderer
    lines = r._flamegraph_box(app.loaded[0], 90)
    band = _band(lines)
    names = next(ln for ln in _labels(lines) if "root (self)" in ln)
    assert [slot for _c, _w, slot in r._token_runs[band]] == [0, 1, 2]
    assert [slot for _c, _w, slot in r._token_runs[names]] == [0, 1, 2]
    # ...and each name's run starts at its segment's own column.
    assert [c for c, _w, _s in r._token_runs[names]] == [c for c, _w, _s in r._token_runs[band]]

    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(height=20, width=120)
        r._paint_token_runs(screen, 4, 1, band, 100)
        r._paint_token_runs(screen, 5, 1, names, 100)
    finally:
        ot.curses.color_pair = orig
    for row in (4, 5):
        painted = [screen.attrs[(row, 1 + col)] for col, _w, _s in r._token_runs[band]]
        assert painted == [
            ((TOKEN_SERIES_BASE_PAIR + slot) << 8) | ot.curses.A_BOLD
            for _c, _w, slot in r._token_runs[band]
        ]
        assert len(set(painted)) == 3


def test_a_pair_starved_terminal_keeps_the_segments_apart_with_glyphs():
    # Same escape hatch chart 1 has: when _set_pair could not take the ramp, colour is
    # gone and the fill itself is the identity -- so no label is punched through it.
    nodes = _tree([_node(1, "Docs", 3.0, 300), _node(1, "Tests", 1.0, 100)])
    app = _app(nodes)
    r = app.renderer
    r._token_series_ok = False
    try:
        band = _band(r._flamegraph_box(app.loaded[0], 90))
    finally:
        r._token_series_ok = True
    assert len(set(band)) == 3  # three segments, three distinct glyphs
    assert "%" not in band and "root" not in band


def test_the_chart_sits_above_both_tables_and_takes_the_sort_header_with_it():
    # The chart is spliced in as the tab's HEAD, not prepended afterwards, because the
    # click-sortable header is registered by absolute line index -- a prefix bolted on
    # later would leave the sort regions pointing at a bar.
    nodes = _tree([_node(1, "Docs", 3.0, 300)])
    app = _app(nodes)
    r = app.renderer

    for arm in (False, True):
        if arm:
            app._model_by_root = {
                "s1": [
                    {
                        "model_name": MODEL,
                        "runs": 1,
                        "cost": 9.0,
                        "tokens_total": 1_300,
                        "input": 1_300,
                        "output": 0,
                        "cache_read": 0,
                        "cache_write": 0,
                    }
                ]
            }
            app.select_whatif_model(MODEL)
        r._line_sort_headers = {}
        lines = r.detail_subagents(app.loaded[0], 120)
        assert lines[0].startswith("┌ Where the money went")
        head = next(i for i, ln in enumerate(lines) if ln.startswith("Started"))
        assert head > 1  # the chart really is above it
        cols, target = r._line_sort_headers[head]
        assert target == "subagent" and cols == r.SUBAGENT_SORT_COLUMNS
        assert len(r._line_sort_headers) == 1  # and nothing registered on a bar


def test_a_solo_session_gets_no_chart_at_all():
    # The tab already says "No subagents used"; a one-segment icicle would add nothing
    # and imply a hierarchy that isn't there.
    app = _app([_node(0, "Root", 6.0, 1000)])
    assert app.renderer.detail_subagents(app.loaded[0], 90)[0] == "# Subagents"
    assert app.session_flame(app.loaded[0]).children == ()


def test_an_armed_whatif_target_leaves_the_chart_alone():
    # `w` is a counterfactual; the chart is recorded/estimated spend. Arming a target
    # must not restate the chart's numbers as what-ifs -- the table below owns that.
    nodes = _tree([_node(1, "Docs", 3.0, 300)])
    app = _app(nodes)
    before = app.renderer._flamegraph_box(app.loaded[0], 90)
    app.select_whatif_model("anthropic/claude-haiku-4.5")
    assert app.renderer._flamegraph_box(app.loaded[0], 90) == before


# --- the edges a second-opinion review turned up ---------------------------------


def test_the_label_ladder_ends_unique_even_against_a_literal_rank():
    # The ranked rung is not self-evidently unique: a node genuinely titled "foo #1"
    # beside two titled "foo" collides with the rank handed to one of them. A ladder
    # that almost gets there just relocates the pair you cannot tell apart.
    nodes = _tree(
        [
            _node(1, "foo", 3.0, 30),
            _node(1, "foo", 2.0, 20),
            _node(1, "foo #1", 1.0, 10),
        ]
    )
    labels = [s.label for s in _app(nodes).session_flame(_app(nodes).loaded[0]).segments]
    assert labels[1:] == ["foo #1", "foo #2", "foo #1 ·"]
    assert len(set(labels)) == len(labels)


def test_a_silent_child_never_marks_a_fully_recorded_chart_estimated():
    # "~" claims a WIDTH ON SCREEN is an estimate. An aborted $0/0-token subagent draws
    # no segment at all, so it cannot make one -- asking the question of every node
    # instead of the drawn ones put a "~" on a chart of entirely recorded dollars.
    # (One session in the real corpus is shaped exactly like this.)
    nodes = _tree([_node(1, "Docs", 3.0, 300), _node(1, "Aborted", 0.0, 0)])
    app = _app(nodes, api=True)
    flame = app.session_flame(app.loaded[0])
    assert flame.silent == 1 and not flame.estimated
    assert "~" not in app.renderer._flamegraph_box(app.loaded[0], 90)[0]
    # ...but a child that WAS drawn from an estimate still marks it.
    est = _app(_tree([_node(1, "Docs", 0.0, 500_000)]), api=True)
    assert est.session_flame(est.loaded[0]).estimated


def test_a_share_is_never_rounded_into_a_lie_at_either_end():
    # 65 segments in the real corpus are under half a percent, and they are exactly the
    # ones a "biggest …" line might name. Rounding them to "0%" describes a segment that
    # is visibly there as nothing -- and rounding the root to a flat "100%" contradicts
    # the subagent segments printed right beside it.
    nodes = _tree([_node(1, "Tiny", 1.0, 10), _node(1, "Tinier", 1.0, 10)], root_cost=9998.0)
    app = _app(nodes)
    head = _unbox(app.renderer._flamegraph_box(app.loaded[0], 100))[0]
    assert head.startswith("root kept >99%")
    assert "biggest Tiny <1%" in head
    # Only an exact whole is 100%, and only an exact nothing is 0%.
    assert app.renderer._flame_pct(1.0) == "100%"
    assert app.renderer._flame_pct(0.0) == "0%"
    # Half-up, like the page's Math.round -- Python's round() would answer "12%" here
    # and the two frontends would print different numbers for the same segment.
    assert app.renderer._flame_pct(0.125) == "13%"


def test_more_segments_than_cells_never_overflows_the_band():
    # The one-cell floor is what keeps a paid segment visible, but it is unpayable once
    # the segments outnumber the columns -- and paying it anyway makes the line longer
    # than the pane, which the box CLIPS, silently dropping whatever sits at the right
    # edge. Below that the chart drops out entirely and the sentence carries it.
    app = _app(_tree([_node(1, f"a{i}", 1.0, 10) for i in range(40)]))
    r = app.renderer
    assert not _band(r._flamegraph_box(app.loaded[0], 40))  # 41 segments, 36 cells
    # The shared primitive holds the line to `cells` even when asked directly.
    rows = [(f"S{i}", 1.0, i % 5) for i in range(31)]
    assert len(r._token_stack_line(rows, 31.0, 30)) == 30


def test_legend_labels_stay_distinct_after_being_clipped():
    # App._flame_labels guarantees the FULL names are unique; the legend's 24-char clip
    # can undo that, and two identical legend LINES collide in _token_runs (keyed by line
    # text) -- the second overwrites the first's runs and both swatches paint in the
    # second segment's colour.
    long_a = "Review the storyboard cache invalidation"
    long_b = "Review the storyboard cache eviction"
    app = _app(_tree([_node(1, long_a, 3.0, 30), _node(1, long_b, 2.0, 20)]))
    r = app.renderer
    lines = r._flamegraph_box(app.loaded[0], 44)
    key = [ln for ln in _unbox(lines) if ln.startswith("█ ")]
    assert len(key) == len(set(key))  # the collision is between whole LINES
    slots = [r._token_runs[ln][0][2] for ln in key]
    assert slots == sorted(set(slots))  # ...so each swatch keeps its own colour


def test_a_label_ending_in_a_pipe_still_gets_its_colour():
    # The boxed-line lookup used to rstrip("│|"), which ate the last character of any
    # label ending in a pipe -- the key missed and that swatch painted in plain ink.
    # Titles are user text; the frame is the only thing that may be stripped.
    app = _app(_tree([_node(1, "make | work", 3.0, 30)]))
    r = app.renderer
    lines = r._flamegraph_box(app.loaded[0], 44)
    boxed = next(ln for ln in lines if ln[:1] == "│" and "make | work" in ln)
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        screen = AttrScreen(height=20, width=80)
        r._paint_token_runs(screen, 2, 0, boxed, len(boxed))
    finally:
        ot.curses.color_pair = orig
    # The swatch sits at content column 0, shifted right by the box gutter.
    assert screen.attrs[(2, 2)] == ((TOKEN_SERIES_BASE_PAIR + 0) << 8) | ot.curses.A_BOLD


def test_the_shared_bar_builder_is_byte_identical_for_chart_ones_five_segments():
    # _token_stack_line grew two parameters and a floor escape hatch for this chart.
    # Chart 1 (Token economics) calls it with neither, and its five-segment bars must
    # come out unchanged to the byte -- so the pre-change algorithm is replayed here and
    # diffed across a seeded sweep of widths, zero rows and lopsided splits.
    app = _app(_tree([_node(1, "Docs", 3.0, 300)]))
    r = app.renderer

    def before(rows, total, cells):
        floor = sum(1 for _, v, _ in rows if v > 0)
        room = max(0, cells - floor)
        widths, acc, used = [], 0.0, 0
        for _label, v, _slot in rows:
            if total > 0:
                acc += v / total
            edge = min(room, round(acc * room))
            widths.append(max(0, edge - used) + (1 if v > 0 else 0))
            used = edge
        short = cells - sum(widths)
        if short > 0 and widths:
            widths[widths.index(max(widths))] += short
        text = ""
        for (_label, v, slot), w in zip(rows, widths):
            if w <= 0:
                continue
            glyph = r._token_glyph(slot)
            share = f"{round(100.0 * v / total)}%" if total > 0 else ""
            fits = r._token_series_ok and share and len(share) + 2 <= w
            text += share.center(w, glyph) if fits else glyph * w
        return text

    rng = random.Random(7)
    for _ in range(1500):
        n = rng.randint(1, 5)  # chart 1 has five token types and never more
        rows = [
            (f"t{i}", rng.choice([0.0, rng.random() * 10 ** rng.randint(0, 6)]), i)
            for i in range(n)
        ]
        total = sum(v for _label, v, _slot in rows)
        cells = rng.randint(5, 140)
        assert r._token_stack_line(rows, total, cells) == before(rows, total, cells)


def test_the_bar_is_always_exactly_as_wide_as_the_pane():
    # The invariant the floor escape hatch exists to protect: whatever the segment count,
    # the line is exactly `cells` wide. A longer one is clipped by the box, which drops
    # whatever sits at the right edge without saying so.
    app = _app(_tree([_node(1, "Docs", 3.0, 300)]))
    r = app.renderer
    rng = random.Random(11)
    for _ in range(1000):
        rows = [(f"t{i}", rng.random() * 100, i % 5) for i in range(rng.randint(1, 60))]
        cells = rng.randint(5, 140)
        assert len(r._token_stack_line(rows, sum(v for _l, v, _s in rows), cells)) == cells


def test_the_boxed_line_lookup_handles_an_ascii_frame_and_a_runt_line():
    # The gutter path also runs on the locale-independent ASCII frame ("| … |"), and it
    # must not index off the end of a line too short to be one.
    app = _app(_tree([_node(1, "Docs", 3.0, 300)]))
    r = app.renderer
    orig = ot.curses.color_pair
    ot.curses.color_pair = lambda n: n << 8
    try:
        for line, recorded in (("| ███ inside      |", "███ inside"), ("|  |", None), ("|", None)):
            r._token_runs = {recorded: [(0, 3, 1)]} if recorded else {}
            screen = AttrScreen(height=8, width=60)
            r._paint_token_runs(screen, 1, 0, line, len(line) + 5)
            want = ((TOKEN_SERIES_BASE_PAIR + 1) << 8) | ot.curses.A_BOLD if recorded else None
            assert screen.attrs.get((1, 2)) == want, line
    finally:
        ot.curses.color_pair = orig


def test_tied_segments_order_by_code_point_descending():
    # The tie-break's whole job is determinism across the two frontends, so pin the
    # Python contract the JS mirrors: a (value, tokens, title) tuple sorted reverse, i.e.
    # title descending by CODE POINT. Both cases here are ones a JS shortcut gets wrong --
    # localeCompare reverses "Z"/"a", and a bare `<` reverses an emoji against a high BMP
    # character (it compares UTF-16 code units).
    def order(*titles):
        # The sort key is the node's TITLE; each node's agent is set to it too, so the
        # resulting order reads straight off the segments.
        nodes = _tree([_node(1, t, 1.0, 5, title=t) for t in titles])
        return [g.agent for g in _app(nodes).session_flame(_app(nodes).loaded[0]).children]

    assert order("Z", "a") == ["a", "Z"]
    assert order("\U0001f600", "�") == ["\U0001f600", "�"]


def test_a_title_that_is_a_dict_method_name_still_gets_disambiguated():
    # Python counts with list.count() and is immune, but the JS mirror counted into a
    # plain object, where "constructor"/"toString"/"__proto__" read Object.prototype
    # instead of a count and two identical labels were never detected. Pin the expected
    # answer here so the two frontends can be compared on it.
    for name in ("constructor", "toString", "__proto__"):
        nodes = _tree([_node(1, name, 3.0, 30), _node(1, name, 2.0, 20)])
        labels = [s.label for s in _app(nodes).session_flame(_app(nodes).loaded[0]).children]
        assert labels == [f"{name} #1", f"{name} #2"], name
