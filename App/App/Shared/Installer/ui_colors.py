"""ClipEm's colors: the clip editor's light and dark palettes
(tray-widget/clip_browser/app.css, whose defaults these match), the themes
built on them (THEMES) and the user's own (run_config.json "saved_themes"),
and the colors changed in Settings > UI Customization (run_config.json
"custom_colors": {theme id: {key: "#rrggbb"}}, keys from COLOR_SETTINGS).
run_config.json "theme" is the chosen theme's id - without it, "dark_mode"
picks Dark or Light Mode; "dark_mode" is kept as the theme's base mode, for
whatever only knows dark from light (the tray menus, app.css's blocks). No
Tk here - dark_theme.py applies them to Tk windows, clip_browser.py to the
editor page."""

PALETTES = {
    True: dict(BG="#15151a", PANEL="#1e1e25", TEXT="#f4f4f6", MUTED="#9a9aa6", LINE="#2e2e38",
               ACCENT="#2080ec", ON_ACCENT="#ffffff", HOVER="#272731", SELECTED="#1b3250", ERROR="#d6455d",
               TRACK="#26262d", TRACK_EDGE="#0c0c10", TICK="#4a4a54",
               WAVE="#6c9bc7", LANE="#24242c", PLAYHEAD="#ff4d6a", PLAYER_BG="#000000",
               BOOST="#2f9e6e", WARN="#b7791f"),
    False: dict(BG="#f4f4f6", PANEL="#ffffff", TEXT="#15151a", MUTED="#6b6b76", LINE="#dedee4",
                ACCENT="#2080ec", ON_ACCENT="#ffffff", HOVER="#ececf2", SELECTED="#dce9fb", ERROR="#d6455d",
                TRACK="#dbdbe2", TRACK_EDGE="#b4b4be", TICK="#a4a4b0",
                WAVE="#4a7aa8", LANE="#ebebf0", PLAYHEAD="#ff4d6a", PLAYER_BG="#000000",
                BOOST="#2f9e6e", WARN="#b7791f"),
}
# The colors Settings > UI Customization offers: (custom_colors key, label,
# the palette name above, the editor's CSS variables it sets). WAVE and the
# rest after it are only drawn by the editor.
COLOR_SETTINGS = (
    ("background", "Background", "BG", ("--bg",)),
    ("panel", "Panels and sidebar", "PANEL", ("--panel",)),
    ("text", "Text color", "TEXT", ("--text",)),
    ("muted", "Secondary text", "MUTED", ("--muted",)),
    ("line", "Borders", "LINE", ("--line",)),
    ("accent", "Accent (buttons, sliders)", "ACCENT", ("--accent",)),
    ("on_accent", "Text on accent", "ON_ACCENT", ("--accent-text",)),
    ("hover", "Hover highlight", "HOVER", ("--hover",)),
    ("selected", "Selected item", "SELECTED", ("--selected",)),
    ("error", "Errors and cuts", "ERROR", ("--cut",)),
    ("track", "Slider bar", "TRACK", ("--track-top", "--track-bottom")),
    ("wave", "Editor waveform", "WAVE", ("--wave",)),
    ("lane", "Editor track background", "LANE", ("--lane",)),
    ("playhead", "Editor playhead", "PLAYHEAD", ("--playhead",)),
    ("player_bg", "Video player background", "PLAYER_BG", ("--player-bg",)),
    ("boost", "Volume boost and meter", "BOOST", ("--boost",)),
    ("warn", "Warnings", "WARN", ("--warn",)),
)


# Themes that come with ClipEm: id -> (label, dark base, colors by
# COLOR_SETTINGS key - the rest are the base mode's).
THEMES = {
    "dark": ("Dark Mode", True, {}),
    "light": ("Light Mode", False, {}),
    "halloween": ("Halloween", True, {
        "background": "#120e0b", "panel": "#1d1712", "text": "#f6ede2", "muted": "#a8937c",
        "line": "#3a2b1e", "accent": "#ff7518", "on_accent": "#1a0e04", "hover": "#2a2018",
        "selected": "#4a2810", "error": "#d43b2a", "track": "#2b2119", "wave": "#a463e0",
        "lane": "#1f1813", "playhead": "#b56cff", "player_bg": "#000000", "boost": "#7cc142",
        "warn": "#e8a317",
    }),
    "christmas": ("Christmas", True, {
        "background": "#0e1611", "panel": "#152019", "text": "#f6f4ee", "muted": "#9fb2a4",
        "line": "#25382b", "accent": "#c8102e", "on_accent": "#ffffff", "hover": "#1d2b22",
        "selected": "#3d1a20", "error": "#ff5c5c", "track": "#1c2a21", "wave": "#d4af37",
        "lane": "#16221a", "playhead": "#f2d16b", "player_bg": "#000000", "boost": "#2fa463",
        "warn": "#d4af37",
    }),
}
SAVED_PREFIX = "saved:"  # a saved theme's id: SAVED_PREFIX + its name
MAX_THEME_NAME = 40


def mode_key(dark):
    """custom_colors' key for a mode - its theme's id."""
    return "dark" if dark else "light"


def saved_theme_id(name):
    return SAVED_PREFIX + name


def saved_themes(config):
    """run_config's "saved_themes" - {name: {"dark": bool, "colors": {key:
    "#rrggbb"}}} - with anything malformed left out."""
    saved = config.get("saved_themes")
    themes = {}
    for name, theme in (saved.items() if isinstance(saved, dict) else ()):
        if not isinstance(name, str) or not name.strip() or not isinstance(theme, dict):
            continue
        colors = theme.get("colors") if isinstance(theme.get("colors"), dict) else {}
        themes[name] = {"dark": bool(theme.get("dark", True)),
                        "colors": {key: colors[key].lower() for key, _label, _name, _css in COLOR_SETTINGS
                                   if is_hex_color(colors.get(key))}}
    return themes


def theme_choices(config):
    """[(id, label)] for the Theme dropdown: ClipEm's, then the user's by name."""
    return ([(theme_id, label) for theme_id, (label, _dark, _colors) in THEMES.items()]
            + [(saved_theme_id(name), name) for name in sorted(saved_themes(config), key=str.lower)])


def theme_base(theme_id, config):
    """(dark, colors by COLOR_SETTINGS key) of a theme before any custom
    colors: every key, from the theme's own colors or its base mode's."""
    if theme_id in THEMES:
        _label, dark, colors = THEMES[theme_id]
    else:
        theme = saved_themes(config).get(theme_id.removeprefix(SAVED_PREFIX), {"dark": True, "colors": {}})
        dark, colors = theme["dark"], theme["colors"]
    palette = PALETTES[dark]
    return dark, {key: colors.get(key, palette[name]) for key, _label, name, _css in COLOR_SETTINGS}


def configured_theme_id(config):
    """The chosen theme's id: run_config's "theme" if it still exists, else
    Dark or Light Mode by "dark_mode"."""
    theme_id = config.get("theme")
    if isinstance(theme_id, str) and (theme_id in THEMES or (
            theme_id.startswith(SAVED_PREFIX) and theme_id.removeprefix(SAVED_PREFIX) in saved_themes(config))):
        return theme_id
    return mode_key(bool(config.get("dark_mode", True)))


def is_hex_color(value):
    """True for "#rrggbb"."""
    return (isinstance(value, str) and len(value) == 7 and value[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in value[1:]))


def mix(color, other, amount):
    """color moved amount (0-1) of the way to other, both "#rrggbb"."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b, strict=True))


def colors_for(dark, custom_colors=None):
    """The palette for a mode with custom_colors (run_config's
    "custom_colors": {"dark"/"light": {key: "#rrggbb"}}) on top - unknown
    keys and anything that isn't a hex color are left out."""
    palette = dict(PALETTES[bool(dark)])
    chosen = (custom_colors or {}).get(mode_key(dark)) or {}
    for key, _label, name, _css in COLOR_SETTINGS:
        if is_hex_color(chosen.get(key)):
            palette[name] = chosen[key].lower()
    return palette


def css_overrides(dark, custom_colors=None):
    """The editor's CSS for the colors changed from its defaults in that mode
    (clip_browser.py adds it to the page); "" when none are."""
    palette = colors_for(dark, custom_colors)
    defaults = PALETTES[bool(dark)]
    lines = []
    for _key, _label, name, css in COLOR_SETTINGS:
        if palette[name] == defaults[name]:
            continue
        if name == "TRACK":  # the editor's bar is a gradient around it
            lines.append(f"  --track-top: {mix(palette[name], '#000000', 0.15)};")
            lines.append(f"  --track-bottom: {mix(palette[name], '#ffffff', 0.08)};")
        else:
            lines.extend(f"  {var}: {palette[name]};" for var in css)
    # Outranks app.css's :root[data-theme="..."] blocks.
    return "html:root[data-theme] {\n" + "\n".join(lines) + "\n}\n" if lines else ""


def theme_from_config(config):
    """(dark, custom colors) for the chosen theme from a loaded run_config,
    for colors_for() and css_overrides(): the theme's base mode, and its
    colors with the user's changes on top, as that mode's custom colors -
    dark with none for anything unset or malformed."""
    theme_id = configured_theme_id(config)
    dark, colors = theme_base(theme_id, config)
    custom = config.get("custom_colors")
    changed = (custom.get(theme_id) if isinstance(custom, dict) else None) or {}
    if isinstance(changed, dict):
        colors.update({key: value.lower() for key, value in changed.items()
                       if key in colors and is_hex_color(value)})
    palette = PALETTES[dark]
    differ = {key: value for key, _label, name, _css in COLOR_SETTINGS
              if (value := colors[key]) != palette[name]}
    return dark, ({mode_key(dark): differ} if differ else {})


def configured_theme():
    """theme_from_config() of run_config.json, or dark with no custom colors
    when it can't be read."""
    try:
        from clipem_common import load_run_config
        return theme_from_config(load_run_config())
    except Exception:
        return True, {}
