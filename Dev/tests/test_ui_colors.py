import os
import re

import pytest
import ui_colors
from conftest import TRAY_WIDGET_DIR


def _css_blocks():
    """app.css's light (:root) and dark (:root[data-theme="dark"]) variables."""
    with open(os.path.join(TRAY_WIDGET_DIR, "clip_browser", "app.css"), encoding="utf-8") as f:
        css = f.read()
    light = re.search(r"^:root \{(.*?)^\}", css, re.S | re.M).group(1)
    dark = re.search(r'^:root\[data-theme="dark"\] \{(.*?)^\}', css, re.S | re.M).group(1)
    variables = lambda block: dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))  # noqa: E731
    light_vars = variables(light)
    return {False: light_vars, True: {**light_vars, **variables(dark)}}


def test_defaults_match_the_editor_css():
    css = _css_blocks()
    for dark in (True, False):
        for _key, _label, name, variables in ui_colors.COLOR_SETTINGS:
            if name == "TRACK":  # the editor's bar is a gradient around it
                continue
            for variable in variables:
                assert css[dark][variable] == ui_colors.PALETTES[dark][name], (dark, variable)


def test_custom_colors_only_for_their_own_mode():
    custom = {"dark": {"background": "#ABCDEF", "text": "nope", "unknown": "#000000"}}
    assert ui_colors.colors_for(True, custom)["BG"] == "#abcdef"
    assert ui_colors.colors_for(True, custom)["TEXT"] == ui_colors.PALETTES[True]["TEXT"]
    assert ui_colors.colors_for(False, custom) == ui_colors.PALETTES[False]


def test_css_overrides_set_only_changed_colors():
    assert ui_colors.css_overrides(True, {}) == ""
    # Set to its default: nothing to override.
    assert ui_colors.css_overrides(True, {"dark": {"wave": ui_colors.PALETTES[True]["WAVE"]}}) == ""
    css = ui_colors.css_overrides(True, {"dark": {"error": "#ff0000", "track": "#808080"}})
    assert "--cut: #ff0000;" in css
    assert "--track-top: #6d6d6d;" in css and "--track-bottom: #8a8a8a;" in css
    assert css.startswith("html:root[data-theme] {")


def test_theme_from_config_ignores_malformed_colors():
    assert ui_colors.theme_from_config({}) == (True, {})
    assert ui_colors.theme_from_config({"dark_mode": False, "custom_colors": "red"}) == (False, {})


def test_mix():
    assert ui_colors.mix("#000000", "#ffffff", 0.5) == "#808080"
    assert ui_colors.mix("#2080ec", "#2080ec", 0.3) == "#2080ec"


def _luminance(color):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(color[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("theme_id", ["halloween", "christmas"])
def test_the_seasonal_themes_are_dark_complete_and_readable(theme_id):
    label, dark, colors = ui_colors.THEMES[theme_id]
    assert dark is True and label
    assert set(colors) == {key for key, *_rest in ui_colors.COLOR_SETTINGS}
    assert all(ui_colors.is_hex_color(value) for value in colors.values())
    # WCAG AA for text: on the background, on panels, and on the accent's buttons.
    for text, under in (("text", "background"), ("text", "panel"), ("on_accent", "accent"),
                        ("muted", "background")):
        minimum = 4.5 if text != "muted" else 3.0
        assert _contrast(colors[text], colors[under]) >= minimum, (theme_id, text, under)


def test_a_theme_is_its_colors_with_the_users_changes_on_top():
    dark, custom = ui_colors.theme_from_config({"theme": "halloween",
                                                "custom_colors": {"halloween": {"wave": "#ABCDEF"},
                                                                  "dark": {"wave": "#000000"}}})
    palette = ui_colors.colors_for(dark, custom)
    assert dark is True and palette["WAVE"] == "#abcdef"
    assert palette["ACCENT"] == ui_colors.THEMES["halloween"][2]["accent"]
    assert palette["TRACK_EDGE"] == ui_colors.PALETTES[True]["TRACK_EDGE"]  # not offered: the base mode's
    assert "--accent: #ff7518;" in ui_colors.css_overrides(dark, custom)


def test_without_a_theme_dark_mode_decides_as_before():
    assert ui_colors.configured_theme_id({}) == "dark"
    assert ui_colors.configured_theme_id({"dark_mode": False}) == "light"
    assert ui_colors.configured_theme_id({"theme": "nope", "dark_mode": False}) == "light"
    # A saved theme that's gone (deleted, the dialog cancelled) falls back too.
    assert ui_colors.configured_theme_id({"theme": "saved:Gone"}) == "dark"


def test_a_saved_theme_has_its_own_base_and_colors():
    config = {"theme": "saved:Paper", "saved_themes": {
        "Paper": {"dark": False, "colors": {"accent": "#AA0000", "text": "red", "nope": "#000000"}},
        "": {"dark": True}, "Broken": "x"}}
    assert ui_colors.saved_themes(config) == {"Paper": {"dark": False, "colors": {"accent": "#aa0000"}}}
    dark, custom = ui_colors.theme_from_config(config)
    assert dark is False and ui_colors.colors_for(dark, custom)["ACCENT"] == "#aa0000"
    assert ui_colors.theme_choices(config)[-1] == ("saved:Paper", "Paper")
    assert [label for _id, label in ui_colors.theme_choices({})] == ["Dark Mode", "Light Mode", "Halloween",
                                                                      "Christmas"]
