#!/usr/bin/env python3
"""Unpacks python.org's tcltk.msi on Linux into a zip laid out for the
embeddable Windows Python - what setup.py's ensure_windows_tkinter would
otherwise do on the user's machine with `msiexec /a`.

Usage: extract_tcltk.py <tcltk.msi> <out.zip>

The zip's root maps onto App\\Windows\\Python\\:
    _tkinter.pyd, tcl86t.dll, tk86t.dll,
    zlib1.dll (tcl86t.dll links it)       (the MSI's DLLs\\)
    tcl\\...                               (Tcl/Tk script libraries)
    Lib\\tkinter\\...                       (the tkinter package, minus tests)
"""

import os
import sys
import tempfile
import zipfile

from msi_tables import extract_msi

DLLS = {"_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll"}
SKIP_DIR_PARTS = {"__pycache__", "test", "demos"}


def main():
    msi_path, out_zip = sys.argv[1:3]
    with tempfile.TemporaryDirectory() as tmp:
        count = 0
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as out:
            for parts, name, src in extract_msi(msi_path, tmp):
                if parts == ["DLLs"] and name in DLLS:
                    dest = name
                elif parts[:1] == ["tcl"] or parts[:2] == ["Lib", "tkinter"]:
                    if SKIP_DIR_PARTS & set(parts) or name.endswith(".lib"):
                        continue
                    dest = "/".join(parts + [name])
                else:
                    continue
                out.write(src, dest)
                count += 1

    with zipfile.ZipFile(out_zip) as check:
        names = set(check.namelist())
    required = DLLS | {"tcl/tcl8.6/init.tcl", "tcl/tk8.6/tk.tcl", "Lib/tkinter/__init__.py"}
    missing = sorted(required - names)
    if missing:
        sys.exit(f"{msi_path} didn't yield {', '.join(missing)} - has the MSI's layout changed?")
    print(f"Bundling Tcl/Tk: {count} files from {os.path.basename(msi_path)} -> {out_zip}")


if __name__ == "__main__":
    main()
