"""ClipEm's look for a Tk window - the clip editor's light or dark palette
(tray-widget/clip_browser/app.css), chosen by Settings > "Use Dark Mode"
(run_config.json "dark_mode", dark by default), with any colors changed in
Settings > UI Customization on top (run_config.json "custom_colors", per
mode - see COLOR_SETTINGS). Call apply_theme() right after
creating the root window, before any widget exists: the classic widgets
(tk.Label, tk.Button...) take their colors from Tk's option database, which
only applies to widgets made afterwards. The palette lives in this module's
BG/PANEL/... names, so read them as dark_theme.BG, not by from-import."""

import sys
import tkinter as tk
from tkinter import ttk

from ui_colors import (  # noqa: F401 - the palette is read through this module too
    COLOR_SETTINGS,
    PALETTES,
    colors_for,
    configured_theme,
    css_overrides,
    is_hex_color,
    mix,
    mode_key,
    theme_from_config,
)

DARK = True
BG = PANEL = TEXT = MUTED = LINE = ACCENT = ON_ACCENT = HOVER = SELECTED = ERROR = TRACK = TRACK_EDGE = TICK = ""
WAVE = LANE = PLAYHEAD = PLAYER_BG = BOOST = WARN = ""  # all set by use_mode()


def use_mode(dark, custom_colors=None):
    global DARK
    DARK = bool(dark)
    globals().update(colors_for(DARK, custom_colors))


def configured_dark():
    """Settings > Use Dark Mode; dark when unset or unreadable."""
    return configured_theme()[0]


use_mode(True)


def apply_theme(root):
    use_mode(*configured_theme())
    root.configure(background=BG)
    _classic_widgets(root)
    _themed_widgets(root)
    if sys.platform == "win32":
        _title_bar(root)


apply_dark_theme = apply_theme  # the old name


def _classic_widgets(root):
    options = {
        "*Background": BG,
        "*Foreground": TEXT,
        "*activeBackground": HOVER,
        "*activeForeground": TEXT,
        "*disabledForeground": MUTED,
        "*highlightBackground": BG,
        "*highlightColor": ACCENT,
        "*Entry.background": PANEL,
        "*Entry.insertBackground": TEXT,
        "*Entry.readonlyBackground": BG,
        "*Entry.selectBackground": ACCENT,
        "*Entry.selectForeground": ON_ACCENT,
        "*Entry.disabledBackground": BG,
        "*Text.background": PANEL,
        "*Text.insertBackground": TEXT,
        "*Text.selectBackground": ACCENT,
        "*Text.selectForeground": ON_ACCENT,
        "*Listbox.background": PANEL,
        "*Listbox.selectBackground": ACCENT,
        "*Spinbox.background": PANEL,
        "*Spinbox.insertBackground": TEXT,
        "*Button.background": PANEL,
        "*Button.highlightBackground": BG,
        "*Button.relief": "flat",
        "*Button.borderWidth": 1,
        "*Checkbutton.selectColor": PANEL,  # the box (the tick is the foreground)
        "*Radiobutton.selectColor": PANEL,
        "*Scale.troughColor": PANEL,
        "*Scale.background": BG,
        "*Canvas.background": BG,
        # The drop-down list of a ttk.Combobox is a plain Tk listbox.
        "*TCombobox*Listbox.background": PANEL,
        "*TCombobox*Listbox.foreground": TEXT,
        "*TCombobox*Listbox.selectBackground": ACCENT,
        "*TCombobox*Listbox.selectForeground": ON_ACCENT,
    }
    for pattern, value in options.items():
        root.option_add(pattern, value)


def _themed_widgets(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # the one theme whose colors can all be set
    except tk.TclError:
        return
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, bordercolor=LINE,
                    lightcolor=BG, darkcolor=BG, troughcolor=PANEL, focuscolor=ACCENT,
                    selectbackground=ACCENT, selectforeground=ON_ACCENT, insertcolor=TEXT)
    style.map(".", foreground=[("disabled", MUTED)])
    style.configure("TButton", background=PANEL, bordercolor=LINE)
    style.map("TButton", background=[("active", HOVER), ("pressed", SELECTED)])
    style.configure("TCheckbutton", background=BG, indicatorbackground=PANEL, indicatorforeground=TEXT)
    style.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("active", HOVER)])
    style.configure("TEntry", fieldbackground=PANEL, bordercolor=LINE)
    style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, arrowcolor=TEXT, bordercolor=LINE)
    style.map("TCombobox", fieldbackground=[("readonly", PANEL), ("disabled", BG)],
              background=[("active", HOVER)], foreground=[("disabled", MUTED)],
              selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)])
    style.configure("TScrollbar", background=LINE, troughcolor=BG, bordercolor=BG, arrowcolor=TEXT)
    style.map("TScrollbar", background=[("active", MUTED)])
    style.configure("TSeparator", background=LINE)
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, bordercolor=LINE)


def _title_bar(root):
    """Windows draws the title bar itself: DWM's immersive-dark-mode attribute
    (20; 19 before Windows 10 20H1) sets it dark or light once the window has a frame."""
    import ctypes

    done = []

    def on_map(event):
        if event.widget is not root or done:
            return
        done.append(True)
        try:
            hwnd = int(root.wm_frame(), 16)
            value = ctypes.c_int(1 if DARK else 0)
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd), attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass

    root.bind("<Map>", on_map, add="+")


class RedSlider(tk.Canvas):
    """The editor's slider (clip_browser/app.css): a dark grooved bar with a
    tick every tenth, a red line from the start to the thumb, and a red thumb.
    Pressing anywhere on the bar jumps the thumb there; dragging moves it.
    Takes a ttk.Scale's from_/to/variable/command, plus default (double-click
    resets to it), plus state() and
    configure(state=...) for enabling and disabling."""

    THUMB_W = 18
    BAR_H = 16

    def __init__(self, master, from_=0.0, to=1.0, variable=None, command=None, height=22, default=None, **kw):
        kw.setdefault("background", BG)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("borderwidth", 0)
        super().__init__(master, height=height, cursor="hand2", **kw)
        self._from, self._to = float(from_), float(to)
        self._variable = variable or tk.DoubleVar(master, value=self._from)
        self._command = command
        self._default = default  # double-click resets to this, when given
        self._enabled = True
        self._trace = self._variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._press)
        self.bind("<Double-Button-1>", self._reset)
        self.bind("<Destroy>", self._forget, add="+")

    def _forget(self, event):
        if event.widget is self:
            try:
                self._variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass

    def get(self):
        return self._variable.get()

    def set(self, value):
        value = min(max(float(value), self._from), self._to)
        self._variable.set(round(value) if isinstance(self._variable, tk.IntVar) else value)

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            self.config(cursor="hand2" if self._enabled else "")
            self._draw()
        if cnf or kw:
            return super().configure(cnf, **kw)

    config = configure

    def _span(self):
        half = self.THUMB_W / 2
        return half, max(self.winfo_width() - half, half + 1)

    def _reset(self, _event):
        if self._enabled and self._default is not None:
            self.set(self._default)
            if self._command:
                self._command(self.get())

    def _press(self, event):
        if not self._enabled:
            return
        left, right = self._span()
        fraction = min(max((event.x - left) / (right - left), 0.0), 1.0)
        self.set(self._from + fraction * (self._to - self._from))
        if self._command:
            self._command(self.get())

    def _draw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 2 * self.THUMB_W:
            return
        left, right = self._span()
        top = (height - self.BAR_H) / 2
        mid = height / 2
        red = ACCENT if self._enabled else mix(ACCENT, BG, 0.55)
        red_hi, edge = mix(red, "#ffffff", 0.35), mix(red, "#000000", 0.55)
        self._round(1, top, width - 1, top + self.BAR_H, 6, TRACK, TRACK_EDGE)
        for i in range(1, 10):
            x = left + (right - left) * i / 10
            self.create_line(x, mid - 3, x, mid + 3, fill=TICK)
        fraction = (self.get() - self._from) / ((self._to - self._from) or 1)
        x = left + (right - left) * min(max(fraction, 0.0), 1.0)
        self.create_line(4, mid, x, mid, fill=red, width=2)
        self._round(x - left, mid - 6, x + left, mid + 6, 4, red, edge)
        self.create_line(x - left + 4, mid - 4, x + left - 4, mid - 4, fill=red_hi)

    def _round(self, x1, y1, x2, y2, r, fill, outline):
        points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
                  x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        self.create_polygon(points, smooth=True, fill=fill, outline=outline)


class Card(tk.Canvas):
    """A rounded, outlined box (the editor's panels) around its .body frame -
    put the widgets in .body. Sizes itself to the body's requested size."""

    def __init__(self, parent, pad=14, radius=10, fill_height=False):
        super().__init__(parent, highlightthickness=0, borderwidth=0, background=BG)
        self._pad, self._radius, self._fill_height = pad, radius, fill_height
        self.body = tk.Frame(self, background=BG)
        self._window = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._fit)
        self.bind("<Configure>", self._draw)

    def _fit(self, _event=None):
        self.configure(width=self.body.winfo_reqwidth() + 2 * self._pad,
                       height=self.body.winfo_reqheight() + 2 * self._pad)

    def _draw(self, event):
        self.itemconfigure(self._window, width=max(event.width - 2 * self._pad, 1))
        if self._fill_height:  # the body is the whole box, not just as tall as its content
            self.itemconfigure(self._window, height=max(event.height - 2 * self._pad, 1))
        self.delete("box")
        r, x2, y2 = self._radius, event.width - 1, event.height - 1
        points = [r, 1, x2 - r, 1, x2, 1, x2, r, x2, y2 - r, x2, y2, x2 - r, y2,
                  r, y2, 1, y2, 1, y2 - r, 1, r, 1, 1]
        self.create_polygon(points, smooth=True, fill=BG, outline=LINE, tags="box")
        self.tag_lower("box")
