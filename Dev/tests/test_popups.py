"""popups.py: the standard width, with the height following the contents."""


import pytest
from conftest import has_display

pytestmark = pytest.mark.skipif(not has_display(),
                                reason="needs a display")

SHORT = "A short explanation."
LONG = "A much longer explanation that wraps onto a good many lines of the popup. " * 30


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("no usable display")
    window.withdraw()
    yield window
    window.destroy()


def _shown_size(root, show):
    """(width, height) of the popup show() opens, closing it straight away."""
    import popups

    sizes = []

    def measure():
        popup = root.winfo_children()[-1]
        popup.update()
        sizes.append((popup.winfo_width(), popup.winfo_height()))
        popup.destroy()

    root.after(100, measure)
    show(popups)
    return sizes[0]


def test_a_short_message_is_the_standard_width_and_short(root):
    import popups

    width, height = _shown_size(root, lambda p: p.showinfo("Title", SHORT, parent=root))
    assert width == popups.POPUP_SIZE[0]
    assert height < popups.POPUP_SIZE[1] / 2


def test_a_long_message_is_the_same_width_but_taller(root):
    import popups

    width, height = _shown_size(root, lambda p: p.showinfo("Title", LONG, parent=root))
    short_height = _shown_size(root, lambda p: p.showinfo("Title", SHORT, parent=root))[1]
    assert width == popups.POPUP_SIZE[0] and height > short_height


def test_a_popup_follows_its_contents_after_placing(root):
    import tkinter as tk

    import popups

    popup = tk.Toplevel(root)
    body = tk.Frame(popup)
    body.pack(fill="both", expand=True)
    first = tk.Label(body, text="Page one")
    first.pack()
    popups.place(popup, root)
    popup.update()
    before = popup.winfo_reqheight()

    first.destroy()
    tk.Label(body, text="\n".join(f"Page two, line {n}" for n in range(12))).pack()
    popup.update()
    assert popup.winfo_reqheight() > before + 100  # what a wizard page change does
    popup.destroy()


def test_wider_contents_still_widen_a_popup(root):
    import tkinter as tk

    import popups

    popup = tk.Toplevel(root)
    tk.Frame(popup, width=popups.POPUP_SIZE[0] + 200, height=20).pack()
    popups.place(popup, root)
    popup.update()
    assert popup.winfo_reqwidth() == popups.POPUP_SIZE[0] + 200
    popup.destroy()


def test_file_pickers_keep_the_whole_standard_size(root):
    import popups

    width, height = popups.POPUP_SIZE
    assert popups.geometry(root).startswith(f"{width}x{height}+")


def _buttons(popup):
    import tkinter as tk
    found = []

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, tk.Button):
                found.append(child)
            walk(child)
    walk(popup)
    return {button.cget("text"): button for button in found}


def test_an_error_can_be_copied_and_reported(root):
    import popups

    seen = {}

    def press_copy():
        popup = root.winfo_children()[-1]
        buttons = _buttons(popup)
        seen["labels"] = list(buttons)
        buttons["Copy"].invoke()
        seen["clipboard"] = root.clipboard_get()
        seen["after"] = buttons["Copy"].cget("text")
        buttons["OK"].invoke()

    root.after(100, press_copy)
    assert popups.showerror("Export", "ffmpeg failed", parent=root) == "ok"
    assert seen["labels"] == ["Copy", "Send Bug Report...", "OK"]
    assert seen["clipboard"] == "Export: ffmpeg failed" and seen["after"] == "Copied"


def test_send_bug_report_opens_the_report_window_with_the_error(root, monkeypatch):
    import bug_report_dialog
    import popups

    opened = []
    monkeypatch.setattr(bug_report_dialog, "open_bug_report",
                        lambda parent, error, wait=False: opened.append((error, wait)))
    root.after(100, lambda: _buttons(root.winfo_children()[-1])["Send Bug Report..."].invoke())
    popups.showerror("Export", "ffmpeg failed", parent=root)
    assert opened == [("Export: ffmpeg failed", True)]  # waited for: the caller may close everything next


def test_the_report_windows_own_errors_only_copy(root):
    import popups

    seen = {}

    def look():
        popup = root.winfo_children()[-1]
        seen["labels"] = list(_buttons(popup))
        popup.destroy()

    root.after(100, look)
    popups.showerror("Send Bug Report", "Couldn't send", parent=root, report=False)
    assert seen["labels"] == ["Copy", "OK"]


def test_the_report_window_starts_with_the_error(root, monkeypatch):
    import tkinter as tk

    import bug_report_dialog

    dialog = bug_report_dialog.open_bug_report(root, "Export: ffmpeg failed")
    text = next(child for frame in dialog.winfo_children() for child in frame.winfo_children()
                if isinstance(child, tk.Text))
    assert "Export: ffmpeg failed" in text.get("1.0", "end")
    assert set(_buttons(dialog)) == {"Cancel", "Save to File Instead...", "Send"}
    dialog.destroy()


def test_waiting_on_the_report_window_ends_however_it_closes(root):
    import time

    import bug_report_dialog

    root.after(200, lambda: root.winfo_children()[-1].destroy())  # not its Cancel: closed from outside
    started = time.monotonic()
    bug_report_dialog.open_bug_report(root, "Export: ffmpeg failed", wait=True)
    assert time.monotonic() - started < 3
