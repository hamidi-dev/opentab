"""Shared theme palettes — the single source of truth for the web report and the TUI.

Each theme names a role palette (semantic slots, not hues) plus a calendar heat ramp
(`heat`, coldest→hottest) and a cheap→pricey price-heat ramp (`price_heat`), and a
`dark` flag. The web report reads these verbatim (`web_payload()` reshapes them to the
JS token names and is injected into the page), and the curses TUI maps the same role
hexes to color pairs via `init_color` on true-color terminals (or nearest-256 elsewhere).
Adding a theme is one entry here; both frontends and the `--theme` CLI choices pick it up.

Pure stdlib. The curses application lives in the TUI (this module only supplies data and
the hex→terminal-color math, which is unit-testable without a screen).
"""

from __future__ import annotations

# Role slots every theme fills. The web maps these to CSS variables (underscores ->
# hyphens); the TUI maps a subset to curses color pairs (see tui/app.run).
ROLE_KEYS = (
    "bg",
    "bg_glow",
    "panel",
    "panel2",
    "line",
    "line2",
    "axis",
    "ink",
    "ink2",
    "mut",
    "accent",
    "accent_bright",
    "good",
    "bad",
)


def _theme(name, dark, roles, heat, price_heat):
    return {
        "name": name,
        "dark": dark,
        "roles": dict(zip(ROLE_KEYS, roles)),
        "heat": heat,
        "price_heat": price_heat,
    }


# roles order == ROLE_KEYS: bg bg_glow panel panel2 line line2 axis ink ink2 mut accent accent_bright good bad
THEMES = {
    "opentab": _theme(
        "opentab",
        True,
        [
            "#0b0c0f",
            "#151823",
            "#12141a",
            "#181b23",
            "#242836",
            "#1b1e28",
            "#383c48",
            "#dcdad2",
            "#9b998f",
            "#6a695f",
            "#e0a458",
            "#ffc06e",
            "#62d391",
            "#e07070",
        ],
        ["#1a1d24", "#3d301b", "#5e4620", "#8a6425", "#bd8a2e", "#f2b13f"],
        ["#62d391", "#a6cf5a", "#e0a458", "#e08453", "#e07070"],
    ),
    "catppuccin-mocha": _theme(
        "Catppuccin Mocha",
        True,
        [
            "#11111b",
            "#1e1e2e",
            "#181825",
            "#313244",
            "#45475a",
            "#313244",
            "#6c7086",
            "#cdd6f4",
            "#a6adc8",
            "#7f849c",
            "#cba6f7",
            "#ddb6ff",
            "#a6e3a1",
            "#f38ba8",
        ],
        ["#181825", "#2e2a3d", "#453a5a", "#6d5a8f", "#9a7fc7", "#cba6f7"],
        ["#a6e3a1", "#c9e29d", "#f9e2af", "#fab387", "#f38ba8"],
    ),
    "tokyo-night": _theme(
        "Tokyo Night",
        True,
        [
            "#1a1b26",
            "#24283b",
            "#1f2335",
            "#292e42",
            "#414868",
            "#2a2e42",
            "#545c7e",
            "#c0caf5",
            "#a9b1d6",
            "#565f89",
            "#7aa2f7",
            "#9cb8ff",
            "#9ece6a",
            "#f7768e",
        ],
        ["#16161e", "#20304a", "#254e6e", "#3f7bb0", "#5a91e0", "#7aa2f7"],
        ["#9ece6a", "#c0cb5f", "#e0af68", "#ff9e64", "#f7768e"],
    ),
    "gruvbox": _theme(
        "Gruvbox Dark",
        True,
        [
            "#282828",
            "#32302f",
            "#32302f",
            "#3c3836",
            "#504945",
            "#3c3836",
            "#665c54",
            "#ebdbb2",
            "#d5c4a1",
            "#928374",
            "#fabd2f",
            "#ffd75f",
            "#b8bb26",
            "#fb4934",
        ],
        ["#1d2021", "#453a1f", "#6b5a20", "#98871a", "#d79921", "#fabd2f"],
        ["#b8bb26", "#cabd26", "#fabd2f", "#fe8019", "#fb4934"],
    ),
    "nord": _theme(
        "Nord",
        True,
        [
            "#2e3440",
            "#343b4a",
            "#333a47",
            "#3b4252",
            "#434c5e",
            "#3b4252",
            "#4c566a",
            "#eceff4",
            "#d8dee9",
            "#7b869c",
            "#88c0d0",
            "#a3d4e0",
            "#a3be8c",
            "#bf616a",
        ],
        ["#2e3440", "#354351", "#3a5566", "#4d7e91", "#6aa7ba", "#88c0d0"],
        ["#a3be8c", "#c3ca86", "#ebcb8b", "#d08770", "#bf616a"],
    ),
    "dracula": _theme(
        "Dracula",
        True,
        [
            "#282a36",
            "#313442",
            "#2f313f",
            "#383a4a",
            "#44475a",
            "#383a4a",
            "#565872",
            "#f8f8f2",
            "#d0d2e0",
            "#6272a4",
            "#bd93f9",
            "#d6b3ff",
            "#50fa7b",
            "#ff5555",
        ],
        ["#21222c", "#33314a", "#473f6e", "#6d5aa0", "#9576d0", "#bd93f9"],
        ["#50fa7b", "#9cf07a", "#f1fa8c", "#ffb86c", "#ff5555"],
    ),
    "rose-pine": _theme(
        "Rosé Pine",
        True,
        [
            "#191724",
            "#1f1d2e",
            "#1f1d2e",
            "#26233a",
            "#403d52",
            "#26233a",
            "#524f67",
            "#e0def4",
            "#908caa",
            "#6e6a86",
            "#c4a7e7",
            "#d7c0f0",
            "#9ccfd8",
            "#eb6f92",
        ],
        ["#1f1d2e", "#33293f", "#4a3653", "#7a5580", "#a17bb5", "#c4a7e7"],
        ["#9ccfd8", "#a9c6c0", "#f6c177", "#ea9d84", "#eb6f92"],
    ),
    # Light themes last, so the picker groups darks together (and, since the picker
    # wraps, `k` from the top jumps straight to a light one).
    "catppuccin-latte": _theme(
        "Catppuccin Latte",
        False,
        [
            "#e6e9ef",
            "#eff1f5",
            "#eff1f5",
            "#dce0e8",
            "#bcc0cc",
            "#ccd0da",
            "#8c8fa1",
            "#4c4f69",
            "#5c5f77",
            "#8c8fa1",
            "#8839ef",
            "#7326d3",
            "#40a02b",
            "#d20f39",
        ],
        ["#dce0e8", "#d0c3ef", "#b795ea", "#9d63e4", "#9048ee", "#8839ef"],
        ["#40a02b", "#8aa524", "#df8e0d", "#fe640b", "#d20f39"],
    ),
    "tokyo-night-day": _theme(
        "Tokyo Night Day",
        False,
        [
            "#d5d6db",
            "#e1e2e7",
            "#e9e9ed",
            "#c4c8da",
            "#a8aecb",
            "#c8cbe0",
            "#9099b8",
            "#343b58",
            "#4c5387",
            "#848cb5",
            "#2e7de9",
            "#1a6ce0",
            "#587539",
            "#f52a65",
        ],
        ["#d5d6db", "#c2cdec", "#93b4e8", "#5a92e0", "#3a82e5", "#2e7de9"],
        ["#587539", "#8a8a2e", "#c88a1a", "#e35f00", "#f52a65"],
    ),
}

THEME_IDS = tuple(THEMES)
DEFAULT_THEME = "opentab"


def resolve_theme(theme_id: str) -> dict:
    """The theme dict for an id, falling back to the default for an unknown id."""
    return THEMES.get(theme_id) or THEMES[DEFAULT_THEME]


def web_payload() -> dict:
    """The themes reshaped for the web page's JS: role underscores become CSS-var
    hyphens (`bg_glow` -> `bg-glow`) and the heat ramps keep the JS field names."""
    out = {}
    for tid, t in THEMES.items():
        css = {k.replace("_", "-"): v for k, v in t["roles"].items()}
        out[tid] = {
            "name": t["name"],
            "dark": t["dark"],
            "css": css,
            "heat": t["heat"],
            "priceHeat": t["price_heat"],
        }
    return out


# --- hex → terminal-color math (pure; the TUI does the curses calls) ---------


def hex_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hex_rgb1000(color: str) -> tuple[int, int, int]:
    """RGB scaled to curses' 0..1000 for init_color."""
    r, g, b = hex_rgb(color)
    return round(r / 255 * 1000), round(g / 255 * 1000), round(b / 255 * 1000)


# The xterm-256 cube (16..231) steps and the greyscale ramp (232..255), for nearest().
_CUBE = (0, 95, 135, 175, 215, 255)


def nearest_256(color: str) -> int:
    """The xterm-256 palette index closest to a hex color, for terminals that can't
    redefine colors. Considers both the 6×6×6 color cube and the 24-step grey ramp."""
    r, g, b = hex_rgb(color)

    def closest(v):  # nearest cube axis level for one channel
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))

    ci = 16 + 36 * closest(r) + 6 * closest(g) + closest(b)
    cr, cg, cb = _CUBE[closest(r)], _CUBE[closest(g)], _CUBE[closest(b)]
    cube_d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    # Grey ramp: 232..255 are 8,18,...,238; index by the average channel.
    grey_level = round((r + g + b) / 3 / 255 * 23) if r or g or b else 0
    gv = 8 + 10 * grey_level if grey_level < 24 else 238
    gv = min(gv, 238)
    grey_i = 232 + min(grey_level, 23)
    grey_d = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return grey_i if grey_d < cube_d else ci


def ramp(hexes: list[str], n: int) -> list[str]:
    """Resample a hex ramp to exactly `n` colors by linear RGB interpolation, so the
    calendar heat map can render at any granularity from a theme's fixed ramp."""
    if n <= 0 or not hexes:
        return []
    if n == 1:
        return [hexes[-1]]
    if len(hexes) == 1:
        return [hexes[0]] * n
    stops = [hex_rgb(h) for h in hexes]
    out = []
    for i in range(n):
        pos = i * (len(stops) - 1) / (n - 1)
        lo = int(pos)
        hi = min(lo + 1, len(stops) - 1)
        f = pos - lo
        r = round(stops[lo][0] + (stops[hi][0] - stops[lo][0]) * f)
        g = round(stops[lo][1] + (stops[hi][1] - stops[lo][1]) * f)
        b = round(stops[lo][2] + (stops[hi][2] - stops[lo][2]) * f)
        out.append(f"#{r:02x}{g:02x}{b:02x}")
    return out
