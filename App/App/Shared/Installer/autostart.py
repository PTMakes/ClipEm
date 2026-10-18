"""
OS integration: the boot/login autostart entry and the desktop shortcut
Setup creates. The only places this otherwise portable app writes outside
its own folder (autostart) or runs another interpreter's scripting host
(PowerShell/AppleScript), so every path interpolated into those is quoted
for the target language here rather than pasted in raw - a folder named
e.g. "O'Brien" or containing spaces must not break (or inject into) them.
"""

import base64
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape as xml_escape

from clipem_common import APP_DIR, INSTALLER_DIR, ROOT_DIR, SHARED_DIR, load_run_config, save_run_config

RUN_PY = os.path.join(INSTALLER_DIR, "run.py")
MACOS_LAUNCH_AGENT_LABEL = "com.ptmakes.clipem"
# The tray widget's AppUserModelID, also stamped on the Start Menu shortcut
# so Windows names its notifications "ClipEm" - see set_shortcut_app_id.
WINDOWS_APP_ID = "PTMakes.ClipEm"
# Bump to have every Windows install rewrite its Start Menu entry on its next
# launch (ensure_start_menu_entry) - e.g. when what the entry carries changes.
# Kept in run_config.json as "start_menu_registered".
START_MENU_REGISTRATION = 1
# Launches that try it before giving up (until Setup runs again): where it
# can't work, each try would hold up starting ClipEm for PowerShell.
START_MENU_MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Quoting helpers
# ---------------------------------------------------------------------------

def ps_quote(value):
    """A PowerShell single-quoted string literal. Inside one, the only
    special characters are the single quotes themselves - including the
    typographic ones PowerShell also accepts - which are escaped by doubling."""
    return "'" + re.sub("(['‘’‚‛])", r"\1\1", str(value)) + "'"


def run_powershell(script, **kwargs):
    """Runs `script` via -EncodedCommand, so nothing in it goes through
    Windows command-line argument parsing (which mangles embedded quotes).
    Never with a console window: from pythonw (the tray, Settings) one would
    flash up on screen."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], **kwargs
    )


def desktop_exec_arg(value):
    """One argument of a .desktop file's Exec= key, per the Desktop Entry
    spec: double-quoted with ", `, $ and \\ backslash-escaped, then the
    string-level escape applied on top (so a literal backslash becomes four),
    and % doubled since it introduces field codes."""
    quoted = re.sub(r'(["`$\\])', r"\\\1", str(value))
    return '"' + quoted.replace("\\", "\\\\").replace("%", "%%") + '"'


def applescript_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def windows_shortcut_script(shortcut_path, target, arguments, working_dir, icon=None):
    lines = [
        "$WshShell = New-Object -ComObject WScript.Shell",
        f"$Shortcut = $WshShell.CreateShortcut({ps_quote(shortcut_path)})",
        f"$Shortcut.TargetPath = {ps_quote(target)}",
        f"$Shortcut.Arguments = {ps_quote(arguments)}",
        f"$Shortcut.WorkingDirectory = {ps_quote(working_dir)}",
    ]
    if icon:
        lines.append(f"$Shortcut.IconLocation = {ps_quote(icon)}")
    lines.append("$Shortcut.Save()")
    return "\n".join(lines)


def windows_command_line_arg(value):
    """Quotes one argument for a Windows command line (a shortcut's
    Arguments field) - subprocess.list2cmdline implements the MSVCRT rules
    python.exe itself parses with."""
    return subprocess.list2cmdline([str(value)])


# ---------------------------------------------------------------------------
# Interpreters
# ---------------------------------------------------------------------------

def find_linux_python_bin():
    """Login/autostart sessions often have a bare-bones PATH, so resolve an
    absolute interpreter path once at setup time instead of trusting PATH
    at boot - prefer the bundled portable Python if present."""
    bundled = os.path.join(APP_DIR, "Linux", "python", "bin", "python3")
    if os.path.exists(bundled):
        return bundled
    return sys.executable or shutil.which("python3")


def find_windows_python_root():
    """The embeddable zip's own internal layout has varied (flat vs. a
    nested python-X.Y.Z-embed-amd64/ folder depending on how it was
    extracted), so locate python.exe instead of assuming a fixed path."""
    windows_python_dir = os.path.join(APP_DIR, "Windows", "Python")
    if not os.path.isdir(windows_python_dir):
        return None
    for dirpath, _dirnames, filenames in os.walk(windows_python_dir):
        if "python.exe" in filenames:
            return dirpath
    return None


# ---------------------------------------------------------------------------
# Boot autostart
# ---------------------------------------------------------------------------

def autostart_entry_path():
    """Where the OS looks for boot/login-autostart entries. None if this
    platform isn't handled."""
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return os.path.join(
            appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "ClipEm.lnk",
        )
    if system == "Darwin":
        return os.path.expanduser(f"~/Library/LaunchAgents/{MACOS_LAUNCH_AGENT_LABEL}.plist")
    if system == "Linux":
        return os.path.expanduser("~/.config/autostart/clipem.desktop")
    return None


# Launchers older ClipEm versions' autostart entries ran instead of run.py.
LEGACY_LAUNCHER_NAMES = ("RunLinux.sh", "RunWindows.bat", "RunMac.command")


def _autostart_dir():
    path = autostart_entry_path()
    return os.path.dirname(path) if path else None


def _points_at_clipem(name, command):
    """Whether an autostart entry (file name + what it runs) is a ClipEm
    one: named after ClipEm, or running a ClipEm run.py / old launcher."""
    text = f"{name}\n{command}".lower()
    if "clipem" in text:
        return True
    if "run.py" in text and "installer" in text:
        return True
    return any(launcher.lower() in text for launcher in LEGACY_LAUNCHER_NAMES)


def _points_at_this_install(command):
    """Whether the entry's command runs this install's run.py - as written
    in a .lnk/plist/.desktop file, i.e. possibly XML- or Exec=-escaped."""
    forms = {RUN_PY, xml_escape(RUN_PY), desktop_exec_arg(RUN_PY)[1:-1]}
    command = os.path.normcase(command)
    return any(os.path.normcase(form) in command for form in forms)


def _windows_startup_shortcuts(folder):
    """[(path, "target arguments")] for every .lnk in folder, read through
    WScript.Shell (a .lnk is binary). Empty if PowerShell isn't usable."""
    script = (
        "$sh = New-Object -ComObject WScript.Shell\n"
        f"Get-ChildItem -LiteralPath {ps_quote(folder)} -Filter *.lnk | ForEach-Object {{\n"
        "  $s = $sh.CreateShortcut($_.FullName)\n"
        "  [pscustomobject]@{ path = $_.FullName; command = ($s.TargetPath + ' ' + $s.Arguments) }\n"
        "} | ConvertTo-Json -Compress"
    )
    try:
        result = run_powershell(script, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    import json

    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    if isinstance(data, dict):  # a single shortcut isn't wrapped in a list
        data = [data]
    return [(item.get("path", ""), item.get("command", "")) for item in data]


def find_autostart_entries():
    """Every ClipEm boot/login autostart entry for this user, from this or
    any other ClipEm folder (and from older versions' launchers), as dicts:
    {"path", "command", "this_install"}. Only looks in the OS's own per-user
    autostart location."""
    folder = _autostart_dir()
    if not folder or not os.path.isdir(folder):
        return []
    system = platform.system()
    if system == "Windows":
        candidates = _windows_startup_shortcuts(folder)
    else:
        suffix = ".plist" if system == "Darwin" else ".desktop"
        candidates = []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not name.endswith(suffix) or not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    candidates.append((path, f.read()))
            except OSError:
                continue
    return [
        {"path": path, "command": command, "this_install": _points_at_this_install(command)}
        for path, command in candidates
        if _points_at_clipem(os.path.basename(path), command)
    ]


def find_other_autostart_entries():
    """ClipEm autostart entries that don't start this install - left behind
    by an older version, or by a copy of ClipEm in another folder."""
    return [entry for entry in find_autostart_entries() if not entry["this_install"]]


def describe_autostart_entry(entry):
    """e.g. "clipem.desktop (starts /home/pt/ClipEm-old)" - which ClipEm
    folder an entry starts, as far as its command shows."""
    name = os.path.basename(entry["path"])
    launchers = "|".join(re.escape(n) for n in LEGACY_LAUNCHER_NAMES)
    # An absolute path ("/..." or "C:\\..."), up to the folder holding
    # App/Shared/Installer/run.py or an old launcher.
    match = re.search(
        rf"((?:[A-Za-z]:)?[\\/][^\"'\n<>=]*?)[\\/](?:App[\\/]Shared[\\/]Installer[\\/]run\.py|{launchers})",
        entry["command"],
    )
    if match and match.group(1).strip():
        return f"{name} (starts {match.group(1).strip()})"
    return name


def remove_autostart_entries(entries):
    """Deletes the given entries (from find_autostart_entries). Returns how
    many were removed."""
    removed = 0
    for entry in entries:
        try:
            os.remove(entry["path"])
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"Could not remove the autostart entry {entry['path']}: {exc}", file=sys.stderr)
            continue
        removed += 1
        print(f"Removed the autostart entry {entry['path']}")
    return removed


def macos_launch_agent_plist(python_exe):
    args = "".join(
        f"    <string>{xml_escape(arg)}</string>\n" for arg in (python_exe, RUN_PY, "--boot")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{MACOS_LAUNCH_AGENT_LABEL}</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        f"{args}"
        "  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "</dict></plist>\n"
    )


def linux_desktop_entry(python_exe, extra_args=(), icon=None, autostart=False, menu=False):
    exec_line = " ".join(desktop_exec_arg(a) for a in (python_exe, RUN_PY, *extra_args))
    lines = ["[Desktop Entry]", "Type=Application", "Name=ClipEm", f"Exec={exec_line}"]
    if icon:
        lines.append(f"Icon={icon}")
    if autostart:
        lines.append("X-GNOME-Autostart-enabled=true")
    if menu:
        # StartupWMClass ties ClipEm's Tk windows (className="ClipEm") to
        # this entry, so taskbars show its name and icon for them.
        lines += [
            "Comment=Clip your gameplay with OBS's replay buffer",
            "Categories=AudioVideo;Video;Recorder;",
            "StartupWMClass=ClipEm",
        ]
    lines.append("Terminal=false")
    return "\n".join(lines) + "\n"


def apply_boot_autostart(enabled):
    """Registers (or removes) a real OS-level boot/login autostart entry
    that runs `run.py --boot`. "Start when the PC boots" is only possible if
    the OS itself knows to launch something at login, and there's no
    portable equivalent. Idempotent either way, and entirely reversible by
    disabling the option again (or deleting autostart_entry_path())."""
    path = autostart_entry_path()
    if not path:
        print(f"Boot autostart isn't supported on {platform.system()}.", file=sys.stderr)
        return

    if not enabled:
        # Only this install's own entry - one from another ClipEm folder at
        # the same path belongs to that install (find_other_autostart_entries
        # / remove_autostart_entries clear those when the user asks).
        own = [e for e in find_autostart_entries() if e["this_install"] and e["path"] == path]
        remove_autostart_entries(own)
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    system = platform.system()
    try:
        if system == "Windows":
            python_root = find_windows_python_root()
            pythonw = os.path.join(python_root, "pythonw.exe") if python_root else sys.executable
            script = windows_shortcut_script(
                path, pythonw, f"{windows_command_line_arg(RUN_PY)} --boot", ROOT_DIR,
            )
            run_powershell(script, check=True)
        elif system == "Darwin":
            with open(path, "w", encoding="utf-8") as f:
                f.write(macos_launch_agent_plist(sys.executable))
        elif system == "Linux":
            with open(path, "w", encoding="utf-8") as f:
                f.write(linux_desktop_entry(find_linux_python_bin(), ["--boot"], autostart=True))
            os.chmod(path, 0o755)
    except Exception as exc:
        print(f"Could not create the boot autostart entry ({exc}) - skipping it.", file=sys.stderr)
        return

    print(f"Boot autostart enabled: {path}")


# ---------------------------------------------------------------------------
# Desktop shortcut
# ---------------------------------------------------------------------------

def start_menu_shortcut_path():
    """The Start Menu entry that makes ClipEm show up in Windows Search, and
    names its notifications (see WINDOWS_APP_ID). None if APPDATA is unset."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "ClipEm.lnk")


def set_shortcut_app_id(shortcut_path, app_id):
    """Stamps a .lnk with System.AppUserModel.ID. A process using the same
    explicit AppUserModelID then gets the shortcut's name and icon on its
    notifications instead of its exe's ("Python"). WScript.Shell can't set
    this property, so it goes through the shell's IPropertyStore directly.
    Raises OSError on failure."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    def guid(text):
        g = GUID()
        ctypes.memmove(g.bytes, uuid.UUID(text).bytes_le, 16)
        return g

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                    ("pwszVal", ctypes.c_wchar_p), ("padding", ctypes.c_void_p)]

    GPS_READWRITE, VT_LPWSTR = 2, 31
    iid_property_store = guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
    pkey_app_user_model_id = PROPERTYKEY(guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)

    ctypes.windll.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED; already-initialized is fine
    store = ctypes.c_void_p()
    ctypes.oledll.shell32.SHGetPropertyStoreFromParsingName(
        ctypes.c_wchar_p(shortcut_path), None, GPS_READWRITE,
        ctypes.byref(iid_property_store), ctypes.byref(store),
    )
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

    def method(index, restype, *argtypes):
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])

    # IPropertyStore vtable: IUnknown (0-2), GetCount, GetAt, GetValue, SetValue (6), Commit (7).
    try:
        value = PROPVARIANT(vt=VT_LPWSTR, pwszVal=app_id)
        method(6, ctypes.HRESULT, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
            store, ctypes.byref(pkey_app_user_model_id), ctypes.byref(value),
        )
        method(7, ctypes.HRESULT)(store)
    finally:
        method(2, ctypes.c_ulong)(store)


def create_shortcut_windows():
    # pythonw.exe, not python.exe: run.py only launches OBS and the tray
    # widget, and python.exe left a console window open that killed the tray
    # widget when closed. Always (re)written so shortcuts from older setups
    # that pointed at python.exe get fixed by re-running Setup.
    shortcut_path = os.path.join(ROOT_DIR, "ClipEm.lnk")
    pythonw, arguments, icon = _windows_launch_target()
    script = windows_shortcut_script(shortcut_path, pythonw, arguments, ROOT_DIR, icon)
    run_powershell(script, check=True)
    print(f"Created shortcut: {shortcut_path}")

    # A shortcut in the ClipEm folder isn't indexed by Windows Search - one
    # in the Start Menu is. Best effort: the folder's own shortcut is enough
    # to run ClipEm.
    try:
        path = create_start_menu_shortcut()
        if path:
            print(f"Added ClipEm to the Start Menu: {path}")
    except Exception as exc:
        print(f"Could not add ClipEm to the Start Menu ({exc}) - skipping it.", file=sys.stderr)


def _windows_launch_target():
    """(pythonw.exe, its arguments, icon) every ClipEm shortcut runs."""
    pythonw = os.path.join(find_windows_python_root() or os.path.join(APP_DIR, "Windows", "Python"), "pythonw.exe")
    return pythonw, windows_command_line_arg(RUN_PY), os.path.join(SHARED_DIR, "tray-widget", "ClipEm.ico")


def create_start_menu_shortcut():
    """Writes the Start Menu entry (with WINDOWS_APP_ID, so notifications
    say "ClipEm") and records it in run_config.json. Returns its path, or
    None without APPDATA. Raises on failure."""
    path = start_menu_shortcut_path()
    if not path:
        return None
    pythonw, arguments, icon = _windows_launch_target()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    run_powershell(windows_shortcut_script(path, pythonw, arguments, ROOT_DIR, icon), check=True,
                   capture_output=True)
    set_shortcut_app_id(path, WINDOWS_APP_ID)
    config = load_run_config()
    config["start_menu_registered"] = START_MENU_REGISTRATION
    config.pop("start_menu_failures", None)
    save_run_config(config)
    return path


def ensure_start_menu_entry():
    """Only Setup adds the Start Menu entry, so an install that updated from
    a version before it - or before it carried WINDOWS_APP_ID - never got
    one: no Windows Search entry, notifications from "Python". run.py calls
    this on each launch; it does the work once per START_MENU_REGISTRATION.
    Returns the shortcut's path when it wrote one, else None."""
    if platform.system() != "Windows":
        return None
    config = load_run_config()
    try:
        done = int(config.get("start_menu_registered") or 0)
        failed = int(config.get("start_menu_failures") or 0)
    except (TypeError, ValueError):
        done = failed = 0
    if done >= START_MENU_REGISTRATION or failed >= START_MENU_MAX_ATTEMPTS:
        return None
    try:
        return create_start_menu_shortcut()
    except Exception:
        config = load_run_config()
        config["start_menu_failures"] = failed + 1
        save_run_config(config)
        raise


def remove_start_menu_shortcut():
    """Removes the Start Menu (Windows) / application menu (Linux) entry if
    it starts this install (one from another ClipEm folder is left alone)."""
    if platform.system() == "Linux":
        remove_linux_menu_entry()
        return
    path = start_menu_shortcut_path() if platform.system() == "Windows" else None
    if not path or not os.path.exists(path):
        return
    for shortcut, command in _windows_startup_shortcuts(os.path.dirname(path)):
        if os.path.normcase(shortcut) == os.path.normcase(path) and _points_at_this_install(command):
            try:
                os.remove(path)
                print(f"Removed the Start Menu entry {path}")
            except OSError as exc:
                print(f"Could not remove the Start Menu entry {path}: {exc}", file=sys.stderr)
            return
    print(f"Left in place: {path} starts ClipEm from somewhere else.")


def create_shortcut_macos():
    alias_path = os.path.join(ROOT_DIR, "ClipEm")
    if os.path.exists(alias_path):
        return

    # Finder aliases can't carry arguments, so point one at a tiny launcher -
    # pinned to this interpreter rather than whatever "python3" is on PATH.
    launcher_path = os.path.join(INSTALLER_DIR, "launch_obs.command")
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/bash\nexec {shlex.quote(sys.executable)} "$(dirname "$0")/run.py"\n')
    os.chmod(launcher_path, 0o755)

    script = (
        'tell application "Finder"\n'
        f"set theAlias to make alias file to POSIX file {applescript_quote(launcher_path)} "
        f"at POSIX file {applescript_quote(ROOT_DIR)}\n"
        'set name of theAlias to "ClipEm"\n'
        "end tell"
    )
    subprocess.run(["osascript", "-e", script], check=True)
    print(f"Created alias: {alias_path}")


def linux_menu_entry_path():
    """The per-user application-menu entry (KDE's launcher, GNOME's app
    grid, etc. all read $XDG_DATA_HOME/applications)."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data_home, "applications", "clipem.desktop")


def _refresh_linux_menus(applications_dir):
    """Best effort: most menus notice a new entry by themselves, these just
    make it show up straight away."""
    for command in (["update-desktop-database", applications_dir], ["kbuildsycoca6"], ["kbuildsycoca5"]):
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.run(command, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        if command[0].startswith("kbuildsycoca"):
            break


def create_linux_menu_entry(python_exe, icon):
    """Always rewritten, so the entry follows this install if it moves (or
    takes over from another ClipEm folder's - the menu only has room for one)."""
    path = linux_menu_entry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(linux_desktop_entry(python_exe, icon=icon, menu=True))
    os.chmod(path, 0o755)
    _refresh_linux_menus(os.path.dirname(path))
    print(f"Added ClipEm to the application menu: {path}")


def remove_linux_menu_entry():
    path = linux_menu_entry_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return
    if not _points_at_this_install(content):
        print(f"Left in place: {path} starts ClipEm from somewhere else.")
        return
    try:
        os.remove(path)
        print(f"Removed the application menu entry {path}")
    except OSError as exc:
        print(f"Could not remove the application menu entry {path}: {exc}", file=sys.stderr)
        return
    _refresh_linux_menus(os.path.dirname(path))


def create_shortcut_linux():
    icon = os.path.join(SHARED_DIR, "tray-widget", "ClipEm.png")
    icon = icon if os.path.exists(icon) else None
    python_exe = find_linux_python_bin()

    desktop_path = os.path.join(ROOT_DIR, "ClipEm.desktop")
    if not os.path.exists(desktop_path):
        with open(desktop_path, "w", encoding="utf-8") as f:
            f.write(linux_desktop_entry(python_exe, icon=icon))
        os.chmod(desktop_path, 0o755)
        print(
            f"Created launcher: {desktop_path} "
            "(GNOME/KDE may ask you to right-click -> Allow Launching, once)"
        )

    # A launcher in the ClipEm folder isn't in the application menu - one in
    # ~/.local/share/applications is. Best effort: the folder's own launcher
    # is enough to run ClipEm.
    try:
        create_linux_menu_entry(python_exe, icon)
    except Exception as exc:
        print(f"Could not add ClipEm to the application menu ({exc}) - skipping it.", file=sys.stderr)


def create_shortcut():
    system = platform.system()
    if system == "Windows":
        create_shortcut_windows()
    elif system == "Darwin":
        create_shortcut_macos()
    elif system == "Linux":
        create_shortcut_linux()
