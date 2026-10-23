import os
import re

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
