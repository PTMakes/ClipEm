"""
Removes the OS-level boot/login autostart entry created by Setup's
autostart step (see apply_boot_autostart) without re-running the rest of
Setup. UninstallAutostart.bat/.sh are thin per-OS shims that just find a
Python interpreter and exec this file, same pattern as Setup*.

Safe to run any time - a no-op if nothing was ever registered. Only removes
this ClipEm folder's own entry, never another copy's.
"""

import os
import sys

INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, INSTALLER_DIR)
import autostart  # noqa: E402


def main():
    entries = autostart.find_autostart_entries()
    own = [entry for entry in entries if entry["this_install"]]
    if own:
        autostart.remove_autostart_entries(own)
    else:
        print("No boot autostart entry was set up for this ClipEm folder - nothing to do.")
    others = [entry for entry in entries if not entry["this_install"]]
    for entry in others:
        # Another ClipEm folder's (or an older version's) - not ours to remove.
        print(f"Left in place: {entry['path']} starts ClipEm from somewhere else.")


if __name__ == "__main__":
    main()
