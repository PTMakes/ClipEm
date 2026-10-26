"""Every popup window ClipEm opens over the setup wizard or Settings - message
boxes, prompts, file pickers and its own dialogs - shares one standard
rectangular size, POPUP_SIZE (96 DPI pixels, scaled like the windows
themselves - see ui_scale.py). place() gives a Toplevel that size, centred on
its parent; a popup only grows past it when its contents wouldn't fit.

Tk's own message boxes size themselves to their text (tall and narrow for a
long message), so this module also has drop-in replacements for the
tkinter.messagebox and simpledialog.askstring functions ClipEm uses, built
from the dark_theme widgets: `import popups as messagebox` and the call
sites stay the same, with the same results."""

import tkinter as tk

POPUP_SIZE = (640, 420)
PAD = 16


def _root(parent):
    return parent.winfo_toplevel() if parent is not None else tk._default_root


def _px(parent):
    """The scaling of the window popups over parent come from (gui.py's and
    settings_dialog.py's px), or none for a window that didn't set one up."""
    widget = _root(parent)
    while widget is not None:
        if getattr(widget, "px", None):
            return widget.px
        widget = widget.master
    return lambda value: value


def size(parent=None):
    """POPUP_SIZE in real pixels for popups over parent."""
    px = _px(parent)
    return px(POPUP_SIZE[0]), px(POPUP_SIZE[1])


def geometry(parent, min_width=0, min_height=0):
    """A "WxH+X+Y" geometry for a popup over parent: the standard size (or
    min_width/min_height, if bigger) centred on parent's window - or the
    screen, when that isn't showing."""
    width, height = size(parent)
    width, height = max(width, min_width), max(height, min_height)
    owner = _root(parent)
    if owner.winfo_viewable():
        x = owner.winfo_rootx() + (owner.winfo_width() - width) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - height) // 2
    else:
        x = (owner.winfo_screenwidth() - width) // 2
        y = (owner.winfo_screenheight() - height) // 2
    return f"{width}x{height}+{max(x, 0)}+{max(y, 0)}"


def place(window, parent=None):
    """Sizes window to the standard popup size (or bigger, if its contents
    need it) and centres it on parent (window's master by default)."""
    window.update_idletasks()
    window.geometry(geometry(parent or window.master, window.winfo_reqwidth(), window.winfo_reqheight()))


def _dialog(title, message, buttons, parent, default, cancel, entry_value=None):
    """A modal popup with message and buttons [(label, result)]; returns the
    pressed button's result (cancel for Escape or the close button), or the
    entry's text in its place when entry_value is given."""
    owner = _root(parent)
    px = _px(parent)
    dialog = tk.Toplevel(owner)
    dialog.title(title)
    dialog.resizable(False, False)
    if owner.winfo_viewable():
        dialog.transient(owner)  # on a withdrawn owner, some window managers hide it too
    width = size(parent)[0]
    body = tk.Frame(dialog, padx=px(PAD), pady=px(PAD))
    body.pack(fill="both", expand=True)
    row = tk.Frame(body)
    row.pack(side="bottom", anchor="e", pady=(px(12), 0))
    tk.Label(body, text=message, justify="left", anchor="nw", wraplength=width - 2 * px(PAD)).pack(
        side="top", anchor="w", fill="x")
    entry = None
    if entry_value is not None:
        entry = tk.Entry(body)
        entry.insert(0, entry_value)
        entry.select_range(0, "end")
        entry.pack(side="top", fill="x", pady=(px(10), 0))

    state = {"result": cancel}

    def finish(result):
        if entry is not None and result is not cancel:
            result = entry.get()
        state["result"] = result
        dialog.destroy()

    focus = entry
    for label, result in buttons:
        button = tk.Button(row, text=label, width=10, command=lambda r=result: finish(r))
        button.pack(side="left", padx=(px(8), 0))
        if result == default and focus is None:
            focus = button
    dialog.bind("<Return>", lambda _event: finish(default))
    dialog.bind("<Escape>", lambda _event: finish(cancel))
    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(cancel))

    place(dialog, parent)
    dialog.grab_set()
    (focus or dialog).focus_set()
    dialog.wait_window()
    return state["result"]


def showinfo(title, message, parent=None, **_options):
    return _dialog(title, message, [("OK", "ok")], parent, "ok", "ok")


showwarning = showerror = showinfo


def askyesno(title, message, parent=None, **_options):
    return _dialog(title, message, [("Yes", True), ("No", False)], parent, True, False)


def askyesnocancel(title, message, parent=None, **_options):
    return _dialog(title, message, [("Yes", True), ("No", False), ("Cancel", None)], parent, True, None)


def askstring(title, prompt, initialvalue="", parent=None, **_options):
    return _dialog(title, prompt, [("OK", True), ("Cancel", None)], parent, True, None,
                   entry_value=initialvalue or "")
