"""
The release notes of every version, shipped in Shared/release_notes.json:
[{"version", "date", "sections": {heading: [note, ...]}}], newest first.
Written by hand: add the coming version's entry at the top, before releasing it.

Shown after an update (run.py) and from Settings > Check for Updates' Release
Notes button.
"""

import os

import updater
from clipem_common import SHARED_DIR, load_json

RELEASE_NOTES_PATH = os.path.join(SHARED_DIR, "release_notes.json")
NO_NOTES = "No release notes for this version."


def load(path=None):
    """Every version's notes, newest first ([] if the file is missing or broken)."""
    entries = load_json(path or RELEASE_NOTES_PATH)
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("version") and isinstance(e.get("sections"), dict)
            and e["sections"]]


def since(entries, old, new):
    """What an update from `old` to `new` brought, newest first. old None
    (it isn't known): only `new`'s. A dev build counts as its coming version."""
    new = new.split("-dev.")[0]
    if old is None:
        return [e for e in entries if e["version"] == new]
    return [e for e in entries
            if updater.is_newer(e["version"], old) and not updater.is_newer(e["version"], new)]


def lines(entries, versions=True):
    """[(style, text)] for notes_box: each entry's sections and notes, under
    its version if `versions`."""
    parts = []
    for entry in entries:
        if versions:
            parts.append(("version", f"{entry['version']}  ({entry['date']})" if entry.get("date") else entry["version"]))
        for heading, notes in entry["sections"].items():
            parts.append(("section", heading))
            parts += [("note", f"\u2022 {note}") for note in notes]
            parts.append((None, ""))
    return parts


def notes_box(parent, parts, width=62, height=8):
    """A read-only, scrolling text box holding parts ([(style or None, text)],
    a line each - see lines()), in a new frame that's returned to be packed.
    Scrolls so a long read can't push a window's buttons off the screen."""
    import tkinter as tk
    from tkinter import ttk

    box = tk.Frame(parent)
    text = tk.Text(box, width=width, height=height, wrap="word")
    scroll = ttk.Scrollbar(box, command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    text.tag_configure("version", font=("", 12, "bold"), spacing1=6, spacing3=4)
    text.tag_configure("section", font=("", 10, "bold"))
    text.tag_configure("note", lmargin2=12)  # wrapped lines start after the bullet
    parts = list(parts)
    if not parts[-1][1]:
        parts.pop()  # the last section's blank line
    for index, (style, line) in enumerate(parts):
        text.insert("end", ("\n" if index else ""))
        text.insert("end", line, style or ())
    text.config(state="disabled")
    return box
