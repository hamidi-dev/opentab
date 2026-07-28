"""User-configurable key bindings: the registry, the conf file, the resolver.

Every key the TUI answers to is an *action* in a *context* here -- `handle_key` and
the menu handlers never compare against a character again; they ask the active
`Keymap` "what action is this key, where I am?" and switch on the answer. That one
indirection is the whole feature: rebind anything by editing
`~/.config/opentab/keymap.conf` (the `K` key opens it in $EDITOR and reloads on
return), and the help overlay / footer chips re-label themselves from the live
table, because they read the same resolver.

Three layers, one source of truth:

- REGISTRY -- every context, its actions, their default keys and one-line docs.
  The shipped conf file is *generated* from it (default_conf_text), so file and
  code cannot drift; a release that adds an action needs no conf migration, the
  resolver falls back to the registry default for anything the file doesn't name.
- keymap.conf -- the user's overrides, INI-style, fully commented. Only what it
  names changes; an empty value unbinds; unknown names warn and fall back rather
  than break the TUI (a typo must never lock you out of the tool that would show
  you the typo).
- Keymap -- the composed result: per context, a flat key-code -> action map. User
  bindings always beat defaults (binding `x` to `down` really moves `x` to it,
  even though `x` was `clear_filter`'s); two *user* claims on one key warn, last
  one wins.

Contexts are keyboard states, not screens: a focused Trends chart, a picker, the
filter line each own the keyboard while they're up, so each is a context. Three
families share keys through a fallback section -- `menu.*` -> `[menu]` (one place
to re-teach every picker j/k), `trends.*` -> `[trends]`, `prices.sessions` ->
`[prices]` -- resolved per action: a sub-context that defines `back` itself keeps
it, everything it doesn't define flows down.

Ctrl-C is deliberately NOT here: it is the hardwired panic quit in every context,
because a keymap that can rebind away the exit is a trap, not a feature.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from opentab import paths

try:
    import curses
except ImportError:  # native Windows has no stdlib curses; specs fall back to ncurses codes
    curses = None


def keymap_path() -> str:
    # Genuine user config (hand-edited, hand-committed) -> the XDG config dir.
    return os.path.join(paths.config_dir(), "keymap.conf")


# --- key specs ----------------------------------------------------------------------
# A spec is what the conf file says ("j", "enter", "ctrl-u", "shift-tab", "ö"); a code
# is what _read_key hands the handlers (an int, or a str for one non-ASCII character).
# One spec can carry several codes -- Enter really is three different codes at the
# terminal -- and the pretty form is what the help overlay and footer print for it.


def _kc(name: str, fallback: int) -> int:
    # A curses KEY_* constant, or its standard ncurses value where curses is absent
    # (native Windows) -- the resolver must compose there too, the web path imports it.
    return getattr(curses, name, fallback) if curses else fallback


_NAMED: dict[str, tuple[int, ...]] = {
    "enter": (10, 13, _kc("KEY_ENTER", 343)),
    "esc": (27,),
    "space": (32,),
    "tab": (9,),
    "shift-tab": (_kc("KEY_BTAB", 353),),
    "backspace": (_kc("KEY_BACKSPACE", 263), 127, 8),
    "delete": (_kc("KEY_DC", 330),),
    "insert": (_kc("KEY_IC", 331),),
    "up": (_kc("KEY_UP", 259),),
    "down": (_kc("KEY_DOWN", 258),),
    "left": (_kc("KEY_LEFT", 260),),
    "right": (_kc("KEY_RIGHT", 261),),
    "pgup": (_kc("KEY_PPAGE", 339),),
    "pgdn": (_kc("KEY_NPAGE", 338),),
    "home": (_kc("KEY_HOME", 262),),
    "end": (_kc("KEY_END", 360),),
    "comma": (44,),  # "," itself is the list separator in the conf file
}
_ALIASES = {
    "escape": "esc",
    "return": "enter",
    "backtab": "shift-tab",
    "s-tab": "shift-tab",
    "bksp": "backspace",
    "del": "delete",
    "ins": "insert",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "npage": "pgdn",
    "ppage": "pgup",
}
for _n in range(1, 13):  # f1..f12
    _NAMED[f"f{_n}"] = (_kc("KEY_F0", 264) + _n,)

_PRETTY: dict[str, str] = {
    "enter": "Enter",
    "esc": "Esc",
    "space": "Space",
    "tab": "Tab",
    "shift-tab": "S-Tab",
    "backspace": "Bksp",
    "delete": "Del",
    "insert": "Ins",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "pgup": "PgUp",
    "pgdn": "PgDn",
    "home": "Home",
    "end": "End",
    "comma": ",",
}

RESERVED_CTRL_C = 3  # the panic quit, hardwired in every handler; never bindable


def parse_key(spec: str) -> tuple[int | str, ...]:
    """One spec token -> its key codes. Raises ValueError on anything it can't read."""
    token = spec.strip()
    if not token:
        raise ValueError("empty key")
    low = token.lower()
    low = _ALIASES.get(low, low)
    if low in _NAMED:
        return _NAMED[low]
    if low.startswith("ctrl-") or low.startswith("^"):
        ch = token[5:] if low.startswith("ctrl-") else token[1:]
        if len(ch) != 1 or not ch.isalpha() or not ch.isascii():
            raise ValueError(f"ctrl- takes one letter, got {spec!r}")
        code = ord(ch.lower()) & 0x1F
        if code == RESERVED_CTRL_C:
            raise ValueError("ctrl-c is the hardwired quit; it cannot be rebound")
        return (code,)
    if len(token) == 1 and not token.isspace():
        # A literal character, case-sensitive ("S" is shift-s). ASCII arrives as an
        # int code; anything wider arrives from get_wch as the character itself.
        return (ord(token),) if token.isascii() else (token,)
    raise ValueError(f"unknown key {spec!r}")


def pretty_key(spec: str) -> str:
    """The display form of one spec token -- what the help overlay and footer print."""
    token = spec.strip()
    low = _ALIASES.get(token.lower(), token.lower())
    if low in _PRETTY:
        return _PRETTY[low]
    if low.startswith("f") and low[1:].isdigit():
        return low.upper()
    if low.startswith("ctrl-") or low.startswith("^"):
        ch = token[5:] if low.startswith("ctrl-") else token[1:]
        return f"^{ch.upper()}"
    return token


# --- the registry -------------------------------------------------------------------
# Order matters twice: it is the order the generated conf file lists things in, and
# the order actions claim keys during composition (user lines still beat defaults).


class Action(NamedTuple):
    name: str
    keys: tuple[str, ...]  # default specs; () = deliberately unbound by default
    doc: str  # ONE short line; it becomes the comment above the conf line


class Context(NamedTuple):
    name: str  # the conf section, dotted for sub-states ("menu.theme")
    doc: str  # the comment block opening the section
    fallback: str | None  # actions not defined here flow down to this context
    actions: tuple[Action, ...]


_SCROLL = (  # the pager keys every list/overlay shares, spelled once
    Action("down", ("j", "down"), "move / scroll down"),
    Action("up", ("k", "up"), "move / scroll up"),
    Action("page_down", ("pgdn", "ctrl-d"), "half a page down"),
    Action("page_up", ("pgup", "ctrl-u"), "half a page up"),
    Action("top", ("g",), "jump to the top"),
    Action("bottom", ("G",), "jump to the bottom"),
)

REGISTRY: tuple[Context, ...] = (
    Context(
        "main",
        "The main views: browse -> zoom -> session. Everything outside an overlay.",
        None,
        (
            Action("select", ("enter",), "drill into the selection / fold a ▸ prompt"),
            Action("back", ("esc", "backspace"), "step back out — session → zoom → browse"),
            *_SCROLL,
            Action("tab_prev", ("h", "left"), "previous detail tab"),
            Action("tab_next", ("l", "right"), "next detail tab"),
            Action("cycle_panel", ("tab",), "cycle the sidebar panels"),
            Action("cycle_panel_back", ("shift-tab",), "cycle panels back / step out"),
            Action("panel_1", ("1",), "jump to sidebar panel 1"),
            Action("panel_2", ("2",), "jump to sidebar panel 2"),
            Action("panel_3", ("3",), "jump to sidebar panel 3"),
            Action("panel_detail", ("0",), "jump to the detail pane"),
            Action("mode_time", ("t",), "Time browse mode"),
            Action("mode_projects", ("p",), "Projects browse mode"),
            Action("mode_machines", ("m",), "Machines browse mode"),
            Action("maximize", ("+",), "maximize / restore the detail pane"),
            Action("sort", ("s", "S"), "sort this list"),
            Action("filter", ("f", "/"), "filter — fuzzy over titles, projects, notes"),
            Action("clear_filter", ("x",), "clear the filter"),
            Action("ignore", ("i",), "ignore / unignore the selection"),
            Action("show_ignored", ("I",), "show ignored rows"),
            Action("bookmark", ("b",), "bookmark ★ this session"),
            Action("show_bookmarks", ("B",), "show only bookmarked sessions"),
            Action("note", ("n",), "note ✎ this session"),
            Action("fold_turns", ("z",), "unfold / fold every ▸ prompt (Turns tab)"),
            Action("export", ("e",), "export this list to CSV"),
            Action("open_dir", ("o",), "open the selection's directory"),
            Action("launch", ("L",), "resume this session in its own tool"),
            Action("whatif", ("w",), "what-if — reprice a session at one model"),
            Action("range", ("R",), "set the date range"),
            Action("all_time", ("a",), "all time"),
            Action("reload", ("r",), "reload the data"),
            Action("refresh_machines", ("F",), "re-pull machine summaries over ssh (fleet)"),
            Action("harness", ("H",), "switch harness / filter harness (fleet)"),
            Action("machine", ("M",), "filter every view to one machine (fleet)"),
            Action("theme", ("C",), "colour theme picker"),
            Action("demo", ("D",), "anonymize for a screenshot"),
            Action("api_prices", ("$",), "price subscription usage at API list rates"),
            Action("trends", ("T",), "trends — charts, calendar heatmap, rankings"),
            Action("prices", ("P",), "model prices overlay"),
            Action("notices", ("N",), "notifications — reread the toasts that faded"),
            Action("help", ("?",), "the key cheat sheet"),
            Action("edit_keymap", ("K",), "edit these bindings in $EDITOR"),
            Action("quit", ("q",), "quit"),
        ),
    ),
    Context(
        "help",
        "The ? cheat-sheet overlay (a pager; it swallows what it doesn't bind).",
        None,
        (
            *_SCROLL,
            Action("theme", ("C",), "the colour picker floats above help"),
            Action("harness", ("H",), "the harness picker floats above help"),
            Action("machine", ("M",), "the machine filter floats above help"),
            Action("edit_keymap", ("K",), "edit these bindings in $EDITOR"),
            Action("close", ("esc", "q", "?"), "close the cheat sheet"),
        ),
    ),
    Context(
        "notices",
        "The N notifications scrollback (a pager).",
        None,
        (
            *_SCROLL,
            Action("close", ("esc", "q", "N"), "close the scrollback"),
        ),
    ),
    Context(
        "trends",
        "The T overlay: tab strip, bar charts, calendar, ranked rows. Sub-states\n"
        "(a focused chart, a drilled row's session list) have their own sections\n"
        "below and fall back here for anything they don't name.",
        None,
        (
            Action("tab_prev", ("h", "left"), "previous Trends tab"),
            Action("tab_next", ("l", "right"), "next Trends tab"),
            Action("select", ("enter",), "focus the chart / drill into a row"),
            Action("down", ("j", "down"), "page the shown month/week/year · move the row cursor"),
            Action("up", ("k", "up"), "page newer · move the row cursor"),
            Action("older", ("[",), "page older (alias)"),
            Action("newer", ("]",), "page newer (alias)"),
            Action("shades_more", ("+", "="), "more heat shades (Calendar)"),
            Action("shades_less", ("-", "_"), "fewer heat shades (Calendar)"),
            Action("back", ("esc",), "leave the focused chart / drill, else close"),
            Action("close", ("q", "T"), "close the overlay"),
            Action("api_prices", ("$",), "re-price the charts at API list rates"),
            Action("help", ("?",), "the cheat sheet, floating above"),
            Action("prices", ("P",), "the price table, floating above"),
            Action("theme", ("C",), "colour theme picker"),
            Action("harness", ("H",), "harness picker / fleet filter"),
            Action("machine", ("M",), "machine filter (fleet)"),
            Action("demo_toggle", ("D",), "flip demo anonymization in place"),
        ),
    ),
    Context(
        "trends.chart",
        "A focused Daily/Weekly/Monthly chart or the Calendar grid (Enter focused it).",
        "trends",
        (
            Action("cursor_left", ("left",), "walk the bar / day cursor left"),
            Action("cursor_right", ("right",), "walk the bar / day cursor right"),
            Action("cursor_up", ("up",), "cursor up (a day · a week on Daily)"),
            Action("cursor_down", ("down",), "cursor down (a day · a week on Daily)"),
            Action("select", ("enter",), "drill into the highlighted bar / day"),
            Action("back", ("esc",), "unfocus — back to tab navigation"),
        ),
    ),
    Context(
        "trends.drill",
        "A ranked row's session list (Enter on a Models/Providers/Harnesses row).",
        "trends",
        (
            *_SCROLL,
            Action("select", ("enter",), "open the selected session"),
            Action("tab_prev", ("h",), "leave the drill, previous tab"),
            Action("tab_next", ("l", "right"), "leave the drill, next tab"),
            Action("back", ("esc", "left", "backspace"), "back to the ranked rows"),
        ),
    ),
    Context(
        "prices",
        "The P overlay's model table. The per-model session drill falls back here.",
        None,
        (
            Action("cycle_view", ("p",), "cycle the view: flat · vendor · provider · models.dev"),
            Action("tab_prev", ("h", "left"), "previous view"),
            Action("tab_next", ("l", "right"), "next view"),
            Action("pin", ("space",), "pin this model ★"),
            *_SCROLL,
            Action("select", ("enter",), "the sessions that used this model"),
            Action("sort", ("s", "S"), "sort the price table"),
            Action("filter", ("f", "/"), "filter the model list"),
            Action("refresh", ("r", "R"), "refresh the rates from models.dev"),
            Action("export", ("e",), "export the price table to CSV"),
            Action("back", ("esc",), "close (from the model list)"),
            Action("close", ("q", "P"), "close the overlay"),
            Action("help", ("?",), "the cheat sheet, floating above"),
            Action("theme", ("C",), "colour theme picker"),
            Action("harness", ("H",), "harness picker / fleet filter"),
            Action("machine", ("M",), "machine filter (fleet)"),
            Action("demo_toggle", ("D",), "flip demo anonymization in place"),
            Action("api_prices", ("$",), "flip the $ what-if pricing behind the table"),
        ),
    ),
    Context(
        "prices.sessions",
        "A model's session list inside P (it only scrolls and steps back out).",
        "prices",
        (
            *_SCROLL,
            Action("back", ("esc", "left", "backspace"), "back to the model list"),
        ),
    ),
    Context(
        "menu",
        "Every small picker shares these: the H harness/source menus, M machines,\n"
        "s sort, C themes, D demo, L launch, w what-if. A per-picker section below\n"
        "overrides one picker; this section re-teaches all of them at once.",
        None,
        (
            Action("down", ("j", "down"), "move the highlight down"),
            Action("up", ("k", "up"), "move the highlight up"),
            Action("first", ("g",), "jump to the first row"),
            Action("last", ("G",), "jump to the last row"),
            Action("select", ("enter",), "apply the highlighted row"),
            Action("cancel", ("esc", "backspace", "q"), "cancel, change nothing"),
        ),
    ),
    Context(
        "menu.source",
        "The H data-source picker (off a fleet).",
        "menu",
        (Action("advance", ("H",), "H again walks the list"),),
    ),
    Context(
        "menu.harness",
        "The H harness filter (in a fleet).",
        "menu",
        (Action("advance", ("H",), "H again walks the list"),),
    ),
    Context(
        "menu.machine",
        "The M machine filter.",
        "menu",
        (Action("advance", ("M",), "M again walks the list"),),
    ),
    Context(
        "menu.sort",
        "The s sort picker.",
        "menu",
        (Action("advance", ("s",), "s again walks the list"),),
    ),
    Context(
        "menu.theme",
        "The C theme picker (j/k live-preview; Esc reverts).",
        "menu",
        (
            Action("advance", ("C",), "C again walks the list"),
            Action("cancel", ("esc", "q"), "cancel and revert the preview"),
        ),
    ),
    Context(
        "menu.demo",
        "The D anonymize picker (a multi-check list).",
        "menu",
        (
            Action("toggle", ("space", "x"), "check / uncheck the category"),
            Action("check_all", ("a",), "check all · clear all"),
            Action("cancel", ("esc", "q", "D"), "cancel, demo state unchanged"),
        ),
    ),
    Context(
        "menu.launch",
        "The L launch picker. Each target also answers to its first letter --\n"
        "those shortcuts follow the target names and are not remappable.",
        "menu",
        (Action("cancel", ("esc", "backspace", "q"), "cancel the launch"),),
    ),
    Context(
        "menu.whatif",
        "The w what-if model picker.",
        "menu",
        (
            Action("advance", ("w",), "w again walks the list"),
            Action("catalog", ("tab", "h", "l", "left", "right"), "your models ↔ the catalog"),
            Action("filter", ("f", "/"), "filter the model list"),
        ),
    ),
    Context(
        "menu.whatif.filter",
        "Typing in the what-if picker's filter (printable keys edit the query).",
        None,
        (
            Action("select", ("enter",), "pick the highlighted model"),
            Action("cancel", ("esc",), "drop the query, back to the list keys"),
            Action("erase", ("backspace",), "delete the last character"),
            Action("clear", ("ctrl-u",), "clear the query"),
            Action("down", ("down", "ctrl-n"), "move the highlight down"),
            Action("up", ("up", "ctrl-p"), "move the highlight up"),
        ),
    ),
    Context(
        "filter",
        "Typing in the live / filter line (printable keys edit the query).",
        None,
        (
            Action("confirm", ("enter",), "keep the filter and leave the line"),
            Action("cancel", ("esc",), "restore the query from before"),
            Action("erase", ("backspace",), "delete the last character"),
            Action("clear", ("ctrl-u",), "clear the query"),
            Action("down", ("down",), "move the selection down, filter stays live"),
            Action("up", ("up",), "move the selection up"),
        ),
    ),
    Context(
        "input",
        "The one-line prompts: the n note, the R range (printables type).",
        None,
        (
            Action("confirm", ("enter",), "accept the value"),
            Action("cancel", ("esc",), "cancel, keep what was there"),
            Action("erase", ("backspace",), "delete the last character"),
            Action("kill_line", ("ctrl-u",), "clear the whole line"),
            Action("kill_word", ("ctrl-w",), "delete the last word"),
        ),
    ),
    Context(
        "prompt.prices",
        "The one-time 'fetch model prices?' prompt (any other key = not now).",
        None,
        (
            Action("accept", ("y", "Y", "enter"), "fetch now"),
            Action("never", ("d", "D"), "never ask again"),
            Action("decline", ("n", "esc"), "not now, ask next run"),
        ),
    ),
)

BY_NAME: dict[str, Context] = {c.name: c for c in REGISTRY}


# --- the composed keymap ------------------------------------------------------------


class Keymap:
    """Defaults + one user's overrides, composed into per-context dispatch tables."""

    def __init__(
        self,
        overrides: dict[tuple[str, str], list[str]] | None = None,
        warnings: list[str] | None = None,
    ):
        # overrides: (context, action) -> spec list ([] = unbind). Built by load();
        # composition turns them into _table (code -> action per context) and _specs
        # (the effective spec list per action, the labels the UI prints).
        self.overrides = overrides or {}
        self.warnings = list(warnings or [])
        self._specs: dict[tuple[str, str], tuple[str, ...]] = {}
        self._table: dict[str, dict[int | str, str]] = {}
        for ctx in REGISTRY:
            self._compose(ctx)

    # - composition -
    def _rows(self, ctx: Context) -> list[tuple[Action, tuple[str, ...], int]]:
        # Every action this context answers to, each with its effective specs and its
        # precedence tier when keys collide:
        #   0  an explicit line in [ctx] itself (own or inherited action) -- strongest
        #   1  an own action's registry default (the sub-context deliberately shadows
        #      the family keys: trends.chart's cursor_left owns ← even though [trends]
        #      tab_prev also says ←)
        #   2  an inherited action carrying the fallback section's user line (one
        #      [menu] edit re-teaches every picker that didn't insist otherwise)
        #   3  an inherited action's registry default
        rows: list[tuple[Action, tuple[str, ...], int]] = []
        for a in ctx.actions:
            if (ctx.name, a.name) in self.overrides:
                rows.append((a, tuple(self.overrides[(ctx.name, a.name)]), 0))
            else:
                rows.append((a, a.keys, 1))
        if ctx.fallback:
            parent = BY_NAME[ctx.fallback]
            named = {a.name for a in ctx.actions}
            for a in parent.actions:
                if a.name in named:
                    continue
                if (ctx.name, a.name) in self.overrides:
                    rows.append((a, tuple(self.overrides[(ctx.name, a.name)]), 0))
                elif (parent.name, a.name) in self.overrides:
                    rows.append((a, tuple(self.overrides[(parent.name, a.name)]), 2))
                else:
                    rows.append((a, a.keys, 3))
        return rows

    def _compose(self, ctx: Context) -> None:
        rows = self._rows(ctx)
        table: dict[int | str, str] = {}
        explicit_codes: set[int | str] = set()  # codes claimed by a [ctx] line (tier 0)
        for tier in (0, 1, 2, 3):
            for action, specs, at in rows:
                if at != tier:
                    continue
                for spec in specs:
                    try:
                        codes = parse_key(spec)
                    except ValueError as exc:
                        self.warnings.append(f"[{ctx.name}] {action.name}: {exc}")
                        continue
                    for code in codes:
                        if code in table:
                            # Within tier 0 two of the user's own lines fight -- say
                            # so, last one wins (file order == registry order here).
                            if tier == 0 and code in explicit_codes:
                                self.warnings.append(
                                    f"[{ctx.name}] {pretty_key(spec)} is bound to both "
                                    f"{table[code]} and {action.name}; {action.name} wins"
                                )
                            else:
                                continue  # a lower tier never steals a claimed key
                        table[code] = action.name
                        if tier == 0:
                            explicit_codes.add(code)
                self._specs.setdefault((ctx.name, action.name), tuple(specs))
        # An action stripped of every key by the user's *explicit* lines is
        # unreachable; say so (an empty value -- a deliberate unbind -- is not, and
        # registry-level shadowing between a sub-context and its fallback is design,
        # not an accident).
        for action, specs, _tier in rows:
            live = {c for spec in specs for c in _codes_or_empty(spec)}
            if not live or any(table.get(c) == action.name for c in live):
                continue
            if any(c in explicit_codes for c in live):
                self.warnings.append(
                    f"[{ctx.name}] {action.name} has no key left — all its keys are "
                    "bound to other actions"
                )
        self._table[ctx.name] = table

    # - dispatch -
    def action(self, context: str, key: int | str) -> str | None:
        """The action `key` triggers in `context`, or None (unbound -> caller decides)."""
        return self._table[context].get(key)

    def is_action(self, context: str, key: int | str, action: str) -> bool:
        return self._table[context].get(key) == action

    # - display -
    def specs(self, context: str, action: str) -> tuple[str, ...]:
        """The spec tokens (context, action) actually answers to -- the user's, else
        default, else the fallback context's, MINUS any spec every one of whose codes
        another action claimed. The labels must read off the same table dispatch does:
        after `down = x` steals x, printing `x  clear the filter` in the help would
        advertise a key that moves the cursor."""
        got = self._specs.get((context, action))
        if got is None:
            parent = BY_NAME[context].fallback
            got = self._specs.get((parent or "", action), ())
        table = self._table.get(context, {})
        return tuple(s for s in got if any(table.get(c) == action for c in _codes_or_empty(s)))

    def label(self, context: str, action: str) -> str:
        """The primary pretty key ("Enter", "s", "^U") -- what a footer chip prints."""
        specs = self.specs(context, action)
        return pretty_key(specs[0]) if specs else ""

    def labels(self, context: str, action: str) -> list[str]:
        """Every pretty key, in binding order -- what the help overlay prints."""
        return [pretty_key(s) for s in self.specs(context, action)]

    def chip(self, context: str, *actions: str) -> str:
        """Compact comma-joined primaries for a footer chip ("f,/" · "t/p/m")."""
        return ",".join(p for a in actions if (p := self.label(context, a)))


def typed_char(key: int | str) -> str | None:
    """The character `key` would type into a text field, or None if it isn't one.
    ASCII printables arrive as ints from _read_key; anything wider arrives as the
    character itself. The text handlers ask this AFTER the keymap, so a bound key
    acts and an unbound one types."""
    if isinstance(key, int):
        return chr(key) if 32 <= key <= 126 else None
    if isinstance(key, str) and key.isprintable():
        return key
    return None


def _codes_or_empty(spec: str) -> tuple[int | str, ...]:
    try:
        return parse_key(spec)
    except ValueError:
        return ()


DEFAULT = Keymap()  # the pristine table; App falls back to it when no file is loaded


# --- the conf file ------------------------------------------------------------------

_HEADER = """\
# opentab keymap — every key the TUI answers to, remappable.
#
# Edit, save, and it takes effect on the next start — or press K inside opentab
# to open this file in $EDITOR and have it reloaded the moment you return.
#
# One line per action:            action = key, key, key
#   - the FIRST key is what the footer and the ? cheat sheet print
#   - an empty value unbinds:     export =
#   - a missing line (or file) means the built-in default — delete anything
#     you don't want to change, and new opentab releases can add keys without
#     touching this file.
#
# Key syntax (case-sensitive for letters: S is shift-s):
#   j  G  /  $  1  ö            a single character, ASCII or not
#   enter esc space tab shift-tab backspace delete insert up down left
#   right pgup pgdn home end f1..f12   named keys
#   ctrl-u  (or ^u)             control chords (letters only)
#   comma                       a literal "," (the bare comma separates keys)
#
# Contexts: each [section] owns the keyboard while its screen is up. The
# pickers share [menu]; a [menu.xxx] line overrides just that picker. The
# Trends sub-states fall back to [trends], the P model drill to [prices].
#
# Ctrl-C always quits and cannot be rebound. Typos here never break the TUI:
# bad lines fall back to the default and land a warning in the N notices.
"""


def default_conf_text() -> str:
    """The full, commented conf file, generated from the registry (so it can't drift)."""
    out = [_HEADER]
    for ctx in REGISTRY:
        out.append("")
        for line in ctx.doc.splitlines():
            out.append(f"# {line}")
        out.append(f"[{ctx.name}]")
        for action in ctx.actions:
            out.append(f"# {action.doc}")
            out.append(f"{action.name} = {', '.join(action.keys)}")
    return "\n".join(out) + "\n"


def ensure_user_keymap(path: str | None = None) -> str:
    """Install the commented default file on first run; never overwrite an edit."""
    path = path or keymap_path()
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(default_conf_text())
        except OSError:
            pass  # a read-only home shouldn't stop the TUI; K will retry and toast
    return path


def load_user_keymap(path: str | None = None) -> Keymap:
    """Read the user's conf into a composed Keymap. Never raises: every problem
    becomes a warning on the result (shown as a toast + the N notices), and the
    broken line falls back to its default -- a typo must not lock the TUI."""
    path = path or keymap_path()
    overrides: dict[tuple[str, str], list[str]] = {}
    warnings: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return Keymap()  # no file (or unreadable): pure defaults, not an error
    import configparser

    # RawConfigParser: no %-interpolation (a spec may BE "%"); strict=False lets a
    # duplicated line mean "last one wins" instead of refusing the whole file; no
    # inline comments, so # and ; stay bindable characters.
    parser = configparser.RawConfigParser(
        delimiters=("=",), comment_prefixes=("#", ";"), strict=False, interpolation=None
    )
    try:
        parser.read_string(text, source=os.path.basename(path))
    except configparser.Error as exc:
        warnings.append(f"keymap unreadable, using defaults: {exc}")
        return Keymap(warnings=warnings)
    for section in parser.sections():
        ctx = BY_NAME.get(section)
        if ctx is None:
            warnings.append(f"unknown section [{section}] ignored")
            continue
        known = {a.name for a in ctx.actions}
        if ctx.fallback:
            known |= {a.name for a in BY_NAME[ctx.fallback].actions}
        for option, value in parser.items(section):
            if option not in known:
                warnings.append(f"[{section}] unknown action {option!r} ignored")
                continue
            specs = [t.strip() for t in value.split(",") if t.strip()]
            good = []
            for token in specs:
                try:
                    parse_key(token)
                except ValueError as exc:
                    warnings.append(f"[{section}] {option}: {exc} — token dropped")
                else:
                    good.append(token)
            if specs and not good:
                warnings.append(f"[{section}] {option}: no valid key left, using the default")
                continue  # all tokens bad: default, not an accidental unbind
            overrides[(section, option)] = good  # [] only when the value was empty
    return Keymap(overrides, warnings)
