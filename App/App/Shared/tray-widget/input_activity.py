"""How long ago the PC was last used, for telling a frozen screen capture from
a screen with nothing happening on it (stuck_screen.py).

Windows has GetLastInputInfo. Wayland keeps input from apps, and KDE doesn't
offer GetSessionIdleTime there, but KWin scripts can watch the mouse: one
tells the tray the name of the screen the mouse moved on (sni_tray's
app.clipem.Tray MouseMoved), then stops listening for REPORT_SECONDS. Only
that counts as using the PC: a mouse on another monitor says nothing about the
captured one, whose name comes from OBS's PipeWire stream ("kwin-screencast-
DP-1"). Elsewhere, or capturing a window, it's unknown.

Where the mouse is goes no further than KWin working out which screen that
is: the position isn't kept, sent or logged, and only the time of the last
move on each screen is, in memory.
"""

import json
import math
import os
import re
import subprocess
import sys
import time

import obs_window

KWIN_SCRIPT_NAME = "clipem-mouse-watch"
REPORT_SECONDS = 10
STREAM_PREFIX = "kwin-screencast-"
# A connector ("DP-1", "HDMI-A-1"); a window's stream is named after its app.
SCREEN_NAME = re.compile(r"^[A-Za-z]+(-[A-Za-z0-9]+)+$")
# How long the captured screen is remembered: it only changes in the picker.
CAPTURED_SCREEN_CACHE_SECONDS = 60


def kwin_mouse_script(tray_bus_name):
    return (
        f"var tray = {json.dumps(tray_bus_name)};"
        "var pause = new QTimer();"
        f"pause.interval = {REPORT_SECONDS * 1000};"
        "pause.singleShot = true;"
        "function moved() {"
        "  workspace.cursorPosChanged.disconnect(moved);"
        "  pause.start();"
        "  var on = workspace.screenAt(workspace.cursorPos);"
        "  if (on) {"
        f"    callDBus(tray, {json.dumps(obs_window.CLIPEM_PATH)}, {json.dumps(obs_window.CLIPEM_INTERFACE)},"
        "             'MouseMoved', on.name);"
        "  }"
        "}"
        "pause.timeout.connect(function () { workspace.cursorPosChanged.connect(moved); });"
        "workspace.cursorPosChanged.connect(moved);"
    )


def captured_screen(run=subprocess.run):
    """The screen OBS's PipeWire capture is showing, by connector name, from
    PipeWire's list of streams. None if it can't be told or it's a window."""
    try:
        dump = json.loads(run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    props = {item.get("id"): (item.get("info") or {}).get("props") or {} for item in dump if isinstance(item, dict)}
    screens = set()
    for node in props.values():
        if node.get("node.name") != "obs" or node.get("media.role") != "Screen":
            continue
        source = props.get(node.get("node.driver-id"), {})
        name = str(source.get("media.name", ""))
        if name.startswith(STREAM_PREFIX):
            screens.add(name[len(STREAM_PREFIX):])
    if len(screens) != 1:  # none, or two copies of OBS on different screens
        return None
    screen = screens.pop()
    return screen if SCREEN_NAME.match(screen) else None


def _windows_seconds_since_input():
    import ctypes

    class LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]

    info = LastInputInfo(cbSize=ctypes.sizeof(LastInputInfo))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000  # both wrap after 49 days


class InputActivity:
    """seconds_since_input() is what stuck_screen asks. On KDE, start() runs
    the KWin script (the tray's poll loop calls it, once its D-Bus name is
    known) and mouse_moved() is told about the mouse; stop() ends it."""

    def __init__(self, log, clock=time.monotonic, find_screen=captured_screen):
        self._log = log
        self._clock = clock
        self._find_screen = find_screen
        self._script = None
        self._started = False
        self._moves = {}  # screen name -> clock() of the mouse's last move on it
        self._screen = None  # (screen name or None, clock() when it was looked up)

    def start(self, tray_bus_name):
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        if self._started or not tray_bus_name or "KDE" not in desktop or not sys.platform.startswith("linux"):
            return
        self._started = True
        self._script = obs_window.load_kwin_script(kwin_mouse_script(tray_bus_name), KWIN_SCRIPT_NAME)
        if self._script is None:
            self._log.warning("Couldn't load the KWin script that watches the mouse.")

    def stop(self):
        if self._script is not None:
            obs_window.unload_kwin_script(KWIN_SCRIPT_NAME, self._script)
            self._script = None

    def mouse_moved(self, screen):
        self._moves[screen] = self._clock()

    def _captured_screen(self):
        now = self._clock()
        if self._screen is None or now - self._screen[1] >= CAPTURED_SCREEN_CACHE_SECONDS:
            self._screen = (self._find_screen(), now)
        return self._screen[0]

    def seconds_since_input(self):
        """Seconds since the PC was last used (on KDE: the mouse, on the
        captured screen); None if there's no way to know."""
        if sys.platform == "win32":
            return _windows_seconds_since_input()
        if self._script is None:
            return None
        screen = self._captured_screen()
        if screen is None:
            return None
        return self._clock() - self._moves[screen] if screen in self._moves else math.inf
