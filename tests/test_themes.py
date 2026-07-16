"""The theme palettes shared by the TUI and the web browser (themes.py)."""

import re

import opentab as ot

# --- Shared themes (one source for the web browser + the TUI) ----------------


def test_themes_are_complete_and_consistent():
    for tid, t in ot.THEMES.items():
        assert set(t["roles"]) == set(ot.themes.ROLE_KEYS), f"{tid} missing role slots"
        assert len(t["heat"]) >= 2 and len(t["price_heat"]) >= 2
        assert isinstance(t["dark"], bool) and t["name"]
        for hexval in list(t["roles"].values()) + t["heat"] + t["price_heat"]:
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", hexval), f"{tid}: bad hex {hexval}"
    assert ot.DEFAULT_THEME in ot.THEMES
    assert ot.themes.resolve_theme("nonsense") is ot.THEMES[ot.DEFAULT_THEME]


def test_theme_color_math():
    # nearest_256 lands in the palette range; pure black/white hit the ends.
    assert 16 <= ot.nearest_256("#e0a458") <= 255
    assert ot.nearest_256("#000000") in (16, 232)  # cube origin or darkest grey
    # ramp resamples to exactly n and interpolates the midpoint.
    r = ot.ramp(["#000000", "#ffffff"], 5)
    assert len(r) == 5 and r[0] == "#000000" and r[-1] == "#ffffff"
    assert r[2] in ("#808080", "#7f7f7f")  # halfway grey
    assert ot.ramp(["#123456"], 4) == ["#123456"] * 4  # single stop repeats


def test_nearest_8_maps_roles_onto_the_basic_ansi_palette():
    # The 8-color path (TERM=linux, real terminals): every hex must land in 0..7,
    # because init_pair raises ValueError for any index past COLORS-1 there -- the
    # crash this function exists to prevent. Obvious hexes hit their obvious colors.
    assert ot.nearest_8("#000000") == 0  # black
    assert ot.nearest_8("#cc0000") == 1  # red
    assert ot.nearest_8("#00cc00") == 2  # green
    assert ot.nearest_8("#0000cc") == 4  # blue
    assert ot.nearest_8("#c0caf5") == 7  # Tokyo Night's ink -> white
    for theme in ot.THEMES.values():  # every bundled role resolves in-palette
        for hexval in theme["roles"].values():
            assert 0 <= ot.nearest_8(hexval) <= 7
