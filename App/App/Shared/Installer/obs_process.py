"""
Finds and closes this distribution's own OBS process - shared by run.py,
the tray widget ("Quit ClipEm + OBS") and its settings dialog (restarting
OBS to apply settings). Only OBS executables inside this distribution's
folder count, so a separate system/Flatpak OBS install is never touched.

Closing is always a normal close request (the window's close button,
SIGTERM, or AppleScript "quit"), never a force-kill, so OBS still saves
its state on the way out.
"""

import os
import signal
import subprocess
import sys
import time

from clipem_common import ROOT_DIR, process_exe_path_win32

OBS_EXECUTABLE_NAMES = {"obs64.exe", "obs"}  # Windows, Linux (macOS: OBS.app/Contents/MacOS/OBS)

# How long stop_obs_outputs waits for OBS's outputs to wind down.
OUTPUT_STOP_WAIT_SECONDS = 10


def _is_ours(exe_path):
    if not exe_path:
        return False
    exe_path = os.path.normcase(os.path.realpath(exe_path))
    if not exe_path.startswith(os.path.normcase(ROOT_DIR) + os.sep):
        return False
    return os.path.basename(exe_path).lower() in OBS_EXECUTABLE_NAMES or exe_path.lower().endswith(
        "/obs.app/contents/macos/obs"
    )


def _all_pids_win32():
    import ctypes
    from ctypes import wintypes

    count = 4096
    while True:
        pids = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not ctypes.windll.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        n = needed.value // ctypes.sizeof(wintypes.DWORD)
        if n < count:
            return list(pids[:n])
        count *= 2


def find_obs_pids():
    """PIDs of this distribution's running OBS process(es)."""
    found = []
    try:
        if sys.platform == "win32":
            for pid in _all_pids_win32():
                if pid and _is_ours(process_exe_path_win32(pid)):
                    found.append(pid)
        elif sys.platform == "darwin":
            out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                pid, _, exe = line.strip().partition(" ")
                if pid.isdigit() and _is_ours(exe.strip()):
                    found.append(int(pid))
        else:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    exe = os.readlink(f"/proc/{entry}/exe")
                except OSError:
                    continue
                if _is_ours(exe):
                    found.append(int(entry))
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def obs_running():
    return bool(find_obs_pids())


def _find_main_window_win32(pids):
    """(hwnd, pid, title) of OBS's main window in one of pids, or None.
    Hidden windows count: started with --minimize-to-tray, OBS's main window
    exists (OBS forces its native handle via SetWin32DropStyle) but is never
    shown. Its title is "OBS <version> - ..." (OBSBasic::UpdateTitleBar)."""
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
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.startswith("OBS "):
            found.append((hwnd, pid.value, buf.value))
            return False
        return True

    user32.EnumWindows(_enum_proc, 0)
    return found[0] if found else None


def request_obs_close(log):
    """Asks this distribution's OBS to close. Returns True if a close request
    was sent. Never raises."""
    try:
        pids = find_obs_pids()
        if sys.platform == "win32":
            match = _find_main_window_win32(set(pids)) if pids else None
            if not match:
                log.warning("Close OBS: no OBS main window from this install found.")
                return False
            import ctypes

            hwnd, pid, title = match
            log.info(f"Close OBS: sending WM_CLOSE to '{title}' (pid {pid}).")
            WM_CLOSE = 0x0010
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return True
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'tell application "OBS" to quit'], timeout=5)
            return True
        # Linux: OBS is started with --minimize-to-tray, so under Wayland it
        # may have no window at all for wmctrl/xdotool to close. SIGTERM is
        # what OBS's own window-close path boils down to anyway
        # (OBSApp::processSigTerm: saves state, then quits).
        # ClipEm only ever runs its own OBS build, so there's nothing else to
        # look for - an OBS outside this folder is the user's own.
        if not pids:
            log.warning("Close OBS: no OBS from this install is running.")
            return False
        for pid in pids:
            os.kill(pid, signal.SIGTERM)
        log.info(f"Close OBS: sent SIGTERM to pid(s) {pids}.")
        return True
    except Exception as exc:
        log.warning(f"Close OBS failed: {exc}")
        return False


def wait_for_obs_exit(timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not find_obs_pids():
            return True
        time.sleep(0.25)
    return not find_obs_pids()


def stop_obs_outputs(client, log, timeout_seconds=OUTPUT_STOP_WAIT_SECONDS):
    """OBS asks "are you sure you want to exit?" instead of closing while
    any output is active (General/ConfirmOnExit, on by default - see
    OBSBasic::closeEvent), and ClipEm always runs it with the replay buffer
    on. Before closing OBS on the user's behalf, stop the replay buffer and
    any recording (a recording is finalized, not lost) over obs-websocket and
    wait for them to wind down. Never raises."""
    stops = (("GetReplayBufferStatus", "StopReplayBuffer"), ("GetRecordStatus", "StopRecord"))
    try:
        for status_request, stop_request in stops:
            if client.call(status_request).get("outputActive"):
                log.info(f"Close OBS: {stop_request}")
                client.call(stop_request)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not any(client.call(status).get("outputActive") for status, _ in stops):
                return
            time.sleep(0.25)
        log.warning(f"Close OBS: outputs still active after {timeout_seconds}s.")
    except Exception as exc:
        log.warning(f"Close OBS: couldn't stop OBS's outputs ({exc}) - closing anyway.")


def connect_to_obs(log, timeout=5):
    """An obs-websocket client connected to the running OBS (with the
    settings OBS itself was started with), or None if that isn't possible -
    no connection, or websocket-client isn't importable from this Python
    (it's installed with the tray widget). Never raises."""
    import obs_config
    from clipem_common import APP_DIR, SHARED_DIR

    for path in (
        os.path.join(SHARED_DIR, "tray-widget"),
        os.path.join(APP_DIR, "Linux", "tray-deps"),
        os.path.join(APP_DIR, "macOS", "tray-deps"),
    ):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
    try:
        from obsws_client import ObsWebSocketClient

        ws = obs_config.load_obs_websocket_settings()
        client = ObsWebSocketClient(f"ws://127.0.0.1:{ws['port']}", ws["password"])
        client.connect(timeout=timeout)
        return client
    except Exception as exc:
        log.warning(f"Close OBS: couldn't connect to obs-websocket ({exc}) - closing without stopping outputs.")
        return None


def close_obs_stopping_outputs(log, timeout_seconds=15):
    """close_obs() with its own obs-websocket connection, for callers that
    don't already have one (Setup, the settings dialog)."""
    if not find_obs_pids():
        return True
    client = connect_to_obs(log)
    try:
        return close_obs(log, client=client, timeout_seconds=timeout_seconds)
    finally:
        if client is not None:
            client.close()


def close_obs(log, client=None, timeout_seconds=15):
    """Stops OBS's outputs (if an obs-websocket client is given), asks OBS to
    close and waits for it to exit. Returns True once no OBS from this
    distribution is running."""
    if not find_obs_pids():
        return True
    if client is not None:
        stop_obs_outputs(client, log)
    if not request_obs_close(log):
        return False
    if wait_for_obs_exit(timeout_seconds):
        log.info("Close OBS: OBS exited.")
        return True
    log.warning(f"Close OBS: OBS still running after {timeout_seconds}s (an OBS dialog may be waiting for an answer).")
    return False
