"""Uninstalling ClipEm, from Settings > Scripts > Troubleshooting.

What's removed is what the Windows installer's own uninstaller removes
(Dev/installer/windows/ClipEmSetup.nsi): App/ - ClipEm, its OBS and Python,
their settings and caches - and the shortcuts next to it, then the ClipEm
folder itself if that leaves it empty. Outside the folder: this install's
boot autostart entry, its Start Menu / application menu entry, its desktop
shortcut and its windows' hidden .desktop entries. The clips are never
touched: the Clips folder isn't removed, and nothing is if the clip folder
is inside App/.

Settings closes OBS, the tray and the clip editor first (as for a
reinstall), removes the entries outside the folder (remove_entries), then:
  Windows: hands over to Uninstall.exe, the installer's uninstaller (it
           removes the files from a copy of itself, so nothing of ClipEm
           runs while they go). An install without one (a copy of the folder)
           can't remove its own running Python: Settings opens the folder
           for the user to delete instead.
  Linux:   runs this file detached - `uninstall.py --files --after <pid>` -
           which waits for Settings to exit, removes the files (a running
           program's files can be deleted there) and says it's done.

Never runs on a developer checkout (a folder in a git checkout).
"""

import os
import platform
import shutil
import subprocess
import sys
import time

USAGE = """
Usage: uninstall.py --files [--after PID] | --entries

Uninstalls ClipEm - Settings > Scripts/Troubleshooting > Uninstall ClipEm
runs this. Your clips are never removed.

  --files        remove ClipEm's files from its folder (Linux), once the
                 process --after names (Settings) has exited
  --entries      remove its autostart, shortcuts and menu entries outside the
                 folder (the Windows uninstaller runs this)
  -h, --help     show this help and exit
"""
if __name__ == "__main__" and {"-h", "--help"} & set(sys.argv[1:]):
    print(USAGE.strip())
    sys.exit(0)

INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, INSTALLER_DIR)

import autostart  # noqa: E402
from clipem_common import ROOT_DIR, pid_alive  # noqa: E402

# What's removed from the ClipEm folder (ClipEmSetup.nsi's list, plus the
# Linux launcher and uninstall script): nothing else in it is ClipEm's.
REMOVED_NAMES = ("App", "ClipEm.lnk", "ClipEm.desktop", "UninstallAutostartWindows.bat",
                 "UninstallAutostartLinux.sh", "Uninstall.exe")
UNINSTALLER_EXE = "Uninstall.exe"
EXIT_WAIT_SECONDS = 30


def _inside(path, folder):
    path, folder = (os.path.normcase(os.path.realpath(p)) for p in (path, folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _checkout(path):
    """The git checkout path is in (a developer's ClipEm: its App/ is the
    ClipEm folder), or None."""
    path = os.path.realpath(path)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def refusal(root_dir=ROOT_DIR, clip_folder=None):
    """Why this folder can't be uninstalled, or None if it can."""
    checkout = _checkout(root_dir)
    if checkout:
        return f"{root_dir} is in a development checkout of ClipEm ({checkout}) - not uninstalling that."
    if not os.path.isdir(os.path.join(root_dir, "App")):
        return f"{root_dir} has no App folder - it doesn't look like a ClipEm install."
    if clip_folder and _inside(clip_folder, os.path.join(root_dir, "App")):
        return (f"Your clip folder ({clip_folder}) is inside ClipEm's App folder, which uninstalling deletes. "
                "Move your clips elsewhere first (Settings > Basic Settings > Clip folder).")
    return None


def windows_uninstaller(root_dir=ROOT_DIR):
    """The installer's Uninstall.exe, or None (an install that's a copy)."""
    path = os.path.join(root_dir, UNINSTALLER_EXE)
    return path if os.path.isfile(path) else None


def remove_entries(log=print):
    """Removes this install's entries outside its folder: the boot autostart
    entry, the Start Menu / menu entry, the desktop shortcut and the windows'
    hidden .desktop entries (another ClipEm folder's are left alone)."""
    own = [entry for entry in autostart.find_autostart_entries() if entry["this_install"]]
    if own:
        autostart.remove_autostart_entries(own)
    for kind in (autostart.SHORTCUT_START_MENU, autostart.SHORTCUT_DESKTOP):
        try:
            autostart.uninstall_shortcut(kind)
        except OSError as exc:
            log(f"Couldn't remove the {kind.replace('_', ' ')} shortcut: {exc}")
    autostart.remove_start_menu_shortcut()
    autostart.remove_window_desktop_entries()


def remove_files(root_dir=ROOT_DIR, log=print):
    """Removes ClipEm's files from root_dir (REMOVED_NAMES), then root_dir if
    that leaves it empty. Returns [(name, error)] of what couldn't go."""
    failed = []
    for name in REMOVED_NAMES:
        path = os.path.join(root_dir, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.lexists(path):
                os.remove(path)
            else:
                continue
            log(f"Removed {path}")
        except OSError as exc:
            failed.append((name, str(exc)))
            log(f"Couldn't remove {path}: {exc}")
    try:
        os.rmdir(root_dir)  # only if empty: the clips (and anything else) stay
        log(f"Removed {root_dir}")
    except OSError:
        pass
    return failed


def start_file_removal(python=sys.executable, after_pid=None):
    """Linux: runs this file detached to remove the files once after_pid
    (Settings) has exited."""
    cmd = [python, os.path.abspath(__file__), "--files"]
    if after_pid:
        cmd += ["--after", str(after_pid)]
    log_path = os.path.join(os.path.expanduser("~"), ".cache", "clipem-uninstall.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "ab") as log_file:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file,
                         start_new_session=True, cwd=os.path.expanduser("~"))
    return log_path


def windows_uninstaller_command(path, after_pid):
    """PowerShell that waits for after_pid (Settings, which runs on App's
    Python - Uninstall.exe couldn't remove that while it runs) to exit, then
    starts Uninstall.exe (which shows its own confirmation)."""
    def quoted(text):
        return "'" + str(text).replace("'", "''") + "'"
    script = (f"Wait-Process -Id {int(after_pid)} -Timeout {EXIT_WAIT_SECONDS} -ErrorAction SilentlyContinue; "
              f"Start-Process -FilePath {quoted(path)} -WorkingDirectory {quoted(os.path.dirname(path))}")
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def start_windows_uninstaller(path, after_pid):
    subprocess.Popen(windows_uninstaller_command(path, after_pid), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     # CREATE_NO_WINDOW (a hidden console: PowerShell doesn't run with none, DETACHED_PROCESS)
                     # | CREATE_NEW_PROCESS_GROUP
                     creationflags=0x08000000 | 0x00000200)


def _wait_for_exit(pid, seconds=EXIT_WAIT_SECONDS):
    deadline = time.monotonic() + seconds
    while pid and pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)


def _done_message(failed, clip_folder):
    if failed:
        names = ", ".join(name for name, _error in failed)
        text = f"ClipEm was uninstalled, but {names} couldn't be removed from {ROOT_DIR} - delete it yourself."
    else:
        text = "ClipEm was uninstalled."
    if clip_folder and os.path.isdir(clip_folder):
        text += f"\n\nYour clips are still in {clip_folder}."
    return text


def main(argv):
    if "--entries" in argv:
        remove_entries()
        return 0
    if "--files" not in argv or platform.system() == "Windows":
        print(USAGE.strip())
        return 2
    after = argv[argv.index("--after") + 1] if "--after" in argv[:-1] else None
    _wait_for_exit(int(after) if after and after.isdigit() else None)
    try:
        import obs_config
        clip_folder = obs_config.load_clip_folder()
    except Exception:
        clip_folder = None
    reason = refusal(ROOT_DIR, clip_folder)
    if reason:
        print(f"Not uninstalling: {reason}")
        return 1
    # The window first: Tk's own files are in App/ too (the bundled Python).
    root = None
    try:
        import tkinter as tk

        import dark_theme
        import popups
        root = tk.Tk(className="ClipEm")
        root.withdraw()
        dark_theme.apply_theme(root)
    except Exception as exc:
        print(f"No window to say it's done: {exc}")
    failed = remove_files(ROOT_DIR)
    message = _done_message(failed, clip_folder)
    print(message)
    if root is not None:
        try:
            popups.showinfo("ClipEm Uninstalled", message, parent=root)
            root.destroy()
        except Exception as exc:
            print(f"Couldn't show it's done: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
