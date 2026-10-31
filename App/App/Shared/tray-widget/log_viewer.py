"""Settings > Troubleshooting's View Logs window: every ClipEm log, OBS log
and OBS crash report, one at a time. bug_report.py finds and reads them, so
the websocket password stays masked here too."""

import os
import tkinter as tk
from tkinter import ttk

import bug_report
import dark_theme
import popups

LIST_WIDTH = 34  # characters
TEXT_SIZE = (96, 30)  # characters, lines


def open_log_viewer(root, px, reveal_file):
    """Opens the window over root, or brings an open one to the front.
    reveal_file(path) shows a log in the system's file manager."""
    existing = getattr(root, "log_viewer", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        existing.focus_set()
        return existing

    dialog = root.log_viewer = tk.Toplevel(root)
    dialog.title("Logs")
    dialog.transient(root)
    body = tk.Frame(dialog, padx=px(12), pady=px(12))
    body.pack(fill="both", expand=True)

    row = tk.Frame(body)
    row.pack(side="bottom", fill="x", pady=(px(10), 0))
    panes = tk.Frame(body)
    panes.pack(side="top", fill="both", expand=True)

    side = tk.Frame(panes)
    side.pack(side="left", fill="y", padx=(0, px(10)))
    files = tk.Listbox(side, width=LIST_WIDTH, exportselection=False, activestyle="none")
    files_scroll = ttk.Scrollbar(side, command=files.yview)
    files.configure(yscrollcommand=files_scroll.set)
    files_scroll.pack(side="right", fill="y")
    files.pack(side="left", fill="y")

    reader = tk.Frame(panes)
    reader.pack(side="left", fill="both", expand=True)
    text = tk.Text(reader, width=TEXT_SIZE[0], height=TEXT_SIZE[1], wrap="none", state="disabled", font="TkFixedFont")
    text_scroll = ttk.Scrollbar(reader, command=text.yview)
    across = ttk.Scrollbar(reader, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=text_scroll.set, xscrollcommand=across.set)
    across.pack(side="bottom", fill="x")
    text_scroll.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)

    rows = []  # one per listbox line: a log's path, or None for a heading
    chosen = {"path": None}

    def show(path):
        chosen["path"] = path
        text.config(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", bug_report.read_log(path))
        text.config(state="disabled")
        text.see("end")  # the newest lines
        text.xview_moveto(0)

    def on_select(_event=None):
        selection = files.curselection()
        path = rows[selection[0]] if selection else None
        if path is None:  # a heading: stay on the open log
            files.selection_clear(0, "end")
            if chosen["path"] in rows:
                files.selection_set(rows.index(chosen["path"]))
            return
        if path != chosen["path"]:
            show(path)

    def refresh():
        current = chosen["path"]
        files.delete(0, "end")
        rows.clear()
        for group, paths in bug_report.log_files():
            files.insert("end", group)
            files.itemconfig("end", foreground=dark_theme.MUTED, selectforeground=dark_theme.MUTED,
                             selectbackground=dark_theme.PANEL)
            rows.append(None)
            for path in paths:
                files.insert("end", f"  {os.path.basename(path)}")
                rows.append(path)
        target = current if current in rows else next((path for path in rows if path), None)
        if target is None:
            chosen["path"] = None
            text.config(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", "No logs found.")
            text.config(state="disabled")
            return
        files.selection_set(rows.index(target))
        files.see(rows.index(target))
        show(target)

    def on_reveal():
        if chosen["path"]:
            reveal_file(chosen["path"])

    tk.Button(row, text="Refresh", width=10, command=refresh).pack(side="left")
    tk.Button(row, text="Show in Folder", command=on_reveal).pack(side="left", padx=(px(4), 0))
    tk.Button(row, text="Close", width=10, command=dialog.destroy).pack(side="right")

    files.bind("<<ListboxSelect>>", on_select)
    dialog.bind("<Escape>", lambda _event: dialog.destroy())
    refresh()
    popups.place(dialog, root)
    return dialog
