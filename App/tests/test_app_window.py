import os

import app_window
import pytest
from app_window import EntryHistory


def test_undo_and_redo_walk_through_edits():
    history = EntryHistory("")
    for value in ("/h", "/home", "/home/pt"):
        history.note(value)
    assert history.undo("/home/pt") == "/home"
    assert history.undo("/home") == "/h"
    assert history.redo("/h") == "/home"
    assert history.redo("/home") == "/home/pt"
    assert history.redo("/home/pt") is None


def test_undo_notes_an_unrecorded_last_edit_first():
    """Text changed since the last note (e.g. undo pressed straight after
    typing) is still undoable, and redo brings it back."""
    history = EntryHistory("a")
    assert history.undo("ab") == "a"
    assert history.redo("a") == "ab"


def test_a_new_edit_clears_redo():
    history = EntryHistory("a")
    history.note("ab")
    history.undo("ab")
    history.note("ax")
    assert history.redo("ax") is None


def test_nothing_to_undo():
    assert EntryHistory("x").undo("x") is None


def test_history_is_bounded():
    history = EntryHistory("")
    for i in range(app_window.MAX_UNDO_STEPS + 50):
        history.note(str(i))
    assert len(history.undo_stack) == app_window.MAX_UNDO_STEPS


needs_display = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="needs a real display (run under xvfb-run in CI)",
)


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk(className="ClipEm")
    except tk.TclError as exc:
        pytest.skip(f"no usable display: {exc}")
    yield root
    root.destroy()


@needs_display
@pytest.mark.parametrize("themed", [False, True])
def test_shortcuts_in_a_real_entry(root, themed):
    import tkinter as tk
    from tkinter import ttk

    app_window.install_entry_shortcuts(root)
    entry = (ttk.Entry if themed else tk.Entry)(root)
    entry.pack()
    entry.focus_force()
    root.update()

    for text in ("C:", "/Clips"):
        entry.insert("end", text)
        entry.event_generate("<KeyRelease>", keysym="s")
    entry.event_generate("<Control-a>")
    assert entry.selection_present() and entry.get() == "C:/Clips"

    entry.event_generate("<Control-z>")
    assert entry.get() == "C:"
    entry.event_generate("<Control-z>")
    assert entry.get() == ""
    entry.event_generate("<Control-Shift-R>")
    assert entry.get() == "C:"
    entry.event_generate("<Control-Shift-Z>")
    assert entry.get() == "C:/Clips"


@needs_display
def test_window_icon_is_the_scaled_logo(root):
    app_window.set_window_icon(root)
    sizes = [(image.width(), image.height()) for image in root.clipem_icons]
    assert sizes[0][0] <= 67 and len(sizes) == len(app_window.ICON_SIZES)
