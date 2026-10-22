"""ClipEm's dark look for a Tk window - the clip editor's dark palette
(tray-widget/clip_browser/app.css), so Settings matches it. Call
apply_dark_theme() right after creating the root window, before any widget
exists: the classic widgets (tk.Label, tk.Button...) take their colors from
Tk's option database, which only applies to widgets made afterwards."""

import sys
import tkinter as tk
from tkinter import ttk

BG = "#15151a"
PANEL = "#1e1e25"
TEXT = "#ececf1"
MUTED = "#9a9aa6"  # for secondary text (replaces the light theme's "gray40")
LINE = "#2e2e38"
ACCENT = "#7d72ff"
HOVER = "#272731"
SELECTED = "#302b5c"
ERROR = "#e0556b"


def apply_dark_theme(root):
    root.configure(background=BG)
    _classic_widgets(root)
    _themed_widgets(root)
    if sys.platform == "win32":
        _dark_title_bar(root)


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
        "*Entry.selectForeground": "#ffffff",
        "*Entry.disabledBackground": BG,
        "*Text.background": PANEL,
        "*Text.insertBackground": TEXT,
        "*Text.selectBackground": ACCENT,
        "*Text.selectForeground": "#ffffff",
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
        "*TCombobox*Listbox.selectForeground": "#ffffff",
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
                    selectbackground=ACCENT, selectforeground="#ffffff", insertcolor=TEXT)
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


def _dark_title_bar(root):
    """Windows draws the title bar itself: DWM's immersive-dark-mode attribute
    (20; 19 before Windows 10 20H1) turns it dark once the window has a frame."""
    import ctypes

    done = []

    def on_map(event):
        if event.widget is not root or done:
            return
        done.append(True)
        try:
            hwnd = int(root.wm_frame(), 16)
            value = ctypes.c_int(1)
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd), attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass

    root.bind("<Map>", on_map, add="+")
