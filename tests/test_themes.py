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
