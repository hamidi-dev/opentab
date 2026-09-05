# TUI Contributor Guide

The terminal frontend separates navigation and data selection from painting, but
keeps them close enough to share table layouts and test interactions without a
real terminal. This guide explains those boundaries and the invariants behind
them, rather than listing every key or every screen.

See [Keys and navigation](keys.md) for user controls, [Architecture](architecture.md)
for the store contract, [Caching](caching.md) for loading and invalidation,
[Pricing](pricing.md) for cost semantics, and [Privacy](privacy.md) for content and
export boundaries. Public controls call data backends **harnesses** (`H`,
`--harness`); internal names such as `source_key`, `zoom_source`, `source_menu`,
and `sources.py` retain their existing spelling.

## App and Renderer

[`App`](../src/opentab/tui/app.py) owns view state, selection, keyboard and mouse
dispatch, and memoized data. [`Renderer`](../src/opentab/tui/renderer.py) owns
layout, painting, and the hit regions produced by the current frame.
`Renderer.__getattr__` delegates missing attributes to its `App`: a renderer can
read `self.current_sessions()` or `self.tab` through that shared interface.
Assignments do not delegate; state changes in rendering code use `self.app`.

Most detail builders return `list[str]`, while scrolling pickers paint directly.
Ordinary lines acquire money/token colors through `write_rich` at paint time.
Structured side channels attach chart spans, header identities, and cursor
positions without requiring the data builders to initialize curses. `TraceLine`
adds semantic roles to transcript strings, which need a different paint path.

Both classes consume store methods and per-session capability checks, not SQL or
transcript formats. `CombinedStore` routes those checks to the owning backend.
`current_tabs()` supplies the actual tabs: it adds supported session extras,
Harnesses for a merged scope, and Machines where a fleet breakdown is useful.
Dispatch by tab name rather than assuming a class tuple's index is still valid.

## Views and Scope

`App.view` has three levels:

| View | Layout and selection |
| --- | --- |
| `browse` | Sidebar active, detail pane previews its selection. |
| `zoom` | Detail active, normally beside an inactive but clickable sidebar. |
| `session` | One session's detail occupies the full body width. |

`zoom_maximized` controls whether **zoom** hides its sidebar. It is a global
preference saved and restored by `state.py`, not a transient flag reset on entry.
The session layout is full-screen regardless of that flag; changing it there
affects the zoom layout on return. A browse preview's trailing `detail` click
region focuses it, but its more specific table and tab regions take precedence.

`BROWSE_MODES` describes Time, Projects, and Machines, including their labels,
actions, and hierarchical/flat distinction. It feeds the mode strip, help/footer,
and restored-mode validation. Time has a Years/Months/Days hierarchy; Projects
and Machines have flat sidebars. Their different geometry stays in explicit
drawers rather than being forced into one generic panel implementation.

`mode_scope_workflows()` answers which sessions the sidebar selection covers.
`current_sessions()` applies in-scope drills and filtering to that base. A picker
must rank the same scope its selected row will open; otherwise its totals can
describe a different set of sessions from the resulting list.
The underlying `ranged_workflows` and `all_workflows` are cached; changes to range
or ignored state invalidate them through `_invalidate_workflow_cache()`.

### Composing Drills

Project, harness, and machine drills select partitions. A model drill selects
membership: one session may have used several models. Outside Machines mode,
these selections compose, with the model innermost. In Machines mode the
Harnesses, Projects, and Models drills are mutually exclusive, so each picker
ranks the selected machine scope rather than a drill the next pick will discard.

`_zoom_picker_scope()` applies the other armed dimensions while excluding the
dimension being picked. `compose_zoom_drills()` provides the corresponding
partition filtering for the Models table. Models deliberately filters model
names, not session titles; `zoom_model_rows()` and the renderer use matching
aggregation and filtering so a cursor ordinal refers to the row actually drawn.

Each drill couples a value with a picker cursor. Clearing helpers reset the pair;
`project_index` needs special care because in Projects mode it is the **sidebar**
selection, not an in-zoom picker. Range changes capture the selection before
clearing drills, since clearing can widen the list containing the selected row.

`_drilled()` is the last safety net: if a nonempty scope has no match for an armed
drill, it disarms that drill instead of leaving a stuck empty list. An already
empty scope does not blame the drill. `Renderer.draw()` calls `settle_drills()`
before painting, so breadcrumbs cannot describe a selection the body just dropped.
Sidebar movement, reloads, and range changes also clear drills eagerly where
appropriate. Wheel handling respects the focused scope, not just the hovered pane.

### Keeping Selection Stable

Indices are useful within a displayed list but are not identities across a
rebuild. `SelectionAnchor` stores year, month, day, project, machine, and session
values. `restore_selection()` resolves parents before children because each
parent changes the next list. Movement clamps an old cursor before stepping it.

Returning from a drill uses `_reanchor()` to find the selected value in the
**current** ranking. This matters after a price-mode toggle or reload reorders
rows: returning to ordinal 2 is not necessarily returning to the same model.
Model drills pop before partition drills; leaving a session returns to the
scope's Sessions tab. Trends keeps its own return context for journeys out of
the overlay and back.

## Model Contribution Scopes

Selecting a Models row arms `zoom_model` and opens **Economics**, followed by
**Sessions**. This is more than a session filter: every model-specific metric
answers what that model contributed within the enclosing year, month, project,
or machine scope. The model-name filter is cleared on entry so it does not become
an unrelated session-title query.

Economics combines `model_scope_usage()` with the shared token-economics card
narrowed to the model. Sessions uses `model_session_usage()` for **Model list**
and **Model tok**, rather than displaying each matching session's entire bill.
Sorting uses those attributed values, and export includes them alongside the
whole-session totals. `session_metric_labels` feeds both displayed headers and
click-to-sort keys, keeping a renamed column clickable.

The attributed dollars are always **list-rate calculations** from per-model token
rows, including the one-hour cache-write subset. They are not a decomposition of
recorded spend. Unknown rates receive an estimate marker. Local models receive
no list-rate charge: Economics labels them explicitly, while Sessions shows $0.
`$` does not change these model-specific columns.

Elsewhere, `$` selects the recorded/API-equivalent cost snapshots. API-equivalent
pricing starts on outside demo, unless a saved preference overrides it. Header
labels and subscription hints use the store's `records_cost` capability, not a
scan for whether all visible rows cost zero. The session-only `w` comparison is
independent of both the model drill and that global price mode; the arithmetic
and caveats belong in [Pricing](pricing.md).

## Input and Overlays

Overlays preserve the underlying view. Keyboard routing in `handle_key` gives
ownership to the highest active context, approximately in this order:

1. Mouse/resize events, then blocking startup warnings and the price prompt.
2. Theme, demo, source, machine, harness, and what-if pickers.
3. Help and notice history.
4. Prices and Trends, including their sort, filter, and drill contexts.
5. Ordinary sort/filter/launch input, then the main view.

Painting and mouse handling follow the same ownership model. A modal must not
leak clicks to a table behind it merely because that table registered a region
first. Regions are rebuilt each frame. Generic hit testing is first-match, so a
catch-all region belongs after the specific controls it surrounds.

Trends and Prices close explicitly and swallow unrelated keys. Their common-key
handlers keep supported global actions available, allowing a theme picker to
preview an overlay without closing it. Charts have a separate focused context so
arrows can select chart elements without permanently trapping tab navigation.
Harness/demo changes re-anchor overlay selections through the reload path.

`bindings.py` resolves actions by context; `keymap.py` describes contextual help
and footer hints. This lets remapping change the action and its advertised key
together. `get_wch` reads characters rather than UTF-8 bytes; `_read_key` returns
ASCII and special keys as integers, other characters as strings. Both forms go
through action lookup, so non-ASCII keys can be bound as well as typed. Text
prompts budget input length separately from visible width and scroll long input.

## Shared Table Geometry

Tables use ruled boxes whether they are static summaries, line-based selectable
tabs, or scrolling pickers. `box_top`, `box_rule`, and `box_row` are the common
pieces. `_ruled_box` and `_sectioned_box` assemble line lists;
`draw_picker_frame` surrounds a scrolling window. A title in the top border
avoids adding an extra heading row when a preview becomes interactive.

`BOX_CHROME = 4` reserves horizontal space for borders and padding.
`PICKER_CHROME = 4` reserves vertical frame space from a picker's row budget.
These are different dimensions despite sharing a number. Boxed table content
also carries a two-cell marker gutter, keeping adjacent tables aligned.

Sessions preview and picker share `session_columns`, `session_header_text`, and
`session_row_text`; Projects shares its header and row builders too. Optional
session columns use the same pane budget in both views: Models, Project, then
Worked drop as width shrinks, protecting the title. Models uses `_model_table`
in both views: zoom adds a cursor, not a replacement table with fewer columns.

`paint_cursor_row` reverses only the interior, preserving the box rules and the
selection marker. It uses `write`, not `write_rich`, so colored numeric spans
cannot overwrite the highlight. Headers paint bold accent text, not filled bands.
`_mark_box_header` records framed header text rather than a fragile line offset;
selectable row maps use the derived `_ruled_body_start` when locating table bodies.

Multi-row tables normally close with a rule and TOTAL row. A single row needs no
duplicate total, and a Top-N slice must not pretend to total the whole scope.
The what-if Subagents footer is a session-level comparison, not a sum of its
ordinary Cost column. Overview sections place the widest Models table last.

The Tools treemap summarizes the same rows as its table. Area follows the current
cost mode, falling back to tokens when all costs are zero. Shade shows cost per
call, or tokens per call in token mode, on a log scale over the full ranking;
missing or uniform per-call rates fall back to area. Small entries fold into
`Other` until each tile can carry its label. The TUI uses theme heat-background
pairs while the browser measures its responsive container. Neither chart needs
another store query.

## Coordinates and Encoding

The outer app frame is a viewport boundary. `draw()` paints it in screen
coordinates, then sets `oy = ox = 1` and passes the inner dimensions to drawers.
`write`, `hline`, and `frame` translate at the curses boundary. Mouse coordinates
subtract the origin exactly once in `handle_mouse`; regions and sort geometry
remain in content coordinates. Standalone drawer tests start with a zero origin.

Widths are terminal **cells**, not Python string lengths. `display_width`, `clip`,
and `wrap_cells` prevent wide characters from overflowing or disappearing at the
right edge. Prompt input handles the viewport inset separately because it paints
from `App`. Pager sizing shares the renderer's chrome budget.

App and panel frames use heavy Unicode box glyphs when `unicode_screen()` permits
them, otherwise curses ACS lines; string-built tables have an ASCII fallback.
Checking the locale first matters: wide curses can silently draw garbage instead
of raising an encoding error. Multibyte frame glyphs go through `addch`/`addstr`,
not the byte-sized `hline`/`vline` interface. The lower-right screen cell may draw
successfully and then raise when curses cannot advance its cursor; that specific
corner error is harmless.

## Themes and Palettes

`themes.py` supplies semantic roles and heat ramps to both frontends. The web
uses CSS variables; `init_theme_colors()` maps the same roles to fixed curses
pairs. On capable terminals an explicit background pair fills the screen before
erase, making light themes genuinely light. Theme changes reuse palette slots.

The exact-color path redefines palette entries with `init_color`; it is not
direct truecolor SGR output. `_slot` reserves indices with bit 3 clear and
`_write_color` writes both the primary and its `+8` twin. This accommodates
terminals that apply "bold is bright" beyond the base ANSI palette.

`can_change_color()` reports a terminfo claim, not proof that palette writes
reach the display. `palette_writes_ignored()` detects known hosts such as herdr.
`OPENTAB_NO_INIT_COLOR=1` forces approximation; `=0` overrides detection in the
other direction. This terminal-specific environment choice is not persisted.

The 256-color fallback searches perceptual CIE L*a*b* distance. Distinct role
colors claim separate indices, with background allocated first; heat ramps may
share indices. Limited terminals use nearest ANSI colors or monochrome, and
`_set_pair` checks the available pair count. Fewer colors should reduce visual
detail, not prevent startup. Glyphs, bold, and selection reversal still carry meaning.

## Lazy Session Detail

Startup loads workflows first and defers the model breakdown until after the
first frame; startup warnings can take input before that scan. Per-model rows
then live in `_model_by_root` for slicing, not repeated store queries.

Nodes, Turns, Tools, and optional Context composition are session-level reads.
`draw_detail` paints a loading frame when `session_data_ready()` is false, even
avoiding tab capability checks that might parse data. The `run()` prefetch tick
does the blocking work and repaints. These numeric memos clear on reload and
harness changes; backend caching details live in [Caching](caching.md).

Turns groups chronological assistant steps under their owning prompts, with
subagent steps interleaved. Prompt drill-down exposes full prompt text and a
selectable list of its turns. Tools, Agents, effort, and Content columns depend
on available row data; content flags advertise only an openable trace and never
trigger a content fetch just to draw a marker. Tools attribution means usage in
steps that invoked a tool, not the size of the tool's output.

Context's measured curve uses main-thread `input + cache_read + cache_write`.
Subagents have their own windows. Curve support is separate from optional
composition support; cumulative-delta backends such as Codex cannot provide
per-request context sizes. Compaction markers are heuristic drops using the
shared thresholds also used by Turns. The chart adds spend and elapsed-time
context; mixed-window sessions scale the chart to the last model while peak
percentage uses the peak turn's window. Composition is a chars/4 estimate, not
tokenizer output. Unlogged system prompts and tool schemas cannot be split out
of the measured first-turn baseline.

## The Turn Reader

A trace is a third level under Turns: prompts explain **when**, their turns
explain **which calls**, and a selected turn explains **what happened**. Its
`content_key` comes from the source record, so separately loaded usage and content
agree. Events contain narration, recorded reasoning, or tool arguments/results.
Missing reasoning text is explained using the harness's `records_reasoning` flag.

Content is excluded from session prefetch. `turn_content(id)` returns capped
session previews through `TraceContent`; supplying `content_key` requests full
content for that turn only. Both reads paint a loading frame first. The app keeps
at most four session previews and one full turn, separately from numeric memos
and the warm-start cache. Full content is temporary, released on navigation,
whole-turn collapse, reload, or harness changes.

Individual tool outputs expand independently using that same one-turn read.
Only opened outputs substitute their full event content; neighboring outputs
and arguments retain preview limits. Enter targets the output at the viewport
top or the next below it; mouse regions identify an event directly. Expansion
anchors scrolling to the section. Whole-turn expansion is a separate action.

The renderer preserves argument/result spacing with cell-based hard wrapping
(tabs display as four spaces). Output previews collapse blank runs and budget
six **screen rows**, including wrapped lines, with omitted lines/characters
reported separately. Full output restores the recorded blank lines. Narration
and reasoning use a 100-cell reading measure and limited Markdown presentation:
headings and bold delimiters, with inline code protected and fenced code kept raw.
Commands and results never undergo Markdown or numeric highlighting.

`TraceLine` roles survive wrapping and scrolling. Tool gutters separate arguments
from labeled output, recorded errors are explicit, and reader chrome quiets the
aggregate totals while retaining prompt/turn identity. The footer paints after
the body because its expansion action depends on the output sections just laid
out. Trace content stays local to the TUI, unavailable in demo or remote
summaries and absent from web payloads; see [Privacy](privacy.md).

## Launching a Session

`L` prepares a resume command; the chosen launcher decides where to run it. Command
construction and transport stay in `util.py`, not in the renderer. The displayed
and copied command should remain the same command passed to the launcher, including
one quoted remote-command argument for SSH. See [custom launchers](keys.md#custom-launchers).

Herdr integration uses its CLI rather than a socket. A tab uses `herdr tab create`;
a split uses `herdr pane split --pane "$HERDR_PANE_ID"` to target the pane actually
hosting OpenTab, not whichever pane happens to be current elsewhere. The response
supplies `result.root_pane.pane_id` or `result.pane.pane_id`, then `herdr pane run`
starts the command. Missing pane identity disables splits, and Herdr has no popup
target. Demo disables session launch and copy before they reach any launcher.
