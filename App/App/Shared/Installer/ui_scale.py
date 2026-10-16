"""
HiDPI handling shared by the setup wizard (gui.py) and its mic filter
popout (mic_filter_gui.py), so both render at the same physical size on
Windows and Linux.

Every hardcoded pixel value in those windows (geometry, padding, wrap
widths) is written for a 96 DPI (100%) display and passed through
UiScale.px() to get the real pixel count for this display. Fonts are
given in points and scale on their own.

Why the platforms need different handling:

  - Windows: python.exe doesn't declare DPI awareness, so on a scaled
    display Windows renders the whole Tk window at 96 DPI and then
    bitmap-stretches it (blurry, and the only reason it used to look
    "bigger" than on Linux). enable_dpi_awareness() opts out of that; Tk
    then reports the real DPI through `tk scaling`.
  - Linux (Xft Tk - see Dev/build/repack-linux-python-xft.sh): point-size fonts
    are handed to Xft as points and converted with the desktop's Xft.dpi
    resource, but Tk's own pixel math (`tk scaling`) comes from the X
    server's reported screen size, which is usually a flat 96 regardless
    of the desktop scale setting. So the real DPI is read from Xft.dpi
    directly, the same value Xft itself uses.
"""

import ctypes
import ctypes.util
import sys

BASE_DPI = 96.0


def enable_dpi_awareness():
    """Windows only; must run before the first Tk window is created."""
    if sys.platform != "win32":
        return
    try:
        # PROCESS_SYSTEM_DPI_AWARE. Not per-monitor: Tk 8.6 doesn't handle
        # WM_DPICHANGED, so on a second monitor with a different scale
        # Windows still stretches it (as before), just not on the main one.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # pre-8.1 fallback
        except (AttributeError, OSError):
            pass


def _xft_dpi():
    """The Xft.dpi X resource (what KDE/GNOME/XFCE set for display
    scaling), or None if unset or unreadable."""
    libname = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        xlib = ctypes.CDLL(libname)
    except OSError:
        return None
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XGetDefault.restype = ctypes.c_char_p
    xlib.XGetDefault.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = xlib.XOpenDisplay(None)
    if not display:
        return None
    try:
        value = xlib.XGetDefault(display, b"Xft", b"dpi")
        dpi = float(value) if value else None
    except ValueError:
        dpi = None
    finally:
        xlib.XCloseDisplay(display)
    return dpi if dpi and dpi > 0 else None


class UiScale:
    def __init__(self, root):
        """Call once, right after creating the Tk root."""
        dpi = root.winfo_fpixels("1i")
        if root.tk.call("tk", "windowingsystem") == "x11":
            dpi = _xft_dpi() or dpi
            # Tk's default X11 fonts (TkDefaultFont etc.) are sized in
            # pixels (-12), which Tk converts to points via `tk scaling`
            # and Xft converts back with Xft.dpi. Pinning `tk scaling` to
            # 96 DPI makes those pixels mean 96 DPI pixels, so they scale
            # by the same dpi / 96 as everything else.
            root.tk.call("tk", "scaling", BASE_DPI / 72.0)
        self.factor = max(dpi / BASE_DPI, 1.0)

    def px(self, value):
        return int(round(value * self.factor))
