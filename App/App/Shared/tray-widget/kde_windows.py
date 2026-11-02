"""Which windows are on the screen OBS is capturing, on KDE - for naming clips
after what's on it (app_audio.ActiveApp).

Wayland keeps windows from apps, so a KWin script tells the tray about the
focused window and any fullscreen ones (sni_tray's app.clipem.Tray
WindowsChanged) when that changes. The captured screen is the one
input_activity.py finds from OBS's PipeWire stream; if it can't be told, or
OBS is capturing a window, every screen counts.

Only the newest report is kept, in memory. A window's title goes no further,
unless it's what a clip ends up named after (window_label).
"""

import json
import os
import sys
import time

import input_activity
import obs_window

KWIN_SCRIPT_NAME = "clipem-window-watch"
CHECK_SECONDS = 2  # what no signal says: a window going fullscreen or moving
CAPTURED_SCREEN_CACHE_SECONDS = 60


def kwin_windows_script(tray_bus_name):
    """Reports the focused and fullscreen windows of the current desktop,
    bottom to top, as JSON: [{"pid", "app", "title", "screen", "fullscreen",
    "active"}]."""
    return (
        f"var tray = {json.dumps(tray_bus_name)};"
        "var last = null;"
        "function shown(w) {"
        "  return w.normalWindow && !w.minimized"
        "    && (w.onAllDesktops || w.desktops.indexOf(workspace.currentDesktop) !== -1);"
        "}"
        "function report() {"
        "  var found = [], active = workspace.activeWindow, stack = workspace.stackingOrder;"
        "  for (var i = 0; i < stack.length; i++) {"
        "    var w = stack[i];"
        "    if (shown(w) && (w.fullScreen || w === active)) {"
        "      found.push({pid: w.pid, app: String(w.resourceClass), title: String(w.caption),"
        "                  screen: w.output ? w.output.name : '', fullscreen: w.fullScreen, active: w === active});"
        "    }"
        "  }"
        "  var text = JSON.stringify(found);"
        "  if (text !== last) {"
        "    last = text;"
        f"    callDBus(tray, {json.dumps(obs_window.CLIPEM_PATH)}, {json.dumps(obs_window.CLIPEM_INTERFACE)},"
        "             'WindowsChanged', text);"
        "  }"
        "}"
        "var check = new QTimer();"
        f"check.interval = {CHECK_SECONDS * 1000};"
        "check.timeout.connect(report);"
        "check.start();"
        "workspace.windowActivated.connect(report);"
        "report();"
    )


def parse_windows(text):
    """The script's report, keeping only well-formed windows."""
    try:
        windows = json.loads(text)
    except ValueError:
        return []
    return [w for w in windows if isinstance(w, dict) and isinstance(w.get("app"), str) and w["app"]] \
        if isinstance(windows, list) else []


class KdeWindows:
    """on_captured_screen() is what clip names ask. start() runs the KWin
    script (the tray's poll loop calls it, once its D-Bus name is known),
    windows_changed() is told what it sees; stop() ends it."""

    def __init__(self, log, clock=time.monotonic, find_screen=input_activity.captured_screen):
        self._log = log
        self._clock = clock
        self._find_screen = find_screen
        self._script = None
        self._started = False
        self._windows = []
        self._screen = None  # (screen name or None, clock() when it was looked up)

    def start(self, tray_bus_name):
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        if self._started or not tray_bus_name or "KDE" not in desktop or not sys.platform.startswith("linux"):
            return
        self._started = True
        self._script = obs_window.load_kwin_script(kwin_windows_script(tray_bus_name), KWIN_SCRIPT_NAME)
        if self._script is None:
            self._log.warning("Couldn't load the KWin script that watches windows.")

    def stop(self):
        if self._script is not None:
            obs_window.unload_kwin_script(KWIN_SCRIPT_NAME, self._script)
            self._script = None
        self._windows = []

    def windows_changed(self, text):
        self._windows = parse_windows(text)

    def _captured_screen(self):
        now = self._clock()
        if self._screen is None or now - self._screen[1] >= CAPTURED_SCREEN_CACHE_SECONDS:
            self._screen = (self._find_screen(), now)
        return self._screen[0]

    def on_captured_screen(self):
        """The focused and fullscreen windows, bottom to top, on the screen
        OBS is capturing - or on any, if that isn't known."""
        if not self._windows:
            return []
        screen = self._captured_screen()
        return [w for w in self._windows if screen is None or w.get("screen") == screen]
