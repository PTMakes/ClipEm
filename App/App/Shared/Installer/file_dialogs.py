"""File and folder pickers for ClipEm's dark Tk windows (dark_theme.py).

Tk's own picker on Linux hard-codes a white file list, which looks broken in a
dark window. There, the desktop's native picker (kdialog or zenity) is used
when one is installed, in the system's own theme. Everywhere else - Windows and
macOS already show their native pickers - and without either tool, this is
tkinter.filedialog. Same arguments and results as the tkinter functions:
the chosen path, or "" if cancelled. On Linux, every picker opens at the
standard popup size (popups.py)."""

import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog

import popups


def _tool():
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    order = ("kdialog", "zenity") if "KDE" in desktop else ("zenity", "kdialog")
    for name in order:
        if shutil.which(name):
            return name
    return None


def _start_path(initialdir, initialfile=None):
    folder = initialdir if initialdir and os.path.isdir(initialdir) else os.path.expanduser("~")
    return os.path.join(folder, initialfile or "")


def _run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return ""  # cancelled
    return result.stdout.strip("\n")


def _native(kind, title, initialdir, initialfile, filetypes):
    """The picked path, "" if cancelled, None if the native picker isn't usable."""
    tool = _tool()
    if tool is None:
        return None
    start = _start_path(initialdir, initialfile)
    # Both tools scale their windows by the desktop's scale themselves.
    width, height = popups.POPUP_SIZE
    patterns = [(label, pattern) for label, pattern in (filetypes or [])]
    if tool == "kdialog":
        flag = {"open": "--getopenfilename", "dir": "--getexistingdirectory", "save": "--getsavefilename"}[kind]
        command = ["kdialog", flag, start]
        if kind != "dir" and patterns:
            command.append("\n".join(f"{pattern}|{label}" for label, pattern in patterns))
        command += ["--title", title or "", "--geometry", f"{width}x{height}"]
    else:
        command = ["zenity", "--file-selection", f"--title={title or ''}", f"--filename={start}",
                   f"--width={width}", f"--height={height}"]
        if kind == "dir":
            command.append("--directory")
        elif kind == "save":
            command += ["--save", "--confirm-overwrite"]
        if kind != "dir":
            command += [f"--file-filter={label} | {pattern}" for label, pattern in patterns]
    return _run(command)


def _use_native():
    return sys.platform.startswith("linux")


def _fit_tk_picker(parent, name):
    """Resizes Tk's own picker (the toplevel `name` under parent, made by
    tk_getOpenFile & co once called) to the standard popup size once it's
    showing; Tk otherwise sizes it to fit its widgets. Only Linux's picker
    is Tk's - Windows and macOS show the system one."""
    root = parent.winfo_toplevel() if parent is not None else tk._default_root
    if root is None or root.tk.call("tk", "windowingsystem") != "x11":
        return
    owner = parent if parent is not None else root
    path = ("" if str(owner) == "." else str(owner)) + "." + name

    def fit(tries):
        if root.tk.call("winfo", "exists", path) and root.tk.call("winfo", "ismapped", path):
            root.tk.call("wm", "geometry", path, popups.geometry(owner))
        elif tries:
            root.after(20, fit, tries - 1)

    root.after(20, fit, 100)


def askopenfilename(parent=None, title=None, initialdir=None, filetypes=None):
    if _use_native():
        path = _native("open", title, initialdir, None, filetypes)
        if path is not None:
            return path
    _fit_tk_picker(parent, "__tk_filedialog")
    return filedialog.askopenfilename(parent=parent, title=title, initialdir=initialdir,
                                      filetypes=filetypes or ())


def askdirectory(parent=None, title=None, initialdir=None):
    if _use_native():
        path = _native("dir", title, initialdir, None, None)
        if path is not None:
            return path
    _fit_tk_picker(parent, "__tk_choosedir")
    return filedialog.askdirectory(parent=parent, title=title, initialdir=initialdir)


def asksaveasfilename(parent=None, title=None, initialdir=None, initialfile=None, defaultextension=None,
                      filetypes=None):
    if _use_native():
        path = _native("save", title, initialdir, initialfile, filetypes)
        if path is not None:
            if path and defaultextension and not os.path.splitext(path)[1]:
                path += defaultextension
            return path
    _fit_tk_picker(parent, "__tk_filedialog")
    return filedialog.asksaveasfilename(parent=parent, title=title, initialdir=initialdir,
                                        initialfile=initialfile, defaultextension=defaultextension,
                                        filetypes=filetypes or ())
