"""The "Clip names" controls - what saved clips and their copies are called
(Scripts/clip_naming.py) - shared by Settings (settings_dialog.py) and
Setup's Clip Folder page (gui.py). Classic Tk widgets, which both windows
theme (dark_theme.py)."""

import datetime
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Scripts"))
import clip_naming  # noqa: E402
import dark_theme  # noqa: E402

DATE_CODES = "%Y year, %m month, %d day, %H hour, %M minute, %S second"
ID_CHOICES = ((clip_naming.ID_NONE, "None"), (clip_naming.ID_NUMBER, "Count up"), (clip_naming.ID_GUID, "GUID"))


class NamingForm(tk.Frame):
    """get() -> the settings as entered (check them with
    clip_naming.validate). folder() is the clip folder, for the preview's
    next number."""

    def __init__(self, parent, initial, px=lambda n: n, folder=lambda: None):
        super().__init__(parent)
        self._folder = folder
        initial = {**clip_naming.DEFAULTS, **(initial or {})}
        self.vars = {
            "prefix": tk.StringVar(value=initial["prefix"]),
            "separator": tk.StringVar(value=initial["separator"]),
            "date": tk.BooleanVar(value=initial["date"]),
            "date_format": tk.StringVar(value=initial["date_format"]),
            "id": tk.StringVar(value=initial["id"]),
            "number_start": tk.StringVar(value=str(initial["number_start"])),
            "number_digits": tk.StringVar(value=str(initial["number_digits"])),
            "small_suffix": tk.StringVar(value=initial["small_suffix"]),
            "tracks_suffix": tk.StringVar(value=initial["tracks_suffix"]),
            "extra_suffix": tk.StringVar(value=initial["extra_suffix"]),
        }
        pad = {"padx": (px(6), 0), "pady": px(2)}
        row = 0

        def label(text):
            tk.Label(self, text=text).grid(row=row, column=0, sticky="w")

        label("Name starts with:")
        line = tk.Frame(self)
        line.grid(row=row, column=1, sticky="w", **pad)
        tk.Entry(line, textvariable=self.vars["prefix"], width=18).pack(side="left")
        row += 1

        label("Number:")
        line = tk.Frame(self)
        line.grid(row=row, column=1, sticky="w", **pad)
        for value, text in ID_CHOICES:
            tk.Radiobutton(line, text=text, value=value, variable=self.vars["id"],
                           command=self._show_number).pack(side="left", padx=(0, px(6)))
        row += 1
        self._number_row = tk.Frame(self)
        self._number_row.grid(row=row, column=1, sticky="w", **pad)
        tk.Label(self._number_row, text="Start at:").pack(side="left")
        tk.Spinbox(self._number_row, textvariable=self.vars["number_start"], from_=0, to=10 ** 9,
                   width=7).pack(side="left", padx=(px(4), px(8)))
        tk.Label(self._number_row, text="Digits:").pack(side="left")
        tk.Spinbox(self._number_row, textvariable=self.vars["number_digits"], from_=0, to=12, width=3).pack(
            side="left", padx=(px(4), 0))
        tk.Label(self._number_row, text="(counts on from the highest already in the folder)",
                 fg=dark_theme.MUTED).pack(side="left", padx=(px(6), 0))
        row += 1

        tk.Checkbutton(self, text="Date and time:", variable=self.vars["date"]).grid(row=row, column=0, sticky="w")
        line = tk.Frame(self)
        line.grid(row=row, column=1, sticky="w", **pad)
        tk.Entry(line, textvariable=self.vars["date_format"], width=22).pack(side="left")
        tk.Label(line, text=DATE_CODES, fg=dark_theme.MUTED).pack(side="left", padx=(px(6), 0))
        row += 1

        label("Addon Suffixes:")
        line = tk.Frame(self)
        line.grid(row=row, column=1, sticky="w", **pad)
        tk.Label(line, text="Multitrack:").pack(side="left")
        tk.Entry(line, textvariable=self.vars["tracks_suffix"], width=10).pack(side="left", padx=(px(4), px(8)))
        tk.Label(line, text="Smaller copy:").pack(side="left")
        tk.Entry(line, textvariable=self.vars["small_suffix"], width=10).pack(side="left", padx=(px(4), px(8)))
        tk.Label(line, text="Post-clip:").pack(side="left")
        tk.Entry(line, textvariable=self.vars["extra_suffix"], width=10).pack(side="left", padx=(px(4), 0))
        row += 1

        label("Between Parts:")
        tk.Entry(self, textvariable=self.vars["separator"], width=3).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        label("Next clip:")
        self._preview = tk.Label(self, fg=dark_theme.MUTED, justify="left", anchor="w")
        self._preview.grid(row=row, column=1, sticky="w", **pad)
        for var in self.vars.values():
            var.trace_add("write", lambda *_args: self._show_preview())
        self._show_number()

    def get(self):
        settings = {key: var.get() for key, var in self.vars.items()}
        for key in ("number_start", "number_digits"):
            try:
                settings[key] = int(settings[key])
            except ValueError:
                settings[key] = -1  # validate() says why
        return settings

    def _show_number(self):
        if self.vars["id"].get() == clip_naming.ID_NUMBER:
            self._number_row.grid()
        else:
            self._number_row.grid_remove()
        self._show_preview()

    def _show_preview(self):
        settings = self.get()
        problem = clip_naming.validate(settings)
        if problem:
            self._preview.configure(text=problem, fg=dark_theme.ERROR)
            return
        folder = self._folder()
        number = clip_naming.next_number(folder, settings) if folder else settings["number_start"]
        stem = clip_naming.stem(settings, datetime.datetime.now(), number)
        self._preview.configure(
            text=(f"{stem}.mp4\naddons: {settings['tracks_suffix']}.mkv, {settings['small_suffix']}.mp4, "
                  f"{settings['extra_suffix']}.mkv"),
            fg=dark_theme.MUTED)
