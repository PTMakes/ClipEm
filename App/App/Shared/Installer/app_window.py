"""
What every ClipEm Tk window shares - the setup wizard (gui.py), the tray's
settings dialog and run.py's "downloading OBS" notice: the ClipEm logo as
the window icon (for the window's own dialogs too, e.g. Tk's folder picker
and message boxes), and the usual text-field shortcuts:

    Ctrl+A                    select all (Tk's X11 default jumps to the line start)
    Ctrl+Z                    undo
    Ctrl+Shift+Z / Ctrl+Shift+R   redo

Tk entries have no undo of their own (only Text widgets do), so each
entry's edits are tracked here - see EntryHistory.
"""

import base64
import io
import os
import sys

from clipem_common import SHARED_DIR

ICON_PNG = os.path.join(SHARED_DIR, "tray-widget", "ClipEm.png")
ICON_ICO = os.path.join(SHARED_DIR, "tray-widget", "ClipEm.ico")
ICON_SIZES = (64, 32, 16)

# Tk widget classes the shortcuts apply to: classic and themed entries,
# and editable comboboxes (read-only ones ignore edits anyway).
ENTRY_CLASSES = ("Entry", "TEntry", "TCombobox")
MAX_UNDO_STEPS = 100


def use_windows_app_id(app_id):
    """Without its own AppUserModelID, Windows groups the window with
    Python's in the taskbar and shows Python's icon there. Must run before
    the first window is created."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass


def _icon_images(tk):
    """The logo at ICON_SIZES - it's 1080px, far bigger than any window icon
    (on X11 the full size would be a ~4.6 MB window property). Scaled with
    Pillow when it's there (it comes with the tray widget), else with Tk's
    own blockier subsample."""
    try:
        from PIL import Image
    except ImportError:
        full = tk.PhotoImage(file=ICON_PNG)
        return [full.subsample(max(1, full.width() // size)) for size in ICON_SIZES]

    images = []
    with Image.open(ICON_PNG) as logo:
        logo = logo.convert("RGBA")
        for size in ICON_SIZES:
            buf = io.BytesIO()
            logo.resize((size, size), Image.LANCZOS).save(buf, format="PNG")
            images.append(tk.PhotoImage(data=base64.b64encode(buf.getvalue())))
    return images


def set_window_icon(root, log=None):
    """The ClipEm logo as root's icon and the default for every window it
    opens later. Best effort - a missing icon just leaves Tk's default."""
    import tkinter as tk

    try:
        if sys.platform == "win32" and os.path.exists(ICON_ICO):
            root.iconbitmap(default=ICON_ICO)  # crisp at every taskbar/title-bar size
        # Kept on root: Tk drops an image once nothing references it.
        root.clipem_icons = _icon_images(tk)
        root.iconphoto(True, *root.clipem_icons)
    except (tk.TclError, OSError) as exc:
        if log:
            log(f"Couldn't set the window icon: {exc}")


class EntryHistory:
    """Undo/redo for one text field, as a list of its past values. note()
    records the current value if it changed since the last note; undo() and
    redo() return the value to put back, or None if there's nothing to do."""

    def __init__(self, value):
        self.current = value
        self.undo_stack = []
        self.redo_stack = []

    def note(self, value):
        if value == self.current:
            return
        self.undo_stack.append(self.current)
        del self.undo_stack[:-MAX_UNDO_STEPS]
        self.redo_stack.clear()
        self.current = value

    def undo(self, value):
        self.note(value)
        if not self.undo_stack:
            return None
        self.redo_stack.append(self.current)
        self.current = self.undo_stack.pop()
        return self.current

    def redo(self, value):
        self.note(value)
        if not self.redo_stack:
            return None
        self.undo_stack.append(self.current)
        self.current = self.redo_stack.pop()
        return self.current


def install_entry_shortcuts(root):
    """Binds the shortcuts in the module docstring for every entry in this
    Tk app (class bindings, so entries created later - including those in
    Tk's own dialogs - get them too). A widget's own binding that returns
    "break" (e.g. the wizard's clips-folder field) still takes precedence."""
    histories = {}

    def history(widget):
        key = str(widget)
        if key not in histories:
            histories[key] = EntryHistory(widget.get())
        return histories[key]

    def note(event):
        try:
            history(event.widget).note(event.widget.get())
        except Exception:
            pass  # e.g. the widget is already being destroyed

    def put(widget, value):
        if value is None:
            return
        state = str(widget.cget("state"))
        if state in ("readonly", "disabled"):
            return
        widget.delete(0, "end")
        widget.insert(0, value)
        widget.icursor("end")

    def select_all(event):
        event.widget.select_range(0, "end")
        event.widget.icursor("end")
        return "break"

    def undo(event):
        put(event.widget, history(event.widget).undo(event.widget.get()))
        return "break"

    def redo(event):
        put(event.widget, history(event.widget).redo(event.widget.get()))
        return "break"

    def forget(event):
        # Tk reuses widget names (the wizard rebuilds each page), so a new
        # field mustn't inherit a destroyed one's history.
        histories.pop(str(event.widget), None)

    for cls in ENTRY_CLASSES:
        # Start each field's history from its value when it gets focus, and
        # note every change made by typing, pasting or cutting.
        for sequence in ("<FocusIn>", "<KeyRelease>", "<<Paste>>", "<<Cut>>", "<ButtonRelease-2>"):
            root.bind_class(cls, sequence, note, add="+")
        root.bind_class(cls, "<Destroy>", forget, add="+")
        for sequence in ("<Control-a>", "<Control-A>"):
            root.bind_class(cls, sequence, select_all)
        for sequence in ("<Control-z>", "<Control-Z>"):
            root.bind_class(cls, sequence, undo)
        for sequence in ("<Control-Shift-z>", "<Control-Shift-Z>", "<Control-Shift-r>", "<Control-Shift-R>"):
            root.bind_class(cls, sequence, redo)


def style_app_window(root, log=None):
    """Icon + text shortcuts for a ClipEm Tk app's root window. Call
    use_windows_app_id() before creating root."""
    set_window_icon(root, log=log)
    install_entry_shortcuts(root)
