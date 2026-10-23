"""The color picker Settings > UI Customization opens from a color's square:
a saturation/brightness square for the chosen hue, a hue bar beside it, the
new color next to the old one, and its hex code. ask_color() returns the
picked "#rrggbb", or None if cancelled.

The square is a rectangle of the hue under two fixed gradient images (white
fading out across it, black fading in down it), so dragging the hue only
recolors that rectangle instead of redrawing every pixel."""

import base64
import colorsys
import struct
import tkinter as tk
import zlib

import dark_theme


def _png(width, height, row):
    """A PNG (base64, for tk.PhotoImage) whose row(y) is width RGBA pixels as bytes."""
    data = b"".join(b"\0" + row(y) for y in range(height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b""))
    return base64.b64encode(png)


def _hsv_to_hex(h, s, v):
    return "#" + "".join(f"{round(c * 255):02x}" for c in colorsys.hsv_to_rgb(h, s, v))


def _hex_to_hsv(color):
    return colorsys.rgb_to_hsv(*(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)))


def normalize_hex(text):
    """"#rrggbb" (lowercase) from what was typed - with or without the "#",
    or the 3-digit short form - or None if it isn't a color."""
    text = text.strip().lower()
    if not text.startswith("#"):
        text = "#" + text
    if len(text) == 4:
        text = "#" + "".join(c * 2 for c in text[1:])
    return text if dark_theme.is_hex_color(text) else None


def ask_color(parent, initial, title, px=lambda n: n):
    """Opens the picker over parent (modal) on initial ("#rrggbb"); px
    scales its sizes (ui_scale.UiScale.px). Returns the color, or None."""
    size, bar = px(220), px(22)
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(parent)
    body = tk.Frame(dialog, padx=px(14), pady=px(14))
    body.pack(fill="both", expand=True)

    h, s, v = _hex_to_hsv(initial)
    state = {"h": h, "s": s, "v": v, "result": None, "busy": False}

    square = tk.Canvas(body, width=size, height=size, highlightthickness=1,
                       highlightbackground=dark_theme.LINE, cursor="crosshair")
    square.grid(row=0, column=0, rowspan=6, sticky="n")
    hue_bar = tk.Canvas(body, width=bar, height=size, highlightthickness=1,
                        highlightbackground=dark_theme.LINE, cursor="sb_v_double_arrow")
    hue_bar.grid(row=0, column=1, rowspan=6, sticky="n", padx=(px(10), px(14)))

    last = size - 1
    whites = tk.PhotoImage(master=dialog, data=_png(size, size, lambda _y: b"".join(
        bytes((255, 255, 255, round(255 * (1 - x / last)))) for x in range(size))))
    blacks = tk.PhotoImage(master=dialog, data=_png(size, size, lambda y: bytes(
        (0, 0, 0, round(255 * y / last))) * size))
    hues = tk.PhotoImage(master=dialog, data=_png(bar, size, lambda y: bytes(
        round(c * 255) for c in (*colorsys.hsv_to_rgb(y / size, 1, 1), 1)) * bar))
    dialog.images = (whites, blacks, hues)  # Tk drops images Python no longer holds

    square.create_rectangle(0, 0, size, size, width=0, tags="hue")
    square.create_image(0, 0, image=whites, anchor="nw")
    square.create_image(0, 0, image=blacks, anchor="nw")
    r = px(6)
    # A white ring inside a black one: seen on light and dark colors alike.
    square.create_oval(0, 0, 2 * r, 2 * r, outline="#000000", width=4, tags="marker")
    square.create_oval(0, 0, 2 * r, 2 * r, outline="#ffffff", width=2, tags="marker")
    hue_bar.create_image(0, 0, image=hues, anchor="nw")
    hue_bar.create_rectangle(0, 0, bar, px(5), outline="#000000", width=4, tags="marker")
    hue_bar.create_rectangle(0, 0, bar, px(5), outline="#ffffff", width=2, tags="marker")

    # Clicking the current color goes back to it.
    tk.Label(body, text="New", fg=dark_theme.MUTED).grid(row=0, column=2, sticky="sw")
    new_swatch = tk.Frame(body, height=px(32), width=px(120), highlightthickness=1,
                          highlightbackground=dark_theme.LINE)
    new_swatch.grid(row=1, column=2, sticky="nw")
    tk.Label(body, text="Current", fg=dark_theme.MUTED).grid(row=2, column=2, sticky="sw", pady=(px(6), 0))
    old_swatch = tk.Frame(body, height=px(32), width=px(120), background=initial, cursor="hand2",
                          highlightthickness=1, highlightbackground=dark_theme.LINE)
    old_swatch.grid(row=3, column=2, sticky="nw")
    body.rowconfigure(4, weight=1)

    hex_row = tk.Frame(body)
    hex_row.grid(row=5, column=2, sticky="sw")
    tk.Label(hex_row, text="Hex:").pack(side="left")
    hex_var = tk.StringVar()
    hex_entry = tk.Entry(hex_row, textvariable=hex_var, width=9, font="TkFixedFont")
    hex_entry.pack(side="left", padx=(px(6), 0))

    def color():
        return _hsv_to_hex(state["h"], state["s"], state["v"])

    def show():
        square.itemconfigure("hue", fill=_hsv_to_hex(state["h"], 1, 1))
        x, y = state["s"] * last, (1 - state["v"]) * last
        for item in square.find_withtag("marker"):  # coords() alone moves just the first
            square.coords(item, x - r, y - r, x + r, y + r)
        y = state["h"] * last
        for item in hue_bar.find_withtag("marker"):
            hue_bar.coords(item, 1, y - px(3), bar - 1, y + px(3))
        new_swatch.configure(background=color())
        if not state["busy"]:  # not while the code is being typed
            show_hex()

    def show_hex():
        # Not read back: the rounded code would nudge the hue of a near-grey.
        state["busy"] = True
        try:
            hex_var.set(color())
        finally:
            state["busy"] = False

    def set_hex(value):
        # Grey and black have no hue of their own: keep the bar where it is.
        h, s, v = _hex_to_hsv(value)
        state.update(s=s, v=v, **({"h": h} if s and v else {}))
        show()

    def on_square(event):
        state["s"] = min(max(event.x / last, 0.0), 1.0)
        state["v"] = 1 - min(max(event.y / last, 0.0), 1.0)
        show()

    def on_hue(event):
        state["h"] = min(max(event.y / last, 0.0), 1.0)
        show()

    def on_typed(*_args):
        value = None if state["busy"] else normalize_hex(hex_var.get())
        if value:
            state["busy"] = True
            try:
                set_hex(value)
            finally:
                state["busy"] = False

    for widget, handler in ((square, on_square), (hue_bar, on_hue)):
        widget.bind("<Button-1>", handler)
        widget.bind("<B1-Motion>", handler)
    old_swatch.bind("<Button-1>", lambda _e: set_hex(initial))
    hex_var.trace_add("write", on_typed)
    hex_entry.bind("<FocusOut>", lambda _e: show_hex())  # an unfinished code shows the color again

    def finish(result):
        state["result"] = result
        dialog.destroy()

    buttons = tk.Frame(body)
    buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(px(14), 0))
    tk.Button(buttons, text="Cancel", width=10, command=lambda: finish(None)).pack(side="left", padx=px(4))
    tk.Button(buttons, text="OK", width=10, command=lambda: finish(color())).pack(side="left", padx=(px(4), 0))
    dialog.bind("<Return>", lambda _e: finish(color()))
    dialog.bind("<Escape>", lambda _e: finish(None))
    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))

    show()
    dialog.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_reqheight()) // 3
    dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    dialog.grab_set()
    dialog.focus_set()
    parent.wait_window(dialog)
    return state["result"]
