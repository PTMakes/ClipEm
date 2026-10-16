"""
"Show OBS" for the tray widget: bring this install's OBS window to the
front. Everything is matched by process ID (obs_process.find_obs_pids - the
OBS running from this ClipEm folder), never by window title: a title match
on "OBS" also finds e.g. a browser tab about OBS.

Linux: OBS runs with --minimize-to-tray, which under Wayland means its main
window doesn't exist at all until OBS shows it. The only way to make OBS
show it is its own tray icon: its StatusNotifierItem "Activate" D-Bus
method is exactly what a click on that icon sends - but OBS treats that
click as show/hide *toggle* (OBSBasic::IconActivated -> ToggleShowHide), so
it must only be sent when the window is hidden, or it hides a visible OBS.
On KDE one KWin script decides: it raises OBS's window if there is one,
otherwise clicks the tray icon and raises the window once it appears
(KWin's focus-stealing prevention would otherwise leave it behind whatever
has focus). Elsewhere: an existing X11 window via wmctrl/xdotool, then the
tray icon.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading

import obs_process

SNI_WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
# The tray widget's own D-Bus object that the KWin watch script reports to
# (exported by sni_tray.py).
CLIPEM_PATH = "/ClipEm"
CLIPEM_INTERFACE = "app.clipem.Tray"
# Qt names each tray icon's D-Bus service after its process.
_SNI_SERVICE_PID = re.compile(r"StatusNotifierItem-(\d+)-\d+$")


def _run(cmd, timeout=5):
    """(returncode, stdout) - (None, "") if the tool isn't there or hangs."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return result.returncode, result.stdout


# ---------------------------------------------------------------------------
# Linux: OBS's tray icon (StatusNotifierItem)
# ---------------------------------------------------------------------------

def sni_items():
    """[(service, path)] for every tray icon the StatusNotifierWatcher knows."""
    code, out = _run([
        "busctl", "--user", "--json=short", "call", *SNI_WATCHER,
        "org.freedesktop.DBus.Properties", "Get", "ss",
        "org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems",
    ])
    if code != 0:
        return []
    try:
        items = json.loads(out)["data"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError):
        return []
    result = []
    for item in items:
        service, _, path = item.partition("/")
        result.append((service, "/" + path if path else "/StatusNotifierItem"))
    return result


def dbus_service_pid(service):
    """PID of the process that owns a D-Bus service name, or None."""
    match = _SNI_SERVICE_PID.search(service)
    if match:
        return int(match.group(1))
    code, out = _run([
        "busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "GetConnectionUnixProcessID", "s", service,
    ])
    parts = out.split()
    if code == 0 and len(parts) == 2 and parts[1].isdigit():  # "u 12345"
        return int(parts[1])
    return None


def obs_tray_icon(pids):
    """(service, path) of the tray icon owned by one of pids, or None."""
    for service, path in sni_items():
        if dbus_service_pid(service) in pids:
            return service, path
    return None


def activate_tray_icon(icon):
    """Clicks (D-Bus Activate) a tray icon. Toggles OBS - see the module
    docstring. True if the call succeeded."""
    service, path = icon
    code, _ = _run(["busctl", "--user", "call", service, path,
                    "org.kde.StatusNotifierItem", "Activate", "ii", "0", "0"])
    return code == 0


# ---------------------------------------------------------------------------
# Linux: KDE (KWin scripting, X11 or Wayland)
# ---------------------------------------------------------------------------

# How long the KWin script stays loaded after a tray-icon click, waiting for
# OBS's window to appear so it can raise it.
KWIN_SCRIPT_LINGER_SECONDS = 5


def kwin_show_script(pids, icon):
    """KWin script (JS): raise the first normal window of one of pids; if
    there's none (hidden to the tray), click `icon` and raise the window as
    soon as OBS shows it."""
    service, path = icon if icon else ("", "")
    return (
        f"var pids = {json.dumps(sorted(pids))};"
        f"var sniService = {json.dumps(service)}, sniPath = {json.dumps(path)};"
        "function isObs(w) { return pids.indexOf(w.pid) !== -1 && w.normalWindow; }"
        "function raise(w) { w.minimized = false; workspace.activeWindow = w; }"
        "var found = null, wins = workspace.windowList();"
        "for (var i = 0; i < wins.length; i++) { if (isObs(wins[i])) { found = wins[i]; break; } }"
        "if (found) { raise(found); }"
        "else if (sniService) {"
        "  workspace.windowAdded.connect(function (w) { if (isObs(w)) raise(w); });"
        "  callDBus(sniService, sniPath, 'org.kde.StatusNotifierItem', 'Activate', 0, 0);"
        "}"
    )


def show_obs_kwin(pids, icon):
    """Runs kwin_show_script. True if KWin ran it - KWin gives no way to learn
    what it found. The script is unloaded after KWIN_SCRIPT_LINGER_SECONDS."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(kwin_show_script(pids, icon))
        script_path = f.name

    def unload():
        _run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
              "org.kde.kwin.Scripting", "unloadScript", "s", script_path])
        try:
            os.remove(script_path)
        except OSError:
            pass

    code, out = _run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
                      "org.kde.kwin.Scripting", "loadScript", "s", script_path])
    parts = out.split()  # "i <id>"
    if code != 0 or len(parts) != 2 or parts[0] != "i":
        unload()
        return False
    code, _ = _run(["busctl", "--user", "call", "org.kde.KWin", f"/Scripting/Script{parts[1]}",
                    "org.kde.kwin.Script", "run"])
    timer = threading.Timer(KWIN_SCRIPT_LINGER_SECONDS, unload)
    timer.daemon = True
    timer.start()
    return code == 0


# ---------------------------------------------------------------------------
# Linux elsewhere: an existing X11 window of one of pids
# ---------------------------------------------------------------------------

def activate_window_x11(pids):
    """X11 only: wmctrl (window list with PIDs - mapped windows only) or
    xdotool (search by PID, visible windows only)."""
    code, out = _run(["wmctrl", "-lp"])
    if code == 0:
        for line in out.splitlines():
            fields = line.split(None, 4)  # id, desktop, pid, host, title
            if len(fields) >= 3 and fields[2].isdigit() and int(fields[2]) in pids:
                return _run(["wmctrl", "-i", "-a", fields[0]])[0] == 0
    for pid in pids:
        code, out = _run(["xdotool", "search", "--onlyvisible", "--pid", str(pid)])
        window_ids = out.split()
        if code == 0 and window_ids:
            return _run(["xdotool", "windowactivate", window_ids[0]])[0] == 0
    return False


# ---------------------------------------------------------------------------

def show_obs(log):
    """Brings this install's OBS to the front. Returns False (after logging
    why) if OBS isn't running or nothing could show it. Never raises."""
    try:
        pids = set(obs_process.find_obs_pids())
        if not pids:
            log.info("Show OBS: OBS isn't running.")
            return False
        if sys.platform == "win32":
            return _show_windows(pids, log)
        if sys.platform == "darwin":
            code, _ = _run(["osascript", "-e", 'tell application "OBS" to activate'])
            return code == 0
        icon = obs_tray_icon(pids)
        if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "") and show_obs_kwin(pids, icon):
            log.info(f"Show OBS: ran the KWin script (OBS tray icon: {icon}).")
            return True
        if activate_window_x11(pids):
            log.info("Show OBS: activated OBS's X11 window.")
            return True
        if icon and activate_tray_icon(icon):
            log.info("Show OBS: clicked OBS's tray icon.")
            return True
        log.warning(f"Show OBS: couldn't find a tray icon or window for OBS (pid {sorted(pids)}).")
        return False
    except Exception as exc:
        log.warning(f"Show OBS failed: {exc}")
        return False


# Windows: OBS hides itself to the tray with Qt's hide(). ShowWindow() on
# that window bypasses Qt - Qt still thinks it's hidden and never paints it
# (a blank white OBS), and the next minimize/hide goes through Qt's stale
# state. So, as on Linux, a hidden OBS is shown by "clicking" its own tray
# icon: Qt's QSystemTrayIcon gets clicks as a message to a hidden helper
# window titled "QTrayIconMessageWindow" (qwindowssystemtrayicon.cpp,
# MYWM_NOTIFYICON = WM_APP + 101; NIN_SELECT is a left click). OBS toggles on
# it, so it's only sent when that toggle goes the right way.
QT_TRAY_WINDOW_TITLE = "QTrayIconMessageWindow"
QT_MYWM_NOTIFYICON = 0x8000 + 101
NIN_SELECT = 0x0400


def _find_qt_tray_window_win32(pids):
    """(hwnd, pid) of the tray icon helper window of one of pids, or None."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        buf = ctypes.create_unicode_buffer(len(QT_TRAY_WINDOW_TITLE) + 2)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        if buf.value == QT_TRAY_WINDOW_TITLE:
            found.append((hwnd, pid.value))
            return False
        return True

    user32.EnumWindows(_enum_proc, 0)
    return found[0] if found else None


def _click_tray_icon_win32(pids, log):
    """Toggles OBS's window the way clicking its tray icon does. True if the
    click was sent."""
    import ctypes

    tray = _find_qt_tray_window_win32(pids)
    if not tray:
        return False
    hwnd, pid = tray
    user32 = ctypes.windll.user32
    # OBS raises its window itself when shown; let it take the foreground,
    # which Windows only allows a process the user just interacted with.
    user32.AllowSetForegroundWindow(pid)
    user32.PostMessageW(hwnd, QT_MYWM_NOTIFYICON, 0, NIN_SELECT)
    log.info("Clicked OBS's tray icon.")
    return True


def _show_windows(pids, log):
    import ctypes

    match = obs_process._find_main_window_win32(pids)
    if not match:
        log.warning("Show OBS: no OBS main window from this install found.")
        return False
    hwnd = match[0]
    user32 = ctypes.windll.user32
    SW_SHOW, SW_RESTORE = 5, 9
    if not user32.IsWindowVisible(hwnd):
        # Hidden to the tray (--minimize-to-tray): OBS has to show it itself.
        if _click_tray_icon_win32(pids, log):
            return True
        log.warning("Show OBS: OBS's tray icon not found; showing its window directly.")
        user32.ShowWindow(hwnd, SW_SHOW)
    elif user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


# ---------------------------------------------------------------------------
# Hide OBS, and knowing which of Show/Hide applies
# ---------------------------------------------------------------------------

def hide_obs(log):
    """Sends this install's OBS back to its tray icon (or minimizes it).
    Returns False (after logging why) if that isn't possible. Never raises."""
    try:
        pids = set(obs_process.find_obs_pids())
        if not pids:
            return False
        if sys.platform == "win32":
            return _hide_windows(pids, log)
        if sys.platform == "darwin":
            code, _ = _run(["osascript", "-e",
                            'tell application "System Events" to set visible of process "OBS" to false'])
            return code == 0
        # OBS's own tray icon toggles its window - while it's shown, a click
        # hides it to the tray exactly as clicking the icon yourself would.
        icon = obs_tray_icon(pids)
        if icon and activate_tray_icon(icon):
            log.info("Hide OBS: clicked OBS's tray icon.")
            return True
        for pid in pids:  # no tray icon: minimize its X11 window instead
            code, out = _run(["xdotool", "search", "--onlyvisible", "--pid", str(pid)])
            if code == 0 and out.split():
                return _run(["xdotool", "windowminimize", out.split()[0]])[0] == 0
        log.warning("Hide OBS: couldn't find a tray icon or window for OBS.")
        return False
    except Exception as exc:
        log.warning(f"Hide OBS failed: {exc}")
        return False


def _hide_windows(pids, log):
    import ctypes

    match = obs_process._find_main_window_win32(pids)
    if not match:
        return False
    user32 = ctypes.windll.user32
    if not user32.IsWindowVisible(match[0]) or user32.IsIconic(match[0]):
        return True  # already out of the way - a tray click would show it
    # Back to the tray, as clicking OBS's own icon does (see _show_windows).
    if _click_tray_icon_win32(pids, log):
        return True
    WM_SYSCOMMAND, SC_MINIMIZE = 0x0112, 0xF020
    # No tray icon: a normal minimize, so OBS's own "minimize to tray" setting applies.
    user32.PostMessageW(match[0], WM_SYSCOMMAND, SC_MINIMIZE, 0)
    return True


def _windows_visible(pids):
    import ctypes

    match = obs_process._find_main_window_win32(pids)
    if not match:
        return False
    user32 = ctypes.windll.user32
    return bool(user32.IsWindowVisible(match[0])) and not user32.IsIconic(match[0])


def _x11_visible(pids):
    """None if wmctrl isn't there to ask."""
    code, out = _run(["wmctrl", "-lp"])
    if code != 0:
        return None
    for line in out.splitlines():
        fields = line.split(None, 4)
        if len(fields) >= 3 and fields[2].isdigit() and int(fields[2]) in pids:
            return True
    return False


KWIN_WATCH_SCRIPT_NAME = "clipem-obs-window-watch"


def kwin_watch_script(pids, tray_bus_name):
    """KWin script (JS) that stays loaded and tells the tray widget (via
    sni_tray's app.clipem.Tray ObsWindowVisible) whenever OBS's window
    appears, disappears (hidden to the tray), or is (un)minimized."""
    return (
        f"var pids = {json.dumps(sorted(pids))};"
        f"var tray = {json.dumps(tray_bus_name)};"
        "function isObs(w) { return pids.indexOf(w.pid) !== -1 && w.normalWindow; }"
        "function report(gone) {"
        "  var visible = false, wins = workspace.windowList();"
        "  for (var i = 0; i < wins.length; i++) {"
        "    if (wins[i] !== gone && isObs(wins[i]) && !wins[i].minimized) { visible = true; }"
        "  }"
        f"  callDBus(tray, {json.dumps(CLIPEM_PATH)}, {json.dumps(CLIPEM_INTERFACE)}, 'ObsWindowVisible', visible);"
        "}"
        "function watch(w) { if (isObs(w)) { w.minimizedChanged.connect(function () { report(null); }); } }"
        "var existing = workspace.windowList();"
        "for (var i = 0; i < existing.length; i++) { watch(existing[i]); }"
        "workspace.windowAdded.connect(function (w) { watch(w); report(null); });"
        "workspace.windowRemoved.connect(function (w) { report(w); });"
        "report(null);"
    )


class ObsWindowState:
    """Whether this install's OBS window is showing - for the tray's
    "Show OBS"/"Hide OBS" item. Asked where the platform can say (Windows;
    X11 via wmctrl; KDE via a KWin script reporting changes); elsewhere
    (e.g. GNOME on Wayland) it remembers what the tray itself last did."""

    def __init__(self, log):
        self.log = log
        self.visible = False
        self._watch_pids = None
        self._watch_script = None

    def refresh(self, pids, tray_bus_name=None):
        """Called from the tray's poll loop. Returns True if `visible` changed."""
        before = self.visible
        pids = set(pids)
        if not pids:
            self.visible = False
            self.stop()
        elif sys.platform == "win32":
            self.visible = _windows_visible(pids)
        elif sys.platform.startswith("linux"):
            if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "") and tray_bus_name:
                if pids != self._watch_pids:  # OBS (re)started: watch its new PIDs
                    self._start_kwin_watch(pids, tray_bus_name)
            elif os.environ.get("XDG_SESSION_TYPE") == "x11" or not os.environ.get("WAYLAND_DISPLAY"):
                x11 = _x11_visible(pids)
                if x11 is not None:
                    self.visible = x11
        return self.visible != before

    def reported(self, visible):
        """From the KWin script."""
        self.visible = visible

    def remember(self, visible):
        """After the tray itself showed or hid OBS - the only signal where
        nothing can be asked."""
        self.visible = visible

    def _start_kwin_watch(self, pids, tray_bus_name):
        self.stop()
        # Also clears one left behind by a tray widget that crashed - KWin
        # refuses to load a second script under the same name.
        _run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
              "unloadScript", "s", KWIN_WATCH_SCRIPT_NAME])
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(kwin_watch_script(pids, tray_bus_name))
            self._watch_script = f.name
        code, out = _run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                          "loadScript", "ss", self._watch_script, KWIN_WATCH_SCRIPT_NAME])
        parts = out.split()
        if code == 0 and len(parts) == 2 and parts[0] == "i" and parts[1] != "-1":
            _run(["busctl", "--user", "call", "org.kde.KWin", f"/Scripting/Script{parts[1]}",
                  "org.kde.kwin.Script", "run"])
            self._watch_pids = pids
        else:
            self.log.warning("Couldn't load the KWin script that tracks OBS's window.")
            self._watch_pids = pids  # don't retry every poll

    def stop(self):
        """Unloads the KWin script (on tray exit, or when OBS closes)."""
        if self._watch_pids is None and self._watch_script is None:
            return
        _run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
              "unloadScript", "s", KWIN_WATCH_SCRIPT_NAME])
        if self._watch_script:
            try:
                os.remove(self._watch_script)
            except OSError:
                pass
        self._watch_pids = self._watch_script = None
