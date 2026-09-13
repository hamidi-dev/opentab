from types import SimpleNamespace

from opentab.tui.components.boxes import TABLE_GLYPHS_ASCII
from opentab.tui.views import subagents


def _node(index, depth, cost, title, agent="explore"):
    return {
        "_node_index": index,
        "depth": depth,
        "agent": agent,
        "model_name": "anthropic/claude-opus-4.5",
        "title": title,
        "created_at": f"2026-06-01 12:0{index}:00",
        "cost": cost,
        "tokens_input": 100,
        "tokens_output": 200,
        "tokens_reasoning": 50,
        "tokens_cache_read": 800,
        "tokens_cache_write": 100,
        "tokens_total": 1250,
    }


def _flame():
    root = SimpleNamespace(
        label="root (self)",
        agent="root (self)",
        model="claude-opus-4.5",
        value=4.0,
        share=0.5,
        slot=0,
        depth=0,
    )
    child = SimpleNamespace(
        label="explore",
        agent="explore",
        model="claude-opus-4.5",
        value=4.0,
        share=0.5,
        slot=1,
        depth=1,
    )
    return SimpleNamespace(
        segments=(root, child),
        children=(child,),
        total=8.0,
        unit="cost",
        estimated=False,
        deep=0,
        silent=0,
        self_share=0.5,
        one_model="claude-opus-4.5",
    )


def test_execution_table_returns_local_interaction_geometry_by_snapshot_index():
    rows = [_node(7, 1, 3.0, "first"), _node(2, 2, 1.0, "second")]
    columns = (("cost", "Cost"), ("title", "Title"))
    layout = subagents.execution_table_layout(
        rows,
        120,
        offset=9,
        tree_cost=8.0,
        glyphs=TABLE_GLYPHS_ASCII,
        sort_headings={"cost": "Cost v", "title": "Title"},
        sort_columns=columns,
        selected_node_index=2,
    )

    assert layout.selected_node_index == 2
    assert layout.cursor_line == layout.row_map[1][0]
    assert [ordinal for _line, ordinal in layout.row_map] == [0, 1]
    assert layout.sort_headers == ((10, columns, "subagent"),)
    assert layout.headers and "Cost v" in layout.headers[0]


def test_overview_uses_explicit_flame_and_whatif_values_and_returns_spans():
    root, child = _node(0, 0, 4.0, "root"), _node(1, 1, 4.0, "delegated")
    columns = (("cost", "Cost"), ("title", "Title"))
    layout = subagents.subagents_overview_layout(
        priced_nodes=[root, child],
        rows=[root, child],
        flame=_flame(),
        width=120,
        glyphs=TABLE_GLYPHS_ASCII,
        colored=True,
        api_prices_key="$",
        select_key="Enter",
        sort_headings={"cost": "Cost", "title": "Title"},
        sort_columns=columns,
        selected_node_index=1,
        target="anthropic/claude-haiku-4.5",
        whatif_totals=(9.0, 3.0),
        whatif_prices={0: 1.0, 1: 2.0},
    )

    text = "\n".join(layout.lines)
    assert "Delegated cost $4.00 (50% of tree)" in text
    assert "your models $9.00" in text and "saved $6.00" in text
    assert layout.token_spans and layout.row_map and layout.sort_headers
    assert layout.selected_node_index == 1


def test_execution_detail_renders_resolved_prompt_and_availability_without_readers():
    root, child = _node(0, 0, 4.0, "root"), _node(1, 1, 3.0, "full child title")
    layout = subagents.subagent_detail_layout(
        child,
        [root, child],
        76,
        glyphs=TABLE_GLYPHS_ASCII,
        back_key="Esc",
        select_key="Enter",
        turns_unavailable="Execution turns are not supported by this harness.",
        prompt_text="Loading received prompt...",
        cost_label="Recorded",
        colored=False,
    )

    text = "\n".join(layout.lines)
    assert "full child title" in text
    assert "Loading received prompt..." in text
    assert "Execution turns are not supported" in text
    assert "Enter: open" not in text
    assert layout.token_spans
