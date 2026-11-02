"""Every popup window ClipEm opens over the setup wizard or Settings - message
boxes, prompts and its own dialogs - shares one standard width, POPUP_SIZE's
(96 DPI pixels, scaled like the windows themselves - see ui_scale.py), and is
as tall as its contents, following them if they change. place() does that to a
Toplevel, centred on its parent; a popup only gets wider when its contents
wouldn't fit. File pickers, which have no natural height, get all of POPUP_SIZE.

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


def _centred(parent, width, height):
    """"+X+Y" for a popup of that size, centred on parent's window - or the
    screen, when that isn't showing."""
    owner = _root(parent)
    if owner.winfo_viewable():
        x = owner.winfo_rootx() + (owner.winfo_width() - width) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - height) // 2
    else:
        x = (owner.winfo_screenwidth() - width) // 2
        y = (owner.winfo_screenheight() - height) // 2
    return f"+{max(x, 0)}+{max(y, 0)}"


def geometry(parent):
    """A "WxH+X+Y" geometry for a file picker over parent: the standard size."""
    width, height = size(parent)
    return f"{width}x{height}{_centred(parent, width, height)}"


def place(window, parent=None):
    """Gives window the standard popup width (or more, if its contents need
    it) and centres it on parent (window's master by default). Its height is
    left to Tk, which follows the contents - the mic filter's pages differ."""
    parent = parent or window.master
    # Holds the width: setting a size in geometry() would fix the height too.
    tk.Frame(window, width=size(parent)[0], height=0).pack(side="top")
    window.update_idletasks()
    window.geometry(_centred(parent, window.winfo_reqwidth(), window.winfo_reqheight()))


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
    # Less the label's own padding, or a full line widens the popup.
    tk.Label(body, text=message, justify="left", anchor="nw", wraplength=width - 2 * px(PAD) - px(6)).pack(
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


def askchoice(title, message, buttons, default, parent=None):
    """One of buttons [(label, result)]; None for Escape or the close button."""
    return _dialog(title, message, buttons, parent, default, None)


def askyesno(title, message, parent=None, **_options):
    return _dialog(title, message, [("Yes", True), ("No", False)], parent, True, False)


def askyesnocancel(title, message, parent=None, **_options):
    return _dialog(title, message, [("Yes", True), ("No", False), ("Cancel", None)], parent, True, None)


def askstring(title, prompt, initialvalue="", parent=None, **_options):
    return _dialog(title, prompt, [("OK", True), ("Cancel", None)], parent, True, None,
                   entry_value=initialvalue or "")
