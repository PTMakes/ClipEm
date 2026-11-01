"""The "Clip names" controls - what saved clips and their copies are called
(Scripts/clip_naming.py) - shared by Settings (settings_dialog.py) and
Setup's Clip Folder page (gui.py). Classic Tk widgets, which both windows
theme (dark_theme.py)."""

import datetime
import os
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Scripts"))
import clip_naming  # noqa: E402
import dark_theme  # noqa: E402

DATE_LABELS = dict(clip_naming.DATE_PARTS)
DATE_CODES = {label: code for code, label in clip_naming.DATE_PARTS}
ID_CHOICES = ((clip_naming.ID_NUMBER, "Count up"), (clip_naming.ID_GUID, "GUID"))
PART_LABELS = {"prefix": "Custom Text:", "id": "Number:", "date": "Date and time:", "app": "Game or app:"}
HANDLE = "\u2261"
PREVIEW_APP = "Game"  # the preview's, while nothing's playing sound


class NamingForm(tk.Frame):
    """get() -> the settings as entered (check them with
    clip_naming.validate). folder() is the clip folder, for the preview's
    next number. Each part of the name has a row: its handle drags it up or
    down, its checkbox turns it on or off, the gray character after it is the
    separator, which the backend adds - it's never typed - and Reset puts it
    back as it comes, on or off too; the rows' order isn't part of any one of
    them. The date's six dropdowns are its order, the separator between them:
    picking what another one shows swaps the two."""

    def __init__(self, parent, initial, px=lambda n: n, folder=lambda: None):
        super().__init__(parent)
        self._folder = folder
        initial = clip_naming.upgrade(initial)
        number_on = initial["id"] != clip_naming.ID_NONE
        self.order = list(initial["order"])
        self.vars = {
            "prefix": tk.StringVar(value=initial["prefix"]),
            "prefix_on": tk.BooleanVar(value=initial["prefix_on"]),
            "separator": tk.StringVar(value=initial["separator"]),
            "date": tk.BooleanVar(value=initial["date"]),
            "date_format": tk.StringVar(value=initial["date_format"]),
            "number_on": tk.BooleanVar(value=number_on),
            "id": tk.StringVar(value=initial["id"] if number_on else clip_naming.ID_NUMBER),
            "number_start": tk.StringVar(value=str(initial["number_start"])),
            "number_digits": tk.StringVar(value=str(initial["number_digits"])),
            "app": tk.BooleanVar(value=initial["app"]),
            "small_suffix": tk.StringVar(value=initial["small_suffix"]),
            "tracks_suffix": tk.StringVar(value=initial["tracks_suffix"]),
            "extra_suffix": tk.StringVar(value=initial["extra_suffix"]),
        }
        self._pad = pad = {"padx": (px(6), 0), "pady": px(2)}
        self._px = px
        self._date_vars = [tk.StringVar(value=DATE_LABELS[code])
                           for code in clip_naming.date_parts(initial["date_format"])]
        self._date_shown = [var.get() for var in self._date_vars]
        self.vars["date_format"].set(self._date_format())
        self._rows = {key: self._part_row(key) for key in clip_naming.PARTS}
        self._place_rows()

        row = len(self.order)
        tk.Label(self, text="Addon Suffixes:").grid(row=row, column=1, sticky="w")
        line = tk.Frame(self)
        line.grid(row=row, column=2, sticky="w", **pad)
        for text, key in (("Multitrack:", "tracks_suffix"), ("Smaller copy:", "small_suffix"),
                          ("Post-clip:", "extra_suffix")):
            tk.Label(line, text=text).pack(side="left")
            self._separator_label(line).pack(side="left", padx=(px(4), 0))
            tk.Entry(line, textvariable=self.vars[key], width=10).pack(side="left", padx=(px(2), px(8)))
        self._reset_button(line, "tracks_suffix", "small_suffix", "extra_suffix").pack(side="left")
        row += 1

        tk.Label(self, text="Between Parts:").grid(row=row, column=1, sticky="w")
        line = tk.Frame(self)
        line.grid(row=row, column=2, sticky="w", **pad)
        tk.Entry(line, textvariable=self.vars["separator"], width=3).pack(side="left")
        self._reset_button(line, "separator").pack(side="left", padx=(px(4), 0))
        row += 1

        tk.Label(self, text="Next clip:").grid(row=row, column=1, sticky="w")
        self._preview = tk.Label(self, fg=dark_theme.MUTED, justify="left", anchor="w")
        self._preview.grid(row=row, column=2, sticky="w", **pad)
        for var in self.vars.values():
            var.trace_add("write", lambda *_args: self._show_preview())
        self._show_number()

    def _separator_label(self, parent):
        return tk.Label(parent, textvariable=self.vars["separator"], fg=dark_theme.MUTED)

    def _reset_button(self, parent, *keys):
        """Reset: these settings as they come. number_on and the date's
        dropdowns stand for "id" and "date_format"."""
        def reset():
            defaults = clip_naming.DEFAULTS
            for key in keys:
                if key == "id":
                    self.vars["number_on"].set(defaults["id"] != clip_naming.ID_NONE)
                    self.vars["id"].set(clip_naming.ID_NUMBER)
                elif key == "date_format":
                    for var, code in zip(self._date_vars, clip_naming.date_parts(defaults[key]), strict=True):
                        var.set(DATE_LABELS[code])
                else:
                    self.vars[key].set(defaults[key])
            self._show_number()

        return tk.Button(parent, text="Reset", command=reset)

    def _part_row(self, key):
        """(handle, checkbox, field) for one part of the name, not placed yet."""
        px = self._px
        handle = tk.Label(self, text=HANDLE, fg=dark_theme.MUTED, cursor="fleur")
        handle.bind("<B1-Motion>", lambda event: self._drag(key, event))
        variable = {"prefix": "prefix_on", "id": "number_on", "date": "date", "app": "app"}[key]
        check = tk.Checkbutton(self, text=PART_LABELS[key], variable=self.vars[variable],
                               command=self._show_number if key == "id" else None)
        field = tk.Frame(self)
        line = tk.Frame(field)
        line.pack(anchor="w")
        if key == "prefix":
            tk.Entry(line, textvariable=self.vars["prefix"], width=18).pack(side="left")
        elif key == "date":
            for index, var in enumerate(self._date_vars):
                if index:
                    self._separator_label(line).pack(side="left", padx=px(2))
                ttk.Combobox(line, textvariable=var, values=list(DATE_CODES), state="readonly", width=7).pack(
                    side="left")
                var.trace_add("write", lambda *_args, changed=index: self._date_picked(changed))
        elif key == "app":
            tk.Label(line, text="the one playing sound when the clip is saved", fg=dark_theme.MUTED).pack(side="left")
        else:
            for value, text in ID_CHOICES:
                tk.Radiobutton(line, text=text, value=value, variable=self.vars["id"],
                               command=self._show_number).pack(side="left", padx=(0, px(6)))
            self._number_row = tk.Frame(field)
            self._number_row.pack(anchor="w")
            tk.Label(self._number_row, text="Start at:").pack(side="left")
            tk.Spinbox(self._number_row, textvariable=self.vars["number_start"], from_=0, to=10 ** 9,
                       width=7).pack(side="left", padx=(px(4), px(8)))
            tk.Label(self._number_row, text="Digits:").pack(side="left")
            tk.Spinbox(self._number_row, textvariable=self.vars["number_digits"], from_=0, to=12, width=3).pack(
                side="left", padx=(px(4), 0))
            tk.Label(self._number_row, text="(counts on from the highest already in the folder)",
                     fg=dark_theme.MUTED).pack(side="left", padx=(px(6), 0))
        self._separator_label(line).pack(side="left", padx=(px(4), 0))
        settings = {"prefix": ("prefix_on", "prefix"), "date": ("date", "date_format"), "app": ("app",),
                    "id": ("id", "number_start", "number_digits")}[key]
        self._reset_button(line, *settings).pack(side="left", padx=(px(6), 0))
        return handle, check, field

    def _date_format(self):
        return "".join(DATE_CODES[var.get()] for var in self._date_vars)

    def _date_picked(self, index):
        """A dropdown changed: one already showing that gets what this one had."""
        picked, replaced = self._date_vars[index].get(), self._date_shown[index]
        self._date_shown[index] = picked
        for other, var in enumerate(self._date_vars):
            if other != index and var.get() == picked:
                self._date_shown[other] = replaced
                var.set(replaced)  # comes back here: nothing else shows it now
        self.vars["date_format"].set(self._date_format())

    def _place_rows(self):
        for row, key in enumerate(self.order):
            handle, check, field = self._rows[key]
            handle.grid(row=row, column=0, sticky="w")
            check.grid(row=row, column=1, sticky="w")
            field.grid(row=row, column=2, sticky="w", **self._pad)

    def _drag(self, key, event):
        """Moves the dragged part to the row under the pointer."""
        y = event.y_root - self.winfo_rooty()
        target = self.order.index(key)
        for row, other in enumerate(self.order):
            field = self._rows[other][2]
            if field.winfo_y() <= y < field.winfo_y() + field.winfo_height():
                target = row
        if target != self.order.index(key):
            self.order.remove(key)
            self.order.insert(target, key)
            self._place_rows()
            self._show_preview()

    def get(self):
        settings = {key: var.get() for key, var in self.vars.items() if key != "number_on"}
        settings["order"] = list(self.order)
        if not self.vars["number_on"].get():
            settings["id"] = clip_naming.ID_NONE
        for key in ("number_start", "number_digits"):
            try:
                settings[key] = int(settings[key])
            except ValueError:
                settings[key] = -1  # validate() says why
        return settings

    def _show_number(self):
        if self.vars["number_on"].get() and self.vars["id"].get() == clip_naming.ID_NUMBER:
            self._number_row.pack(anchor="w")
        else:
            self._number_row.pack_forget()
        self._show_preview()

    def _show_preview(self):
        settings = self.get()
        problem = clip_naming.validate(settings)
        if problem:
            self._preview.configure(text=problem, fg=dark_theme.ERROR)
            return
        folder = self._folder()
        number = clip_naming.next_number(folder, settings) if folder else settings["number_start"]
        stem = clip_naming.stem(settings, datetime.datetime.now(), number,
                                app=clip_naming.active_app() or PREVIEW_APP)
        self._preview.configure(text=f"{stem}.mp4", fg=dark_theme.MUTED)
