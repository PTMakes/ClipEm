"""Which windows are on the screen OBS is capturing, on Windows - for naming
clips after what's on it (app_audio.ActiveApp), as kde_windows.py does on KDE.

Windows just says: the foreground window, and every window in stacking order.
A fullscreen one is a window with no title bar covering its whole monitor,
which is how both exclusive and borderless fullscreen games look (a maximized
window keeps its title bar, however much of the monitor it overhangs).

The captured screen is the "monitor_id" OBS's screen capture is set to: OBS's
own picker saves the path EnumDisplayDevices gives for a monitor, but it can
also be its GDI name ("\\\\.\\DISPLAY1"), so a window's report has both. If it
can't be told, or OBS is capturing a window or game, every screen counts.

Nothing's kept: a window's title goes no further unless it's what a clip ends
up named after.

A program's name is what its .exe's version info calls it (program_name):
"Left 4 Dead 2" for left4dead2.exe, whose window is "Left 4 Dead 2 -
Direct3D 9". Its window and its sound (app_audio's Windows streams) both go
by it, so a clip is named the same as its game's track.
"""

import ctypes
import functools
import os
import re
import time
from ctypes import wintypes

CAPTURED_SCREEN_CACHE_SECONDS = 60
MONITOR_CAPTURE = "monitor_capture"

MONITOR_DEFAULTTONULL = 0
EDD_GET_DEVICE_INTERFACE_NAME = 1
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
# The desktop itself: a window that covers the monitor, but isn't an app.
DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
UNKNOWN_APP = "unknown"  # a process Windows won't name (some anti-cheat): its title will do
BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi"}
# Version info that names the engine or the kind of program, not the program
# itself. Compared case-insensitively.
GENERIC_PROGRAM_NAMES = {
    "game", "launcher", "application", "app", "client", "main", "bootstrappackagedgame", "unrealgame",
    "unreal engine", "unreal engine 4", "unreal engine 5", "unity", "unity player", "godot engine", "electron",
    "java(tm) platform se binary", "openjdk platform binary", "microsoft windows operating system",
}
# Code pages to try when an .exe doesn't list its own (US English, Unicode / Western / neutral).
FALLBACK_LANGUAGES = ("040904b0", "040904e4", "04090000")


class _Rect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _Rect), ("rcWork", _Rect), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


class _DisplayDevice(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128)]


def covers(window, monitor):
    """Whether a (left, top, right, bottom) rectangle covers another."""
    return (window[0] <= monitor[0] and window[1] <= monitor[1]
            and window[2] >= monitor[2] and window[3] >= monitor[3])


def is_fullscreen(style, window_rect, monitor_rect):
    return not (style & WS_CAPTION) == WS_CAPTION and covers(window_rect, monitor_rect)


def as_report(row, foreground):
    """A window as kde_windows reports them, or None if it's neither focused
    nor fullscreen. row: {"hwnd", "pid", "exe", "name", "title", "style",
    "rect", "monitor_rect", "screen"} - name: program_name(), if any."""
    fullscreen = is_fullscreen(row["style"], row["rect"], row["monitor_rect"])
    active = row["hwnd"] == foreground
    if not (fullscreen or active):
        return None
    app = os.path.splitext(row["exe"])[0] or UNKNOWN_APP
    report = {"pid": row["pid"], "app": app, "title": row["title"], "screen": row["screen"],
              "screen_name": row.get("screen_name", ""), "fullscreen": fullscreen, "active": active,
              # An .exe is often no name for a game ("cs2"), but a page's title is no name for a browser.
              "use_title": app == UNKNOWN_APP or (fullscreen and app.lower() not in BROWSERS)}
    if row.get("name"):
        report["name"] = row["name"]
    return report


def pick_program_name(values):
    """The first of an .exe's FileDescription and ProductName that names it,
    tidied ("Left 4 Dead 2"), or ""."""
    for value in values:
        name = re.sub(r"\s+", " ", re.sub("[®™©]", "", str(value or ""))).strip(" .-")
        if name and name.lower() not in GENERIC_PROGRAM_NAMES:
            return name
    return ""


def _version_strings(path, keys):
    """The values of an .exe's version info strings, in the order of keys
    (any it doesn't have left out)."""
    version = ctypes.windll.version
    version.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p)
    version.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    version.VerQueryValueW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.UINT))
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return []
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return []
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    languages = []
    if version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        words = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))  # (language, code page) pairs
        languages = [f"{words[i]:04x}{words[i + 1]:04x}" for i in range(0, length.value // 2 - 1, 2)]
    values = []
    for key in keys:
        for language in dict.fromkeys(languages + list(FALLBACK_LANGUAGES)):
            if version.VerQueryValueW(data, f"\\StringFileInfo\\{language}\\{key}", ctypes.byref(pointer),
                                      ctypes.byref(length)) and length.value:
                values.append(ctypes.wstring_at(pointer, length.value).rstrip("\0"))
                break
    return values


@functools.lru_cache(maxsize=256)
def program_name(path):
    """What an .exe's version info calls it, or "" if it can't be read or
    says nothing (GENERIC_PROGRAM_NAMES). path: the .exe's full path."""
    if not path:
        return ""
    try:
        return pick_program_name(_version_strings(path, ("FileDescription", "ProductName")))
    except (OSError, AttributeError, ValueError):  # AttributeError: not Windows
        return ""


def _monitor_id(user32, device):
    display = _DisplayDevice(cb=ctypes.sizeof(_DisplayDevice))
    return display.DeviceID if user32.EnumDisplayDevicesW(device, 0, ctypes.byref(display),
                                                          EDD_GET_DEVICE_INTERFACE_NAME) else ""


def _exe_path(kernel32, pid):
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return ""
    try:
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        return path.value if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(process)


def _window_row(hwnd, user32, kernel32, dwmapi, monitors):
    """A visible app window's details, or None. monitors: a cache by handle."""
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    cloaked = wintypes.DWORD()  # on another virtual desktop, or a suspended store app
    if dwmapi and not dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                                   ctypes.sizeof(cloaked)) and cloaked.value:
        return None
    class_name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_name, len(class_name))
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL)
    rect = _Rect()
    if class_name.value in DESKTOP_CLASSES or not monitor or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    if monitor not in monitors:
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        area = info.rcMonitor
        monitors[monitor] = ((area.left, area.top, area.right, area.bottom), _monitor_id(user32, info.szDevice),
                             info.szDevice)
    monitor_rect, screen, screen_name = monitors[monitor]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, len(title))
    path = _exe_path(kernel32, pid.value)
    return {"hwnd": hwnd, "pid": pid.value, "exe": os.path.basename(path), "name": program_name(path),
            "title": title.value,
            "style": user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF,
            "rect": (rect.left, rect.top, rect.right, rect.bottom), "monitor_rect": monitor_rect, "screen": screen,
            "screen_name": screen_name}


def _bind(user32, kernel32):
    """Handles are pointer-sized: without these, ctypes cuts them to 32 bits."""
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    user32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.c_void_p)
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
    for name in ("IsWindowVisible", "IsIconic"):
        getattr(user32, name).argtypes = (wintypes.HWND,)
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.c_void_p)
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_void_p)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def list_windows():
    """The focused and fullscreen windows, bottom to top, as kde_windows'
    script reports them."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    _bind(user32, kernel32)
    try:
        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmGetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
    except OSError:
        dwmapi = None
    # True pixels: a monitor and a window scaled differently would never match.
    previous = user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    try:
        foreground = user32.GetForegroundWindow()
        handles = []
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
            lambda hwnd, _lparam: handles.append(hwnd) or True)
        user32.EnumWindows(callback, 0)
        monitors = {}
        rows = (_window_row(hwnd, user32, kernel32, dwmapi, monitors) for hwnd in reversed(handles))
        reports = (as_report(row, foreground) for row in rows if row)
        return [report for report in reports if report]
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)


def captured_monitor(client, find_capture):
    """The "monitor_id" of OBS's screen capture, or None if it isn't one
    (a window or game capture) or can't be told. find_capture(client) ->
    stuck_screen's Capture."""
    capture = find_capture(client)
    if capture is None or capture.kind != MONITOR_CAPTURE:
        return None
    settings = client.call("GetInputSettings", {"inputName": capture.name})["inputSettings"]
    return settings.get("monitor_id") or None


class WinWindows:
    """on_captured_screen() is what clip names ask. find_screen() is the
    captured monitor's id, which may raise (OBS has gone away)."""

    def __init__(self, log, find_screen, clock=time.monotonic, windows=list_windows):
        self._log = log
        self._find_screen = find_screen
        self._clock = clock
        self._windows = windows
        self._screen = None  # (id or None, clock() when it was looked up)
        self._failed = False

    def _captured_screen(self):
        now = self._clock()
        if self._screen is None or now - self._screen[1] >= CAPTURED_SCREEN_CACHE_SECONDS:
            try:
                screen = self._find_screen()
            except Exception:
                screen = None
            self._screen = (screen, now)
        return self._screen[0]

    def on_captured_screen(self):
        """The focused and fullscreen windows, bottom to top, on the screen
        OBS is capturing - or on any, if that isn't known."""
        try:
            windows = self._windows()
        except Exception as exc:  # no reason a clip's name should suffer
            if not self._failed:
                self._log.warning(f"Can't tell which windows are open ({exc}) - clip names go by sound alone.")
                self._failed = True
            return []
        if not windows:
            return []
        screen = self._captured_screen()
        if screen is None:
            return windows
        wanted = screen.lower()
        return [window for window in windows
                if wanted in (str(window.get("screen", "")).lower(), str(window.get("screen_name", "")).lower())]
