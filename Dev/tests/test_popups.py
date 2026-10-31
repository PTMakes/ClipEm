"""popups.py: the standard width, with the height following the contents."""

import os

import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
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
