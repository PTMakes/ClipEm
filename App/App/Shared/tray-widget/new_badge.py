"""The "New" badge on a setting Settings is highlighting, and the confetti
it pops into when clicked.

Tk can't draw see-through over other widgets, so each piece of confetti is a
tiny widget of its own.
"""

import io
import math
import os
import random
import tkinter as tk

from PIL import Image

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NewIcon.png")
SIZE = 40  # 96 DPI pixels, as GAP
GAP = 6  # between the setting and the badge

# The look of https://lottiefiles.com/free-animation/confetti-Ljf8PgS2P4:
# its colors, piece count and 2 seconds at 30 fps.
COLORS = ("#50e3c2", "#ff1a61", "#2bb7ff", "#ffe41d", "#7ed321", "#ba1dff")
PIECES = 50
FRAME_MS = 33
FRAMES = 60
SPEED = (4.0, 18.0)  # 96 DPI pixels a frame, as GRAVITY
GRAVITY = 0.15
DRAG = 0.93


def _picture(master, size):
    """The badge's picture at `size` pixels (a PhotoImage: keep a reference)."""
    with Image.open(ICON_PATH) as icon:
        small = icon.convert("RGBA").resize((size, size), Image.LANCZOS)
    png = io.BytesIO()
    small.save(png, "PNG")
    return tk.PhotoImage(master=master, data=png.getvalue())


def add_badge(widget, px, on_click):
    """Puts the badge right after `widget`, vertically centered with it, and
    returns it. widget has to be packed side="left": what's beside it moves
    over to make room, and back when the badge goes."""
    parent = widget.master
    picture = _picture(parent, px(SIZE))
    badge = tk.Label(parent, image=picture, background=parent.cget("background"), borderwidth=0,
                     highlightthickness=0, padx=0, pady=0, cursor="hand2")
    badge.picture = picture  # on the badge: Tk drops unreferenced ones
    badge.pack(side="left", after=widget, padx=(px(GAP), 0))
    badge.bind("<Button-1>", lambda _event: on_click())
    return badge


def burst(root, x, y, px):
    """Confetti from x, y (pixels in root), which clears itself away."""
    pieces = []
    for _ in range(PIECES):
        angle = random.uniform(0, math.tau)
        speed = px(random.uniform(*SPEED))
        size = (px(random.randint(4, 8)), px(random.randint(5, 10)))
        piece = tk.Frame(root, background=random.choice(COLORS), width=size[0], height=size[1])
        piece.place(x=x, y=y)
        pieces.append({"widget": piece, "x": x, "y": y, "dx": speed * math.cos(angle), "dy": speed * math.sin(angle),
                       "size": size, "spin": random.uniform(0.15, 0.5), "phase": random.uniform(0, math.tau)})

    def step(frame):
        if frame >= FRAMES:
            for piece in pieces:
                piece["widget"].destroy()
            return
        for piece in pieces:
            piece["dx"] *= DRAG
            piece["dy"] = piece["dy"] * DRAG + px(GRAVITY)
            piece["x"] += piece["dx"]
            piece["y"] += piece["dy"]
            # Tumbling: seen edge-on every half turn.
            height = piece["size"][1] * abs(math.cos(piece["phase"] + frame * piece["spin"]))
            piece["widget"].place_configure(x=int(piece["x"]), y=int(piece["y"]))
            piece["widget"].config(height=max(int(height), 1))
        root.after(FRAME_MS, step, frame + 1)

    step(0)
