"""System tray companion for ClipEm.

Shows a static tray icon, a tooltip with replay-buffer status, and a
right-click menu with a Start/Stop Replay Buffer toggle plus a couple of
conveniences.

Talks to OBS purely over obs-websocket (see obsws_client.py) - it does not
touch OBS's process or files directly, so it can run/crash/restart
independently of OBS.

Requires: Pillow, websocket-client, and either pystray (Windows/macOS) or
dbus-next (Linux - see sni_tray.py) (see requirements.txt).
Run it with the same distribution's Python (bundled embeddable Python on
Windows, system python3 on macOS/Linux - see the platform launch scripts).
"""

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageFilter

# Linux talks StatusNotifierItem/dbusmenu directly (sni_tray.py) instead of
# through pystray's GTK/AppIndicator backend, which needs PyGObject and
# libayatana-appindicator - system packages pip can't vendor. sni_tray.py
# exposes the same Icon/Menu/MenuItem surface pystray does, so nothing below
# this needs to know which one it's actually talking to.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Installer"))
if sys.platform == "linux":
    import sni_tray as pystray
else:
    import pystray

import autostart  # noqa: E402 - from App/Shared/Installer, see sys.path above
import editor_window  # noqa: E402
import obs_config  # noqa: E402
import obs_process  # noqa: E402
import obs_window  # noqa: E402
from app_audio import AppTrackMonitor  # noqa: E402
from app_window import use_windows_app_id  # noqa: E402
from clip_folder import ClipFolderWatch  # noqa: E402
from clipem_common import (  # noqa: E402
    LOG_DIR,
    acquire_instance_lock,
    load_run_config,
    pid_alive,
    release_instance_lock,
    setup_logging,
    start_clipem,
)
from disk_space import DiskSpaceMonitor, format_size  # noqa: E402
from obsws_client import ObsWebSocketConnectionLost, ObsWebSocketError, connect_with_retry  # noqa: E402
from stuck_screen import DisplayChoice, PipeWireStreamWatch, StuckScreenMonitor  # noqa: E402

log = setup_logging("tray")

# pystray's Windows backend only pops the menu up on right-click; left-click
# runs the menu's default item, and ClipEm's has none, so it did nothing.
# Route left-clicks through the right-click path so either button opens the
# menu, as it does on Linux (sni_tray.py reports ItemIsMenu). Overrides
# pystray 0.19.5's private _win32.Icon._on_notify (pinned in requirements.txt).
if sys.platform == "win32":
    _WM_LBUTTONUP = 0x0202
    _WM_RBUTTONUP = 0x0205

    class _TrayIcon(pystray.Icon):
        def _on_notify(self, wparam, lparam):
            if lparam == _WM_LBUTTONUP:
                lparam = _WM_RBUTTONUP
            return super()._on_notify(wparam, lparam)

        # Also pystray 0.19.5's private _win32.Icon._create_menu (it builds
        # each submenu through this too): no check-mark column. Win32 menus
        # keep a blank strip left of every item for one, which made the menu
        # much wider than on Linux - and ClipEm's has no checkable items.
        def _create_menu(self, descriptors, callbacks):
            hmenu = super()._create_menu(descriptors, callbacks)
            if hmenu:
                _without_check_column(hmenu)
            return hmenu

    def _without_check_column(hmenu):
        import ctypes
        from ctypes import wintypes

        class MENUINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.DWORD), ("dwStyle", wintypes.DWORD),
                        ("cyMax", wintypes.UINT), ("hbrBack", wintypes.HANDLE),
                        ("dwContextHelpID", wintypes.DWORD), ("dwMenuData", ctypes.c_size_t)]

        mim_style, mns_nocheck = 0x10, 0x80000000
        info = MENUINFO(cbSize=ctypes.sizeof(MENUINFO), fMask=mim_style, dwStyle=mns_nocheck)
        try:
            handle = getattr(hmenu, "value", hmenu)  # an int, or a ctypes HMENU
            ctypes.windll.user32.SetMenuInfo(ctypes.c_void_p(handle), ctypes.byref(info))
        except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
            pass
else:
    _TrayIcon = pystray.Icon


def use_dark_menus():
    """Makes the tray menu (a plain Win32 popup menu) dark. Windows has no
    public API for that: uxtheme's unnamed SetPreferredAppMode (ordinal 135,
    Windows 10 1903+) with ForceDark, then FlushMenuThemes (136) so menus
    made afterwards pick it up. Older Windows: they stay light."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        uxtheme = ctypes.WinDLL("uxtheme")
        force_dark = 2
        set_preferred_app_mode = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)((135, uxtheme))
        flush_menu_themes = ctypes.WINFUNCTYPE(None)((136, uxtheme))
        set_preferred_app_mode(force_dark)
        flush_menu_themes()
    except (AttributeError, OSError) as exc:
        log.info(f"Dark tray menu unavailable: {exc}")

POLL_SECONDS = 2

# "Quit ClipEm + OBS": how long to wait for OBS to exit before giving up
# (and just logging it).
OBS_EXIT_WAIT_SECONDS = 15
ICON_SIZE = 64

# How long we'll keep retrying a lost/never-established OBS connection before
# concluding OBS has been closed for good and quitting ourselves. Without
# this, closing OBS any way other than through this tray icon's own menu
# left the widget spinning on obs-websocket reconnects forever.
MAX_DISCONNECTED_SECONDS = 120

# How long to give a Linux desktop's tray host (panel/extension) to pick up
# our icon before assuming there isn't one. Registering with a
# StatusNotifierWatcher that never shows up doesn't raise or report failure
# on its own (see sni_tray.py) - so an icon that's invisible this long has
# no menu for a user to Quit from and would otherwise run forever.
TRAY_AVAILABILITY_CHECK_SECONDS = 20

SHARED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tray.lock")
# How much of a helper window's stderr goes into tray.log when it fails.
HELPER_STDERR_TAIL_BYTES = 4000
SETTINGS_DIALOG_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings_dialog.py"
)
CLIP_BROWSER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clip_browser.py")
# The running clip editor's pid and launch URL (written by clip_browser.py).
CLIP_BROWSER_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clip_browser.lock")
CLIP_BROWSER_URL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clip_browser.url")


def clip_editor_quit_url(launch_url):
    """http://127.0.0.1:port/<token>/api/quit from the editor's launch URL
    (http://127.0.0.1:port/clipem-editor/#<token>), or None."""
    parts = urllib.parse.urlsplit(launch_url or "")
    if parts.hostname != "127.0.0.1" or not parts.port or not parts.fragment:
        return None
    return f"http://127.0.0.1:{parts.port}/{urllib.parse.quote(parts.fragment)}/api/quit"


def stop_clip_editor(timeout_seconds=3):
    """Closes the clip editor if one is running - started from the tray or
    on its own from the Start menu: asks it to quit (it cancels any export
    and closes its window), then terminates it if it's still there."""
    try:
        with open(CLIP_BROWSER_LOCK_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    if pid == os.getpid() or not pid_alive(pid):
        return
    try:
        with open(CLIP_BROWSER_URL_PATH, "r", encoding="utf-8") as f:
            url = clip_editor_quit_url(f.read().strip())
    except OSError:
        url = None
    if not url:
        return
    try:
        request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=2).close()
    except Exception as exc:
        # Nothing answered: a stale lock file whose pid may now be some
        # other program - leave it alone.
        log.warning(f"Couldn't ask the clip editor to close: {exc}")
        return
    deadline = time.monotonic() + timeout_seconds + 2  # it gives its window 3 s to close
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if pid_alive(pid):
        log.warning("The clip editor didn't close - ending it.")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                               creationflags=0x08000000)
            else:
                import signal

                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def ensure_single_instance():
    """Exits immediately if another tray_widget.py is already alive.

    Each fresh `run.py` launch used to spawn a brand new tray_widget.py
    without checking for an old one - since OBS itself can be relaunched
    repeatedly (its own "already running" check is system-wide, not tied to
    this distribution's folder, and is bypassed entirely by --multi), stale
    tray widgets from earlier sessions piled up. They don't crash - they just
    sit retrying a websocket connection forever - but multiple of them
    fighting over the same tray slot means newer ones can silently fail to
    show an icon at all. One lock file (this distribution's own folder, so
    separate copies of the whole tool still get their own tray widget) is
    enough to stop that pile-up.
    """
    if not acquire_instance_lock(LOCK_PATH):
        log.warning("Tray widget already running; not starting another.")
        sys.exit(0)


def use_clipem_notification_name():
    """Windows names a notification after the process's AppUserModelID - by
    default pythonw.exe's ("Python"). Setup's Start Menu shortcut carries
    WINDOWS_APP_ID, so claiming it makes them say "ClipEm" - but only once
    that shortcut exists, or Windows would show the bare ID instead."""
    if sys.platform != "win32":
        return
    path = autostart.start_menu_shortcut_path()
    if path and os.path.exists(path):
        use_windows_app_id(autostart.WINDOWS_APP_ID)


def tray_host_available():
    """Best-effort check for a system tray host, Linux only.

    Windows/macOS always have a taskbar/menu-bar tray, so this only matters
    on Linux, where a StatusNotifierWatcher (implemented by KDE's tray, most
    GNOME "AppIndicator" extensions, etc.) may simply not be running. Any
    ambiguity (dbus-send missing, unexpected output) is treated as
    "available" so this never kills a perfectly working tray icon.
    """
    if sys.platform != "linux":
        return True

    try:
        result = subprocess.run(
            [
                "dbus-send",
                "--session",
                "--dest=org.freedesktop.DBus",
                "--type=method_call",
                "--print-reply",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.NameHasOwner",
                "string:org.kde.StatusNotifierWatcher",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True

    if result.returncode != 0:
        return True
    return "boolean false" not in result.stdout


def show_obs_window():
    """Best-effort: bring this install's OBS to the front - see obs_window.py.
    Returns False if it isn't running or couldn't be shown. Never raises."""
    return obs_window.show_obs(log)


def close_obs_window(client=None):
    """Best-effort: stop OBS's outputs (via client, if connected) and ask this
    distribution's OBS to close - see obs_process.close_obs. Never raises."""
    try:
        obs_process.close_obs(log, client=client, timeout_seconds=OBS_EXIT_WAIT_SECONDS)
    except Exception as exc:
        log.warning(f"Failed to close OBS: {exc}")


# The tray icon itself - the ClipEm logo, resized to tray size. Dimmed
# (lower alpha) while disconnected from OBS, glowing red behind the logo
# while the replay buffer is actively recording.
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ClipEm.png")
_logo_source = Image.open(LOGO_PATH).convert("RGBA")

GLOW_COLOR = (230, 45, 45)
GLOW_FALLOFF = 1.6  # >1 shrinks the glow inward so it fades out before the canvas edge.
GLOW_BLUR_FRACTION = 0.06  # blur radius as a fraction of icon size, for a soft edge.


def _radial_glow(size):
    gradient = Image.radial_gradient("L").resize((size, size), Image.LANCZOS)
    alpha = gradient.point(lambda v: max(0, 255 - int(v * GLOW_FALLOFF)))
    glow = Image.new("RGBA", (size, size), GLOW_COLOR + (0,))
    glow.putalpha(alpha)
    return glow.filter(ImageFilter.GaussianBlur(max(1, round(size * GLOW_BLUR_FRACTION))))


def app_icon(dim=False, active=False):
    logo = _logo_source.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
    if active:
        img = _radial_glow(ICON_SIZE)
        img.alpha_composite(logo)
    else:
        img = logo
    if dim:
        r, g, b, a = img.split()
        a = a.point(lambda v: v * 0.45)
        img = Image.merge("RGBA", (r, g, b, a))
    return img


class TrayApp:
    def __init__(self):
        self.client = None
        self.replay_active = False
        self.recording_active = False
        self.connected = False
        self._children = []  # helper processes we started - see stop_children
        self._stopping_children = False  # their exit codes are ours, not errors, from then on
        self.obs_window = obs_window.ObsWindowState(log)  # for Show OBS / Hide OBS
        self.stuck_screen = StuckScreenMonitor(
            self._notify,
            DisplayChoice(obs_config.load_setup_capture_display, obs_config.set_capture_display_user_changed),
            PipeWireStreamWatch(obs_config.OBS_LOG_DIR),
        )
        self.disk_space = DiskSpaceMonitor(self._notify, fallback_dir=obs_config.load_clip_folder)
        self.app_tracks = AppTrackMonitor()
        self.clip_folder = ClipFolderWatch(
            self._notify,
            fallback_dir=obs_config.load_clip_folder,
            deferred=os.environ.pop("CLIPEM_REPLAY_BUFFER_DEFERRED", "") == "1",  # see run.py
        )
        self.icon = _TrayIcon(
            "clipem",
            icon=app_icon(dim=True),
            title="ClipEm: connecting...",
            menu=self._build_menu(),
        )
        if hasattr(self.icon, "obs_window_callback"):  # sni_tray (Linux)
            self.icon.obs_window_callback = self._on_obs_window_reported

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(
                "Clips Folder Unavailable",
                pystray.Menu(
                    pystray.MenuItem(self._missing_clip_folder_label, None, enabled=lambda item: False),
                    pystray.MenuItem("Check Again", self._check_clip_folder),
                    pystray.MenuItem("Create Clips Folder", self._create_clip_folder),
                ),
                visible=self._clip_folder_missing,
            ),
            pystray.MenuItem(self._save_clip_label, self._save_clip, enabled=self._can_save_clip),
            pystray.MenuItem("Open Clip Editor", self._open_clip_editor),
            pystray.MenuItem("Open Clips Folder", self._open_clips_folder),
            pystray.MenuItem(self._replay_label, self._toggle_replay_buffer),
            pystray.MenuItem(self._recording_label, self._toggle_recording),
            pystray.MenuItem(self._obs_window_label, self._toggle_obs_window),
            pystray.MenuItem("Settings...", self._open_settings),
            pystray.MenuItem(
                "Quit",
                pystray.Menu(
                    pystray.MenuItem("Restart ClipEm Tray and OBS", self._restart_clipem_and_obs),
                    pystray.MenuItem("Quit ClipEm Tray Only", self._quit),
                    pystray.MenuItem("Quit ClipEm Tray and OBS", self._quit_and_close_obs),
                ),
            ),
        )

    def _replay_label(self, item):
        if not self.connected:
            return "Replay Buffer (disconnected)"
        return "Stop Replay Buffer" if self.replay_active else "Start Replay Buffer"

    def _save_clip_label(self, item):
        """ "Save Clip" plus the configured clip hotkey, re-read each time the
        menu is rebuilt so a change from Settings shows up. Inline on every
        platform: a Windows shortcut column (after a tab) made the menu much
        wider than on Linux."""
        try:
            hotkey = obs_config.describe_hotkey(*obs_config.load_clip_hotkey())
        except Exception:
            return "Save Clip"
        return f"Save Clip ({hotkey})"

    def _can_save_clip(self, item):
        return self.connected and self.replay_active

    def _toggle_replay_buffer(self, icon, item):
        if not self.connected or self.client is None:
            return
        try:
            if self.replay_active:
                self.client.call("StopReplayBuffer")
            else:
                self.client.call("StartReplayBuffer")
        except ObsWebSocketError as exc:
            log.warning(f"Failed to toggle replay buffer: {exc}")

    def _recording_label(self, item):
        if not self.connected:
            return "Recording (disconnected)"
        return "Stop Recording" if self.recording_active else "Start Recording"

    def _toggle_recording(self, icon, item):
        if not self.connected or self.client is None:
            return
        try:
            if self.recording_active:
                self.client.call("StopRecord")
            else:
                self.client.call("StartRecord")
        except ObsWebSocketError as exc:
            log.warning(f"Failed to toggle recording: {exc}")

    def _notify(self, message):
        # Not every tray host implements org.freedesktop.Notifications (or
        # pystray's own per-platform notify path) - this is a convenience,
        # not something that should ever crash the poll/action thread.
        try:
            self.icon.notify(message, "ClipEm")
        except Exception as exc:
            log.warning(f"Couldn't show notification {message!r}: {exc!r}")

    def _save_clip(self, icon, item):
        if not self.connected or self.client is None or not self.replay_active:
            return
        try:
            self.client.call("SaveReplayBuffer")
            if load_run_config().get("clip_saved_notification", True):  # Settings > Clip Sound
                self._notify("Clip saved.")
        except ObsWebSocketError as exc:
            log.warning(f"Failed to save clip: {exc}")
            self._notify(f"Failed to save clip: {exc}")

    def _open_clip_editor(self, icon, item):
        if not editor_window.installed():
            self._notify("Setting up the clip editor - downloading its window (about 130 MB)...")
        self._start_helper(CLIP_BROWSER_SCRIPT, "the clip editor")

    def _clip_folder_missing(self, item):
        return self.connected and self.clip_folder.missing_dir is not None

    def _missing_clip_folder_label(self, item):
        return f"Waiting for {self.clip_folder.missing_dir}"

    def _check_clip_folder(self, icon, item):
        if self.connected and self.client is not None:
            self.clip_folder.tick(self.client, self.replay_active)
            self.icon.update_menu()

    def _create_clip_folder(self, icon, item):
        if self.connected and self.client is not None:
            self.clip_folder.create_missing_dir(self.client, self.replay_active)
            self.icon.update_menu()

    def _open_clips_folder(self, icon, item):
        # The folder the user picked in Setup (basic.ini), not a fixed path.
        clips_dir = obs_config.load_clip_folder()
        if not os.path.isdir(clips_dir):
            # Not created here: on a cloud drive that isn't mounted yet, a
            # local folder in its mount point can stop it mounting.
            if clips_dir != obs_config.DEFAULT_CLIPS_DIR:
                self._notify(f"The clips folder {clips_dir} isn't available.")
                return
            os.makedirs(clips_dir, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(clips_dir)  # noqa: S606 - user-initiated, opens own folder
        elif sys.platform == "darwin":
            subprocess.Popen(["open", clips_dir])
        else:
            subprocess.Popen(["xdg-open", clips_dir])

    def _obs_window_label(self, item):
        return "Hide OBS" if self.obs_window.visible else "Show OBS"

    def _toggle_obs_window(self, icon, item):
        if self.obs_window.visible:
            if obs_window.hide_obs(log):
                self.obs_window.remember(False)
        elif show_obs_window():
            self.obs_window.remember(True)
        elif obs_process.obs_running():
            self._notify("Couldn't bring OBS to the front - see logs/tray.log.")
        else:
            self._notify("OBS isn't running. Start ClipEm from its shortcut to open it.")
        self.icon.update_menu()

    def _on_obs_window_reported(self, visible):
        self.obs_window.reported(visible)
        self.icon.update_menu()

    def _open_settings(self, icon, item):
        self._start_helper(SETTINGS_DIALOG_SCRIPT, "settings")

    def _start_helper(self, script, what):
        """Runs a helper window's script (the clip editor, the settings
        dialog) and tells the user if it exits with an error. Its stderr goes
        to logs/<script>.stderr.log for that: under pythonw, a crash before it
        opens its own log (e.g. a failed import) otherwise leaves no trace.
        A file, not a pipe: what the helper starts (Settings restarting
        ClipEm and OBS, the user's video editor) inherits it and outlives
        this tray widget - a pipe would break under them (SIGPIPE)."""
        name = os.path.splitext(os.path.basename(script))[0]
        stderr_path = os.path.join(LOG_DIR, f"{name}.stderr.log")
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(stderr_path, "wb") as stderr_file:
                proc = subprocess.Popen(
                    [sys.executable, script, "--parent-pid", str(os.getpid())], stderr=stderr_file
                )
        except Exception as exc:
            log.warning(f"Failed to open {what}: {exc}")
            self._notify(f"Couldn't open {what} - see logs/tray.log.")
            return
        self._children.append(proc)
        threading.Thread(target=self._watch_helper, args=(proc, what, stderr_path), daemon=True).start()

    def _watch_helper(self, proc, what, stderr_path):
        code = proc.wait()
        if code == 0 or self._stopping_children:
            return
        try:
            with open(stderr_path, "rb") as f:
                f.seek(max(0, os.path.getsize(stderr_path) - HELPER_STDERR_TAIL_BYTES))
                tail = f.read().decode("utf-8", errors="replace").strip()
        except OSError:
            tail = ""
        log.warning(f"{what[0].upper()}{what[1:]} exited with code {code}:\n{tail}")
        self._notify(f"{what[0].upper()}{what[1:]} closed with an error - see logs/tray.log.")

    def stop_children(self, timeout_seconds=3):
        """Closes the helper windows (the settings dialog) this tray widget
        opened - they'd otherwise stay open after it quits. Asks first
        (SIGTERM / TerminateProcess), then kills whatever is left. The clip
        editor - ours or one opened from the Start menu - is asked first."""
        self._stopping_children = True
        stop_clip_editor(timeout_seconds)
        running = [child for child in self._children if child.poll() is None]
        for child in running:
            try:
                child.terminate()
            except OSError:
                pass
        deadline = time.monotonic() + timeout_seconds
        for child in running:
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
        self._children.clear()

    def _quit(self, icon, item):
        self.stop_children()
        if self.client:
            self.client.close()
        icon.stop()

    def _quit_and_close_obs(self, icon, item):
        close_obs_window(self.client if self.connected else None)
        self._quit(icon, item)

    def _restart_clipem_and_obs(self, icon, item):
        """Closes OBS (stopping its outputs first), then starts ClipEm again -
        OBS and a fresh tray widget - and quits this one. Nothing restarts if
        OBS won't close (e.g. it's showing a dialog)."""
        try:
            closed = obs_process.close_obs(
                log, client=self.client if self.connected else None, timeout_seconds=OBS_EXIT_WAIT_SECONDS
            )
        except Exception as exc:
            log.warning(f"Restart: couldn't close OBS: {exc}")
            closed = False
        if not closed:
            self._notify("OBS didn't close (it may be showing a dialog) - nothing was restarted.")
            return
        # Hand the single-instance lock over first, or the new tray widget
        # would see this one still running and exit.
        release_instance_lock(LOCK_PATH)
        try:
            start_clipem()
        except Exception as exc:
            log.warning(f"Restart: couldn't start ClipEm again: {exc}")
            self._notify(f"Couldn't start ClipEm again ({exc}) - use its shortcut.")
        log.info("Restart: started ClipEm again; quitting this tray widget.")
        self._quit(icon, item)

    def _shutdown(self, reason):
        log.warning(f"Shutting down: {reason}")
        if self.client:
            self.client.close()
        self.icon.stop()

    def _connect(self):
        settings = obs_config.load_obs_websocket_settings()
        url = f"ws://127.0.0.1:{settings['port']}"
        self.client = connect_with_retry(
            url,
            settings["password"],
            timeout_seconds=MAX_DISCONNECTED_SECONDS,
            log=log.info,
        )
        self.client.on_event("InputSettingsChanged", self._on_input_settings_changed)
        self.connected = True
        log.info(f"Connected to OBS at {url}.")
        self.stuck_screen.forget()
        self.app_tracks.forget()
        self.clip_folder.forget()

    def _on_input_settings_changed(self, event):
        try:
            self.stuck_screen.display_choice.on_input_settings_changed(event)
        except Exception as exc:
            log.warning(f"Couldn't record a capture display change: {exc}")

    @staticmethod
    def _stuck_check_enabled():
        try:
            return obs_config.load_stuck_check_enabled()
        except Exception as exc:
            log.warning(f"Couldn't read the stuck-screen setting ({exc}); leaving the check on.")
            return True

    @staticmethod
    def _black_check_enabled():
        try:
            return obs_config.load_black_check_enabled()
        except Exception as exc:
            log.warning(f"Couldn't read the black-screen setting ({exc}); leaving the check on.")
            return True

    @staticmethod
    def _disk_space_warnings_enabled():
        try:
            return obs_config.load_disk_space_warnings_enabled()
        except Exception as exc:
            log.warning(f"Couldn't read the disk-space warnings setting ({exc}); leaving them on.")
            return True

    def _check_tray_available(self):
        time.sleep(TRAY_AVAILABILITY_CHECK_SECONDS)
        if not tray_host_available():
            self._shutdown(
                f"No system tray host found after {TRAY_AVAILABILITY_CHECK_SECONDS}s "
                "(nothing to show the icon or its Quit menu in)."
            )

    def _poll_loop(self):
        while True:
            if not self.connected:
                try:
                    self._connect()
                except Exception as exc:
                    self._shutdown(f"could not reach OBS ({exc})")
                    return

            try:
                status = self.client.call("GetReplayBufferStatus")
                self.replay_active = status.get("outputActive", False)
                record_status = self.client.call("GetRecordStatus")
                self.recording_active = record_status.get("outputActive", False)
                self.icon.icon = app_icon(active=self.replay_active or self.recording_active)

                replay_text = "recording" if self.replay_active else "idle"
                record_text = "recording" if self.recording_active else "idle"
                record_bytes = record_status.get("outputBytes", 0)
                if self.recording_active and record_bytes:
                    record_text += f" ({format_size(record_bytes)})"
                self.icon.title = f"ClipEm: Replay Buffer {replay_text}, Recording {record_text}"
                self.icon.update_menu()

                self.stuck_screen.tick(self.client, self._stuck_check_enabled, self._black_check_enabled)
                self.disk_space.tick(self.client, self._disk_space_warnings_enabled,
                                     self.replay_active or self.recording_active, self.recording_active, record_bytes)
                self.app_tracks.tick(self.client)
                missing_before = self.clip_folder.missing_dir
                self.clip_folder.tick(self.client, self.replay_active)
                if self.clip_folder.missing_dir != missing_before:
                    self.icon.update_menu()
            except ObsWebSocketConnectionLost as exc:
                # e.g. OBS restarted to apply new websocket settings - this
                # connection is dead for good; the next loop reconnects,
                # re-reading the port/password from disk.
                self._mark_disconnected(exc)
            except ObsWebSocketError as exc:
                log.warning(f"obs-websocket error: {exc}")
            except Exception as exc:
                self._mark_disconnected(exc)

            self._refresh_obs_window()
            time.sleep(POLL_SECONDS)

    def _refresh_obs_window(self):
        try:
            if self.obs_window.refresh(obs_process.find_obs_pids(), getattr(self.icon, "bus_name", None)):
                self.icon.update_menu()
        except Exception as exc:
            log.warning(f"Couldn't check OBS's window: {exc}")

    def _mark_disconnected(self, reason):
        log.warning(f"Lost connection to OBS: {reason}")
        self.connected = False
        self.replay_active = self.recording_active = False
        if self.client:
            self.client.close()
        self.icon.icon = app_icon(dim=True)
        self.icon.title = "ClipEm: disconnected"
        self.icon.update_menu()

    def run(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._check_tray_available, daemon=True).start()
        self.icon.run()


if __name__ == "__main__":
    ensure_single_instance()
    use_clipem_notification_name()
    use_dark_menus()
    app = TrayApp()
    try:
        app.run()
    finally:
        app.stop_children()
        app.obs_window.stop()
        # The icon is gone: end the process now, rather than waiting on
        # whatever else is still running in it (a menu action closing OBS,
        # a stuck D-Bus or websocket call) - that left a python3 running in
        # the background with no icon to quit it from.
        log.info("Tray widget stopped.")
        release_instance_lock(LOCK_PATH)
        logging.shutdown()
        os._exit(0)
