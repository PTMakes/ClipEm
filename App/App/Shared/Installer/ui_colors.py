"""ClipEm's colors: the clip editor's light and dark palettes
(tray-widget/clip_browser/app.css, whose defaults these match) and the ones
changed in Settings > UI Customization (run_config.json "custom_colors":
{"dark"/"light": {key: "#rrggbb"}}, keys from COLOR_SETTINGS). No Tk here -
dark_theme.py applies them to Tk windows, clip_browser.py to the editor page."""

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


def mode_key(dark):
    """custom_colors' key for a mode."""
    return "dark" if dark else "light"


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
    """(Settings > Use Dark Mode, its custom colors) from a loaded
    run_config - dark with none for anything unset or malformed."""
    custom = config.get("custom_colors")
    return bool(config.get("dark_mode", True)), custom if isinstance(custom, dict) else {}


def configured_theme():
    """theme_from_config() of run_config.json, or dark with no custom colors
    when it can't be read."""
    try:
        from clipem_common import load_run_config
        return theme_from_config(load_run_config())
    except Exception:
        return True, {}
