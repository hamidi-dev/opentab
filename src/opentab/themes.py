"""Shared theme palettes — the single source of truth for the web browser and the TUI.

Each theme names a role palette (semantic slots, not hues) plus a calendar heat ramp
(`heat`, coldest→hottest) and a cheap→pricey price-heat ramp (`price_heat`), and a
`dark` flag. The web browser reads these verbatim (`web_payload()` reshapes them to the
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
    "tokyo-night-storm": _theme(
        "Tokyo Night Storm",
        True,
        [
            "#1f2335",
            "#292e42",
            "#24283b",
            "#2f344d",
            "#414868",
            "#343a54",
            "#545c7e",
            "#c0caf5",
            "#a9b1d6",
            "#565f89",
            "#7aa2f7",
            "#9cb8ff",
            "#9ece6a",
            "#f7768e",
        ],
        ["#1f2335", "#26365a", "#2c567e", "#4079b8", "#5c93e2", "#7aa2f7"],
        ["#9ece6a", "#c0cb5f", "#e0af68", "#ff9e64", "#f7768e"],
    ),
    "tokyo-night-moon": _theme(
        "Tokyo Night Moon",
        True,
        [
            "#1e2030",
            "#222436",
            "#222436",
            "#2f334d",
            "#444a73",
            "#2f334d",
            "#545c7e",
            "#c8d3f5",
            "#828bb8",
            "#636da6",
            "#82aaff",
            "#a8c3ff",
            "#c3e88d",
            "#ff757f",
        ],
        ["#1e2030", "#273356", "#2f527c", "#4a7cbd", "#658fe0", "#82aaff"],
        ["#c3e88d", "#e0d883", "#ffc777", "#ff966c", "#ff757f"],
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
    "catppuccin-macchiato": _theme(
        "Catppuccin Macchiato",
        True,
        [
            "#181926",
            "#24273a",
            "#1e2030",
            "#363a4f",
            "#494d64",
            "#363a4f",
            "#6e738d",
            "#cad3f5",
            "#a5adcb",
            "#8087a2",
            "#c6a0f6",
            "#dab8ff",
            "#a6da95",
            "#ed8796",
        ],
        ["#1e2030", "#332e48", "#4a3d63", "#71538f", "#9b79c4", "#c6a0f6"],
        ["#a6da95", "#cad79a", "#eed49f", "#f5a97f", "#ed8796"],
    ),
    "catppuccin-frappe": _theme(
        "Catppuccin Frappé",
        True,
        [
            "#232634",
            "#303446",
            "#292c3c",
            "#414559",
            "#51576d",
            "#414559",
            "#737994",
            "#c6d0f5",
            "#a5adce",
            "#838ba7",
            "#ca9ee6",
            "#e0b6f5",
            "#a6d189",
            "#e78284",
        ],
        ["#292c3c", "#3c3a52", "#585070", "#8a6ba0", "#ab86c8", "#ca9ee6"],
        ["#a6d189", "#c8d38a", "#e5c890", "#ef9f76", "#e78284"],
    ),
    "kanagawa-wave": _theme(
        "Kanagawa Wave",
        True,
        [
            "#16161d",
            "#1f1f28",
            "#1a1a22",
            "#2a2a37",
            "#363646",
            "#2a2a37",
            "#54546d",
            "#dcd7ba",
            "#c8c093",
            "#727169",
            "#7e9cd8",
            "#9cb8e8",
            "#98bb6c",
            "#ff5d62",
        ],
        ["#1a1a22", "#223249", "#2d4f67", "#4a7196", "#6487bc", "#7e9cd8"],
        ["#98bb6c", "#c0bb78", "#e6c384", "#ffa066", "#ff5d62"],
    ),
    "kanagawa-dragon": _theme(
        "Kanagawa Dragon",
        True,
        [
            "#0d0c0c",
            "#181616",
            "#12120f",
            "#282727",
            "#393836",
            "#282727",
            "#625e5a",
            "#c5c9c5",
            "#a6a69c",
            "#737c73",
            "#8ba4b0",
            "#a8c0cb",
            "#87a987",
            "#c4746e",
        ],
        ["#12120f", "#1e2a2e", "#2c444c", "#4c6b76", "#6c8894", "#8ba4b0"],
        ["#87a987", "#a8a887", "#c4b28a", "#b6927b", "#c4746e"],
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
    "everforest": _theme(
        "Everforest Dark",
        True,
        [
            "#232a2e",
            "#2d353b",
            "#2a3339",
            "#343f44",
            "#475258",
            "#3d484d",
            "#7a8478",
            "#d3c6aa",
            "#9da9a0",
            "#859289",
            "#a7c080",
            "#bdd49a",
            "#83c092",
            "#e67e80",
        ],
        ["#2d353b", "#3a4a3d", "#4c6344", "#6d8b55", "#8aa66a", "#a7c080"],
        ["#a7c080", "#c1be80", "#dbbc7f", "#e69875", "#e67e80"],
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
    "one-dark": _theme(
        "One Dark",
        True,
        [
            "#21252b",
            "#282c34",
            "#252930",
            "#2c313c",
            "#3e4451",
            "#31353f",
            "#4b5263",
            "#abb2bf",
            "#969ead",
            "#5c6370",
            "#61afef",
            "#8cc5ff",
            "#98c379",
            "#e06c75",
        ],
        ["#252930", "#23405c", "#255a85", "#3b82b8", "#4e9cdc", "#61afef"],
        ["#98c379", "#c1c277", "#e5c07b", "#d19a66", "#e06c75"],
    ),
    "ayu-dark": _theme(
        "Ayu Dark",
        True,
        [
            "#0b0e14",
            "#11151c",
            "#0d1017",
            "#131721",
            "#232834",
            "#171c26",
            "#475266",
            "#bfbdb6",
            "#9c9a94",
            "#6c7380",
            "#e6b450",
            "#ffc963",
            "#aad94c",
            "#d95757",
        ],
        ["#0d1017", "#2e2a1a", "#584a20", "#8f7428", "#c29a3c", "#e6b450"],
        ["#aad94c", "#c8c74e", "#e6b450", "#ff8f40", "#d95757"],
    ),
    "solarized": _theme(
        "Solarized Dark",
        True,
        [
            "#002b36",
            "#073642",
            "#04313c",
            "#0a3d4a",
            "#1a4a56",
            "#0e4250",
            "#586e75",
            "#93a1a1",
            "#839496",
            "#657b83",
            "#268bd2",
            "#4ba6e8",
            "#859900",
            "#dc322f",
        ],
        ["#073642", "#0c4a63", "#12608b", "#1a76b2", "#2081c4", "#268bd2"],
        ["#859900", "#a29400", "#b58900", "#cb4b16", "#dc322f"],
    ),
    "github-dark": _theme(
        "GitHub Dark",
        True,
        [
            "#0d1117",
            "#161b22",
            "#11161d",
            "#21262d",
            "#30363d",
            "#21262d",
            "#484f58",
            "#e6edf3",
            "#b1bac4",
            "#8b949e",
            "#58a6ff",
            "#79c0ff",
            "#3fb950",
            "#f85149",
        ],
        ["#161b22", "#1b3050", "#20497e", "#2f6cb4", "#4489da", "#58a6ff"],
        ["#3fb950", "#90b83c", "#d29922", "#db6d28", "#f85149"],
    ),
    "night-owl": _theme(
        "Night Owl",
        True,
        [
            "#011627",
            "#0b2942",
            "#041c30",
            "#1d3b53",
            "#264d68",
            "#13344f",
            "#5f7e97",
            "#d6deeb",
            "#a2b8cf",
            "#637777",
            "#82aaff",
            "#a3c0ff",
            "#addb67",
            "#ef5350",
        ],
        ["#0b2942", "#173a67", "#24508f", "#3f6fc0", "#5f8ce2", "#82aaff"],
        ["#addb67", "#cfd670", "#ecc48d", "#f78c6c", "#ef5350"],
    ),
    "vitesse": _theme(
        "Vitesse Dark",
        True,
        [
            "#121212",
            "#1c1c1c",
            "#161616",
            "#222222",
            "#2e2e2e",
            "#242424",
            "#4e4e4e",
            "#dbd7ca",
            "#b8b3a2",
            "#758575",
            "#4d9375",
            "#5eae8b",
            "#80a665",
            "#cb7676",
        ],
        ["#161616", "#1c2f27", "#25493a", "#356852", "#428463", "#4d9375"],
        ["#80a665", "#b3aa6e", "#e6cc77", "#d4976c", "#cb7676"],
    ),
    "poimandres": _theme(
        "Poimandres",
        True,
        [
            "#171922",
            "#1b1e28",
            "#1b1e28",
            "#303340",
            "#3e4462",
            "#252b3b",
            "#506477",
            "#e4f0fb",
            "#a6accd",
            "#767c9d",
            "#5de4c7",
            "#89f0dc",
            "#5fb3a1",
            "#d0679d",
        ],
        ["#1b1e28", "#1f4241", "#226358", "#328e77", "#43ba9e", "#5de4c7"],
        ["#5de4c7", "#b0e3b6", "#fffac2", "#e6a37e", "#d0679d"],
    ),
    "monokai": _theme(
        "Monokai",
        True,
        [
            "#1e1f1c",
            "#272822",
            "#23241f",
            "#31322a",
            "#49483e",
            "#3e3d32",
            "#6e6a56",
            "#f8f8f2",
            "#c8c8bd",
            "#75715e",
            "#fd971f",
            "#ffb04f",
            "#a6e22e",
            "#f92672",
        ],
        ["#23241f", "#453521", "#6b4b22", "#9c6a22", "#ce8420", "#fd971f"],
        ["#a6e22e", "#d5de4e", "#e6db74", "#fd971f", "#f92672"],
    ),
    "synthwave-84": _theme(
        "Synthwave '84",
        True,
        [
            "#241b2f",
            "#2a2139",
            "#262335",
            "#34294f",
            "#463465",
            "#342a4f",
            "#6d6a94",
            "#f0eff1",
            "#bbb6d1",
            "#848bbd",
            "#ff7edb",
            "#ff9ee6",
            "#72f1b8",
            "#fe4450",
        ],
        ["#262335", "#4a2b56", "#713478", "#a4479f", "#d260bf", "#ff7edb"],
        ["#72f1b8", "#b8e88a", "#fede5d", "#ff8b39", "#fe4450"],
    ),
    "vesper": _theme(
        "Vesper",
        True,
        [
            "#101010",
            "#191919",
            "#141414",
            "#1f1f1f",
            "#282828",
            "#1e1e1e",
            "#404040",
            "#ededed",
            "#b8b8b8",
            "#7e7e7e",
            "#ffc799",
            "#ffd9b8",
            "#99ffe4",
            "#ff8080",
        ],
        ["#141414", "#3d2f24", "#69503a", "#9c7654", "#cc9c72", "#ffc799"],
        ["#99ffe4", "#c6e6bb", "#ffc799", "#ffa27a", "#ff8080"],
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
    "everforest-light": _theme(
        "Everforest Light",
        False,
        [
            "#efebd4",
            "#fdf6e3",
            "#f4f0d9",
            "#e6e2cc",
            "#bdc3af",
            "#e0dcc7",
            "#939f91",
            "#5c6a72",
            "#708089",
            "#939f91",
            "#8da101",
            "#7a8e00",
            "#35a77c",
            "#f85552",
        ],
        ["#e6e2cc", "#d7dcae", "#c2cd7f", "#a8bb45", "#98ae1d", "#8da101"],
        ["#35a77c", "#8da101", "#dfa000", "#f57d26", "#f85552"],
    ),
    "solarized-light": _theme(
        "Solarized Light",
        False,
        [
            "#eee8d5",
            "#fdf6e3",
            "#f5efdc",
            "#e4ddc8",
            "#cfc8b4",
            "#ddd6c1",
            "#93a1a1",
            "#586e75",
            "#657b83",
            "#93a1a1",
            "#268bd2",
            "#1a76b2",
            "#859900",
            "#dc322f",
        ],
        ["#e4ddc8", "#c0d2d8", "#8fbede", "#55a3d8", "#3595d5", "#268bd2"],
        ["#859900", "#a29400", "#b58900", "#cb4b16", "#dc322f"],
    ),
    "one-light": _theme(
        "One Light",
        False,
        [
            "#eaeaeb",
            "#fafafa",
            "#f2f2f2",
            "#e0e0e1",
            "#c9c9ca",
            "#d8d8d9",
            "#a0a1a7",
            "#383a42",
            "#4f525b",
            "#a0a1a7",
            "#4078f2",
            "#2660e0",
            "#50a14f",
            "#e45649",
        ],
        ["#e0e0e1", "#c3cfef", "#9cb4f2", "#6d95f2", "#5286f2", "#4078f2"],
        ["#50a14f", "#899b28", "#c18401", "#d1642a", "#e45649"],
    ),
    "github-light": _theme(
        "GitHub Light",
        False,
        [
            "#f6f8fa",
            "#ffffff",
            "#fafbfc",
            "#eaeef2",
            "#d0d7de",
            "#dfe4ea",
            "#8c959f",
            "#1f2328",
            "#424a53",
            "#656d76",
            "#0969da",
            "#0550ae",
            "#1a7f37",
            "#cf222e",
        ],
        ["#eaeef2", "#c4d7f2", "#8fbaf0", "#4f94e8", "#2a7ce2", "#0969da"],
        ["#1a7f37", "#6a8a1f", "#9a6700", "#bc4c00", "#cf222e"],
    ),
    "vitesse-light": _theme(
        "Vitesse Light",
        False,
        [
            "#f2f2f0",
            "#ffffff",
            "#f7f7f5",
            "#e8e8e5",
            "#d2d2cd",
            "#e0e0dc",
            "#9c9c92",
            "#393a34",
            "#55564e",
            "#a0ada0",
            "#1e754f",
            "#145c3c",
            "#59873a",
            "#ab5959",
        ],
        ["#e8e8e5", "#c2dccd", "#8ec4a7", "#57a37c", "#328a63", "#1e754f"],
        ["#59873a", "#8f8a2e", "#bda437", "#a65e2b", "#ab5959"],
    ),
}

THEME_IDS = tuple(THEMES)
DEFAULT_THEME = "tokyo-night"


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


# The 8 basic ANSI colors at xterm's default RGB, index == the curses COLOR_* constant.
_ANSI8 = (
    (0, 0, 0),  # black
    (205, 0, 0),  # red
    (0, 205, 0),  # green
    (205, 205, 0),  # yellow
    (0, 0, 205),  # blue
    (205, 0, 205),  # magenta
    (0, 205, 205),  # cyan
    (229, 229, 229),  # white
)


def nearest_8(color: str) -> int:
    """The basic ANSI color (0..7) closest to a hex, for 8-color terminals -- the
    Linux console, real serial terminals -- whose palette init_pair refuses anything
    past. The heat ramps have their own generated ANSI path; this is for the roles."""
    r, g, b = hex_rgb(color)
    return min(
        range(8),
        key=lambda i: (_ANSI8[i][0] - r) ** 2 + (_ANSI8[i][1] - g) ** 2 + (_ANSI8[i][2] - b) ** 2,
    )


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
