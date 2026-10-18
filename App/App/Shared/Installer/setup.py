"""
Universal setup entry point for ClipEm.

Setup_Windows.bat, SetupMac.command, and Setup_Linux.sh are thin shims that
just find (or, on Linux, bootstrap) a Python interpreter and exec this
file - all the actual platform-dispatch logic (fetching ClipEm's own OBS
build if it's missing, asking where to save clips, offering to install the
tray widget) lives here once instead of being duplicated per-OS.

Safe to re-run any time - every step here is idempotent or asks first.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

# The embeddable Windows Python's ._pth file puts it in isolated mode, which
# leaves this script's own folder off sys.path - without this, the sibling
# imports below fail before anything runs.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autostart  # noqa: E402
import editor_window  # noqa: E402
import mic_filter  # noqa: E402
import obs_config  # noqa: E402
import obs_install  # noqa: E402
import obs_process  # noqa: E402
import updater  # noqa: E402
import windows_python  # noqa: E402
from autostart import apply_boot_autostart, find_windows_python_root  # noqa: E402
from clipem_common import (  # noqa: E402
    APP_DIR,
    DEFAULT_RUN_CONFIG,  # noqa: F401 - re-exported for older importers
    INSTALLER_DIR,
    ROOT_DIR,
    SHARED_DIR,
    TRAY_PYTHON_PIN_FILENAME,
    install_requirements,
    load_json,
    load_run_config,
    read_version,
    run_pip,  # noqa: F401 - used by tests and older importers
    save_run_config,
    setup_logging,
    sha256_of_file,
    write_json_atomic,
)
from obs_config import (  # noqa: E402
    BASIC_INI,
    RECORD_ENCODER_JSON,
    SCENE_COLLECTION_JSON,
    find_ini_section,
    find_script_entry,
    generate_obs_websocket_password,
    load_clip_folder,
    load_obs_websocket_settings,
    load_scene_collection,
    save_scene_collection,
    update_ini_section,
)

# Baseline hashes for the files this distribution ships with, keyed
# relative to App/Shared - see Dev/release/generate_checksums.py (regenerates this
# at release time) and verify_checksums() below (checks the current files
# against it).
CHECKSUMS_MANIFEST_PATH = os.path.join(SHARED_DIR, "checksums.json")

TRAY_REQUIREMENTS = os.path.join(SHARED_DIR, "tray-widget", "requirements.txt")

REPLAY_SOUND_SCRIPT_NAME = "replay_buffer_save_sound.py"
REPLAY_SOUND_WAV_PATH = obs_config.CLIP_SOUND_WAV_PATH



def get_clipem_version():
    return read_version()


# Single source of truth for the upstream OBS/Python versions a release
# pins - Dev/release.sh reads these three lines to build install-manifest.json.
# Setup itself never downloads by these directly: it takes the URL and
# sha256 from the signed manifest (see _fetch_verified below).
# All three platforms run the same OBS as the Linux build
# (Dev/build/build-obs-from-source-for-linux.sh).
MACOS_OBS_VERSION = "32.2.2"
WINDOWS_OBS_VERSION = "32.2.2"
# Windows' Python is also the one OBS runs ClipEm's scripts with, and OBS
# (up to at least 32.x - PY_MINOR_VERSION_MAX in obs-scripting-python-
# import.c) only loads Python 3.6-3.12, so this can't move past 3.12.
# 3.12.10 is the last 3.12 release with Windows binaries.
WINDOWS_PYTHON_VERSION = "3.12.10"


# ---------------------------------------------------------------------------
# OBS must be closed while Setup edits its config: OBS rewrites basic.ini and
# the scene collection when it exits, discarding anything Setup wrote.
# ---------------------------------------------------------------------------

OBS_RUNNING_MESSAGE = (
    "OBS is running. Setup changes OBS's settings, and OBS overwrites them with "
    "its own copy when it closes - so changes made now would be lost."
)


def obs_running():
    return obs_process.obs_running()


def close_running_obs():
    """Stops OBS's outputs (finalizing any recording) and closes it. True
    once no OBS from this install is running."""
    return obs_process.close_obs_stopping_outputs(setup_logging("setup"))


def offer_to_close_obs():
    if not obs_running():
        return
    print(OBS_RUNNING_MESSAGE)
    if not ask_yes_no("Close OBS now?", default=True):
        print("Continuing with OBS open - close it before it next saves, or re-run Setup afterwards.")
        return
    if close_running_obs():
        print("OBS closed.")
    else:
        print("OBS didn't close (it may be showing a dialog) - close it yourself before continuing.",
              file=sys.stderr)


def ask_yes_no(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer.startswith("y")


# ---------------------------------------------------------------------------
# Autostart with PC boot
# ---------------------------------------------------------------------------

def offer_to_clear_other_autostarts():
    """Autostart entries left by another ClipEm folder or an older version
    would start that copy at login too (or fail, if it's gone) - offer to
    remove them."""
    others = autostart.find_other_autostart_entries()
    if not others:
        return
    print("Found ClipEm autostart entries that don't start this copy of ClipEm:")
    for entry in others:
        print(f"  {autostart.describe_autostart_entry(entry)}")
    if ask_yes_no("Remove them?", default=False):
        autostart.remove_autostart_entries(others)


def configure_autostart():
    offer_to_clear_other_autostarts()
    config = load_run_config()
    # The tray widget is required, so it always starts at boot alongside OBS.
    enabled = ask_yes_no(
        "Start ClipEm (OBS and the tray icon) when the PC boots?", default=config["autostart_obs"]
    )
    config["autostart_obs"] = config["autostart_tray"] = enabled
    save_run_config(config)
    apply_boot_autostart(config["autostart_obs"])


# ---------------------------------------------------------------------------
# Clip output folder
# ---------------------------------------------------------------------------

def pick_folder_windows():
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = 'Select folder for OBS recordings / replay buffer clips'; "
        "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.SelectedPath }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True
    )
    return result.stdout.strip() or None


def pick_folder_macos():
    script = 'POSIX path of (choose folder with prompt "Select folder for OBS recordings / replay buffer clips")'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    path = result.stdout.strip()
    return path.rstrip("/") or None


def pick_folder_linux():
    if shutil.which("kdialog"):
        result = subprocess.run(
            ["kdialog", "--getexistingdirectory", os.path.expanduser("~"),
             "--title", "Select folder for OBS recordings / replay buffer clips"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() or None
    if shutil.which("zenity"):
        result = subprocess.run(
            ["zenity", "--file-selection", "--directory",
             "--title=Select folder for OBS recordings / replay buffer clips"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() or None
    try:
        typed = input("Enter full path for recordings/clips folder (blank to skip): ").strip()
    except EOFError:
        typed = ""
    return typed or None


def set_clip_folder(folder):
    """Records the chosen folder in basic.ini. The folder itself isn't
    created here - only once setup actually finishes (see
    ensure_clip_folder_exists / declutter_and_create_shortcut) - so
    picking a folder and then backing out or aborting setup doesn't leave
    a stray directory behind."""
    try:
        obs_config.save_clip_folder(folder)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return
    print(f"Clip output folder set to: {folder}")


def ensure_clip_folder_exists():
    """Actually creates the configured clip folder on disk. Deliberately
    deferred to the very end of setup (declutter_and_create_shortcut)
    rather than done the moment the folder is picked, so an aborted or
    backed-out-of setup doesn't leave a stray directory behind."""
    folder = load_clip_folder()
    os.makedirs(folder, exist_ok=True)


def configure_clip_folder():
    default_clips = os.path.join(ROOT_DIR, "Clips")
    if not ask_yes_no(
        f"Pick a custom folder for recordings/replay-buffer clips? "
        f"(default: {default_clips})",
        default=False,
    ):
        set_clip_folder(default_clips)
        return

    system = platform.system()
    if system == "Windows":
        folder = pick_folder_windows()
    elif system == "Darwin":
        folder = pick_folder_macos()
    else:
        folder = pick_folder_linux()

    if not folder:
        print("No folder selected; using the default.")
        folder = default_clips
    set_clip_folder(folder)


# ---------------------------------------------------------------------------
# Display / resolution / FPS / target clip file size.
#
# File size is controlled by picking a recording video bitrate: for a fixed
# bitrate, output size ~= bitrate * duration. The replay buffer's configured
# length (AdvOut.RecRBTime) is used as the duration estimate - the actual
# saved clip is often shorter (whenever the hotkey is pressed before the
# buffer fills), so real files usually come in at or under the target rather
# than over it.
# ---------------------------------------------------------------------------

FPS_CHOICES = [24, 30, 48, 60]
COMMON_RESOLUTIONS = [
    (3840, 2160), (2560, 1440), (1920, 1080), (1600, 900), (1280, 720), (854, 480),
]
AUDIO_BITRATE_KBPS = 160
KBIT_PER_MB = 8000  # Must match Scripts/clip_reexport.py's.
MIN_VIDEO_BITRATE_KBPS = 500
DEFAULT_TARGET_FILESIZE_MB = obs_config.DISCORD_TARGET_MB  # Discord's free-tier upload limit.
REPLAY_BUFFER_MIN_SECONDS = 15
REPLAY_BUFFER_MAX_SECONDS = 600  # RecRBSize (512MB) caps how much of this is reachable in practice.
REPLAY_BUFFER_STEP_SECONDS = 15
BITRATE_MIN_KBPS = MIN_VIDEO_BITRATE_KBPS
BITRATE_MAX_KBPS = 50000
BITRATE_STEP_KBPS = 500
# The default recording bitrate is REFERENCE_BITRATE_KBPS at 1080p, scaled
# by output pixel count for other resolutions (recommended_recording_bitrate).
REFERENCE_BITRATE_KBPS = 18000
REFERENCE_PIXELS = 1920 * 1080

# Source id -> the settings key OBS uses to store the selected monitor/screen
# index, for the capture source types that could plausibly be in the scene
# collection (only "monitor_capture" ships in the committed scene JSON,
# since it was captured on Windows - ensure_linux_capture_source() below
# swaps it for a working source on Linux at Setup time).
#
# "monitor_capture" is special: on Win8+/D3D11 (i.e. virtually every real
# Windows machine today), OBS registers that source id with its duplicator-
# based plugin, which ignores this "monitor" index entirely and instead
# keys off a "monitor_id" device-interface-path string (see
# _list_displays_windows / save_capture_monitor below). The legacy GDI
# plugin that actually reads "monitor" only gets registered on pre-Win8 or
# non-D3D11 setups, which is why this key is still recorded here (kept
# in sync for that rare case) but can no longer be treated as sufficient
# on its own.
CAPTURE_SOURCE_MONITOR_KEYS = {
    "monitor_capture": "monitor",  # Windows (legacy GDI plugin only - see above)
    "display_capture": "display",  # macOS
    "xshm_input": "screen",  # Linux/X11
}

# C# EnumDisplayDevices P/Invoke shim, run via PowerShell's Add-Type, to
# compute the exact same "monitor_id" device-interface-path string OBS's
# own duplicator-capture plugin resolves internally (see enum_monitor() /
# find_monitor() in obs-studio's plugins/win-capture/duplicator-monitor-
# capture.c) - EnumDisplayDevices(adapterDeviceName, 0, ..., EDD_GET_
# DEVICE_INTERFACE_NAME) called against a Screen's own DeviceName (which
# is the same adapter device name Win32's MONITORINFOEXA.szDevice would
# give OBS) yields the identical DeviceID string OBS matches against.
_MONITOR_DEVICE_ID_CSHARP = r"""
using System;
using System.Runtime.InteropServices;

public struct DISPLAY_DEVICE {
    public int cb;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
    public int StateFlags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
}

public class MonitorInterop {
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern bool EnumDisplayDevices(string lpDevice, uint iDevNum, ref DISPLAY_DEVICE lpDisplayDevice, uint dwFlags);

    [DllImport("user32.dll")]
    public static extern bool SetProcessDpiAwarenessContext(IntPtr value);

    public static string GetDeviceId(string adapterDeviceName) {
        DISPLAY_DEVICE dd = new DISPLAY_DEVICE();
        dd.cb = Marshal.SizeOf(dd);
        if (EnumDisplayDevices(adapterDeviceName, 0, ref dd, 1)) {
            return dd.DeviceID;
        }
        return "";
    }
}
"""


def _list_displays_windows():
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"Add-Type -TypeDefinition @'\n{_MONITOR_DEVICE_ID_CSHARP}\n'@; "
        # PowerShell isn't DPI-aware by default, which makes Screen.Bounds
        # report DPI-virtualized (post-scaling) dimensions instead of true
        # native pixels. Opting into Per-Monitor-V2 awareness here fixes
        # Bounds to reflect actual native resolution.
        "[MonitorInterop]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null; "
        "[System.Windows.Forms.Screen]::AllScreens | ForEach-Object { "
        "$devId = [MonitorInterop]::GetDeviceId($_.DeviceName); "
        '"$($_.DeviceName)|$($_.Bounds.Width)|$($_.Bounds.Height)|$($_.Primary)|$devId" }'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True
    )
    displays = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 5:
            continue
        name, width, height, primary, device_id = parts
        try:
            displays.append({
                "name": name, "width": int(width), "height": int(height),
                "primary": primary == "True",
                # When the device-interface path can't be read (seen in
                # VMs), OBS's find_monitor() falls back to matching
                # monitor_id against the GDI name ("\\.\DISPLAY1") -
                # the Screen.DeviceName we already have.
                "device_id": device_id or name or None,
            })
        except ValueError:
            continue
    return displays


def _list_displays_macos():
    result = subprocess.run(
        ["system_profiler", "SPDisplaysDataType", "-json"], capture_output=True, text=True
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    displays = []
    for gpu in data.get("SPDisplaysDataType", []):
        for disp in gpu.get("spdisplays_ndrvs", []):
            match = re.match(r"(\d+)\s*x\s*(\d+)", disp.get("_spdisplays_resolution", ""))
            if not match:
                continue
            displays.append({
                "name": disp.get("_name", "Display"),
                "width": int(match.group(1)), "height": int(match.group(2)),
                "primary": disp.get("spdisplays_main") == "spdisplays_yes",
            })
    return displays


def _is_wayland_session():
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


def _list_displays_kde_wayland():
    """KDE Plasma (Wayland) via kscreen-doctor's JSON dump. currentModeId
    indexes into "modes" for the true native (unscaled) resolution, and
    "priority" == 1 marks the primary output - both unavailable from
    xrandr, which under Wayland only sees XWayland's single, already-
    scaled virtual output rather than the real per-monitor state."""
    if not shutil.which("kscreen-doctor"):
        return []
    result = subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    displays = []
    for output in data.get("outputs", []):
        if not output.get("connected") or not output.get("enabled"):
            continue
        modes = {m.get("id"): m for m in output.get("modes", [])}
        mode = modes.get(output.get("currentModeId"))
        size = (mode or {}).get("size", {})
        width, height = size.get("width"), size.get("height")
        if not width or not height:
            continue
        displays.append({
            "name": output.get("name", "Display"),
            "width": int(width), "height": int(height),
            "primary": output.get("priority") == 1,
        })
    return displays


def _list_displays_wlroots_wayland():
    """Sway/Hyprland/other wlroots-based compositors via wlr-randr's JSON
    output. The wlr-output-management protocol has no concept of a
    "primary" display, so the output positioned at (0, 0) is treated as
    primary - a common convention, not a guarantee."""
    if not shutil.which("wlr-randr"):
        return []
    result = subprocess.run(["wlr-randr", "--json"], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    displays = []
    for output in data:
        if not output.get("enabled"):
            continue
        mode = next((m for m in output.get("modes", []) if m.get("current")), None)
        if not mode:
            continue
        position = output.get("position", {})
        displays.append({
            "name": output.get("name", "Display"),
            "width": int(mode["width"]), "height": int(mode["height"]),
            "primary": position.get("x") == 0 and position.get("y") == 0,
        })
    return displays


def _list_displays_x11():
    if not shutil.which("xrandr"):
        return []
    result = subprocess.run(["xrandr", "--query"], capture_output=True, text=True)
    displays = []
    for line in result.stdout.splitlines():
        match = re.match(r"^(\S+) connected (primary )?(\d+)x(\d+)\+\d+\+\d+", line)
        if match:
            name, primary, width, height = match.group(1), bool(match.group(2)), match.group(3), match.group(4)
            displays.append({
                "name": name, "width": int(width), "height": int(height), "primary": primary,
            })
    return displays


def _list_displays_linux():
    """Under Wayland, xrandr talks to XWayland's single virtual output
    rather than the compositor's real per-monitor state, which reports the
    wrong (already-scaled) resolution and can't identify the true primary
    display - so compositor-specific tools are tried first there. Neither
    of those covers GNOME/Mutter (no stable CLI for its Wayland randr
    protocol), so that - like any other undetected case - falls through to
    [], and callers fall back to a single synthetic "primary display"
    entry."""
    if _is_wayland_session():
        for detector in (_list_displays_kde_wayland, _list_displays_wlroots_wayland):
            displays = detector()
            if displays:
                return displays
        return []
    return _list_displays_x11()


def list_displays():
    """Returns [{"name", "width", "height", "primary"}, ...] for the
    connected displays, best-effort per platform. Empty list means detection
    isn't available here (e.g. Wayland) - callers should fall back to a
    single synthetic display."""
    system = platform.system()
    try:
        if system == "Windows":
            return _list_displays_windows()
        if system == "Darwin":
            return _list_displays_macos()
        if system == "Linux":
            return _list_displays_linux()
    except Exception:
        pass
    return []


def resolution_choices(native_width, native_height):
    """Native resolution first, then common presets that fit within it,
    de-duplicated."""
    choices = [(native_width, native_height)]
    for width, height in COMMON_RESOLUTIONS:
        if width <= native_width and height <= native_height and (width, height) not in choices:
            choices.append((width, height))
    return choices


def load_video_settings():
    defaults = {"width": 1920, "height": 1080, "fps": 30}
    if not os.path.exists(BASIC_INI):
        return defaults
    with open(BASIC_INI, "r", encoding="utf-8") as f:
        lines = f.readlines()
    start, end = find_ini_section(lines, "Video")
    if start is None:
        return defaults
    values = {}
    for line in lines[start:end]:
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    try:
        return {
            "width": int(values.get("OutputCX", defaults["width"])),
            "height": int(values.get("OutputCY", defaults["height"])),
            "fps": int(values.get("FPSCommon", defaults["fps"])),
        }
    except ValueError:
        return defaults


def save_video_settings(base_width, base_height, output_width, output_height, fps):
    """Base resolution (the canvas) is set to the chosen display's native
    size so capture is pixel-for-pixel; output resolution is what actually
    gets encoded/recorded, and can be downscaled from that."""
    if not os.path.exists(BASIC_INI):
        print(f"Config file not found: {BASIC_INI}", file=sys.stderr)
        return
    update_ini_section(BASIC_INI, "Video", {
        "BaseCX": base_width, "BaseCY": base_height,
        "OutputCX": output_width, "OutputCY": output_height,
        "FPSType": 0, "FPSCommon": fps, "FPSInt": fps, "FPSNum": fps, "FPSDen": 1,
    })
    print(f"Canvas set to {base_width}x{base_height}, output {output_width}x{output_height} @ {fps}fps.")


def load_capture_monitor_index():
    """For "monitor_capture", matches the stored "monitor_id" device path
    against the currently detected displays - that's the key modern
    (Win8+/D3D11) Windows machines actually use. Returns None (rather than
    guessing) when that match fails or there's no saved "monitor_id" yet -
    e.g. a fresh install still carrying the shipped scene collection's
    "monitor_id" from the machine it was captured on, which won't match any
    display here. The plain "monitor" int is deliberately never trusted as
    a fallback for "monitor_capture": modern Windows OBS builds ignore it
    entirely (see CAPTURE_SOURCE_MONITOR_KEYS above), so treating it as
    meaningful would make every fresh install "randomly" land on whatever
    display happens to be index 0 rather than the actual primary. Callers
    should fall back to the primary display when this returns None.

    For "display_capture" (macOS) and "xshm_input" (Linux), there's no
    device-id equivalent, so the plain index is the only signal and is
    trusted directly."""
    data = load_scene_collection()
    if not data:
        return None
    for source in data.get("sources", []):
        settings_key = CAPTURE_SOURCE_MONITOR_KEYS.get(source.get("id"))
        if not settings_key:
            continue
        settings = source.get("settings", {})
        if source.get("id") == "monitor_capture":
            device_id = settings.get("monitor_id")
            if device_id:
                for i, display in enumerate(list_displays()):
                    if display.get("device_id") == device_id:
                        return i
            return None
        try:
            return int(settings.get(settings_key, 0))
        except (TypeError, ValueError):
            return None
    return None


def ensure_windows_capture_monitor():
    """A fresh install's scene collection still carries the "monitor_id" of
    the machine it was captured on, which matches no display here - OBS
    then shows Display Capture as "[Device not connected or not
    available]". Points it at this machine's primary display unless it
    already names a connected one, so it works even if the user never
    touches the display settings."""
    if load_capture_monitor_index() is not None:
        return
    displays = list_displays()
    if not displays:
        print("Could not list displays - leaving Display Capture's monitor as is.", file=sys.stderr)
        return
    idx = next((i for i, d in enumerate(displays) if d.get("primary")), 0)
    display = displays[idx]
    print(f"Pointing Display Capture at {display['name']} ({display['width']}x{display['height']}).")
    save_capture_monitor(idx, display.get("device_id"))


def save_capture_monitor(monitor_index, monitor_device_id=None):
    """Sets the display-capture source's monitor index and, for
    "monitor_capture" on Windows, its "monitor_id" device path too -
    modern (Win8+/D3D11) Windows registers "monitor_capture" as the
    duplicator-based plugin, which ignores the plain index entirely and
    reads "monitor_id" instead (see CAPTURE_SOURCE_MONITOR_KEYS above).

    monitor_device_id should be the chosen display's "device_id" from
    list_displays() (computed by _list_displays_windows() to match
    exactly what OBS's own plugin resolves). When it's None - non-Windows,
    or detection failed - any existing monitor_id is left untouched rather
    than deleted, since deleting it (the previous behavior) left the
    source pointing at no display at all on modern Windows rather than
    falling back to anything sensible."""
    data = load_scene_collection()
    if not data:
        print(f"Scene collection not found: {SCENE_COLLECTION_JSON}", file=sys.stderr)
        return
    updated = False
    missing_device_id = False
    for source in data.get("sources", []):
        settings_key = CAPTURE_SOURCE_MONITOR_KEYS.get(source.get("id"))
        if not settings_key:
            continue
        settings = source.setdefault("settings", {})
        settings[settings_key] = monitor_index
        if source.get("id") == "monitor_capture":
            if monitor_device_id:
                settings["monitor_id"] = monitor_device_id
            else:
                missing_device_id = True
        chosen = (source.get("id"), {k: settings[k] for k in (settings_key, "monitor_id") if k in settings})
        updated = True
    if not updated:
        print("No display-capture source found in the scene collection.", file=sys.stderr)
        return
    save_scene_collection(data)
    # So the tray's capture recovery can point it back here - see
    # obs_config.save_setup_capture_display.
    obs_config.save_setup_capture_display(*chosen)
    print(f"Display capture set to monitor index {monitor_index}.")
    if missing_device_id:
        print(
            "Could not determine this display's Windows device ID - modern "
            "Windows OBS builds (Win8+/D3D11) key display capture off that ID "
            "rather than the index, so this change may not take effect. "
            "Leaving the existing device ID in place; if the wrong display "
            "gets captured, reselect it from OBS's own Sources properties.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Audio devices: which mic/desktop-audio device OBS records from. Device IDs
# live in the scene collection JSON's top-level "DesktopAudioDevice1" (output/
# desktop audio, wasapi_output_capture on Windows, pulse_output_capture on
# Linux) and "AuxAudioDevice1" (input/mic, wasapi_input_capture / pulse_
# input_capture) entries, each just a plain settings.device_id string.
# ---------------------------------------------------------------------------

AUDIO_DEVICE_SCENE_KEYS = {"output": "DesktopAudioDevice1", "input": "AuxAudioDevice1"}

# C# Core Audio (MMDevice) interop shim, run via PowerShell's Add-Type, to
# enumerate active render (desktop audio output) and capture (mic input)
# endpoints and print their exact endpoint-ID strings - the same IDs OBS's
# own win-wasapi plugin resolves via IMMDevice::GetId(), so a device picked
# here is guaranteed to match what OBS would enumerate itself.
_AUDIO_DEVICE_CSHARP = r"""
using System;
using System.Runtime.InteropServices;

public enum EDataFlow { eRender = 0, eCapture = 1, eAll = 2 }
public enum ERole { eConsole = 0, eMultimedia = 1, eCommunications = 2 }

[StructLayout(LayoutKind.Sequential)]
public struct PROPERTYKEY {
    public Guid fmtid;
    public int pid;
}

[StructLayout(LayoutKind.Explicit)]
public struct PROPVARIANT {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
}

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(EDataFlow dataFlow, uint dwStateMask, out IMMDeviceCollection ppDevices);
    int GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice ppEndpoint);
    int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string pwstrId, out IMMDevice ppDevice);
    int RegisterEndpointNotificationCallback(IntPtr pClient);
    int UnregisterEndpointNotificationCallback(IntPtr pClient);
}

[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceCollection {
    int GetCount(out uint pcDevices);
    int Item(uint nDevice, out IMMDevice ppDevice);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    int Activate(ref Guid iid, uint dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    int OpenPropertyStore(uint stgmAccess, out IPropertyStore ppProperties);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
    int GetState(out uint pdwState);
}

[ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore {
    int GetCount(out uint cProps);
    int GetAt(uint iProp, out PROPERTYKEY pkey);
    int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    int SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    int Commit();
}

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumeratorComObject { }

public class AudioDeviceInterop {
    static PROPERTYKEY PKEY_Device_FriendlyName = new PROPERTYKEY {
        fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 14
    };

    public static void PrintAll() {
        var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();
        PrintFlow(enumerator, EDataFlow.eRender, "output");
        PrintFlow(enumerator, EDataFlow.eCapture, "input");
    }

    static void PrintFlow(IMMDeviceEnumerator enumerator, EDataFlow flow, string label) {
        IMMDeviceCollection collection;
        enumerator.EnumAudioEndpoints(flow, 1 /* DEVICE_STATE_ACTIVE */, out collection);
        uint count;
        collection.GetCount(out count);
        for (uint i = 0; i < count; i++) {
            IMMDevice device;
            collection.Item(i, out device);
            string id;
            device.GetId(out id);
            IPropertyStore store;
            device.OpenPropertyStore(0, out store);
            PROPERTYKEY key = PKEY_Device_FriendlyName;
            PROPVARIANT pv;
            store.GetValue(ref key, out pv);
            string name = Marshal.PtrToStringUni(pv.p);
            Console.WriteLine(label + "|" + name + "|" + id);
        }
    }
}
"""


def _list_audio_devices_windows():
    ps = (
        f"Add-Type -TypeDefinition @'\n{_AUDIO_DEVICE_CSHARP}\n'@; "
        "[AudioDeviceInterop]::PrintAll()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True
    )
    outputs, inputs = [], []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        flow, name, device_id = parts
        (outputs if flow == "output" else inputs).append({"name": name, "device_id": device_id})
    return outputs, inputs


def _run_pactl(args):
    result = subprocess.run(["pactl"] + args, capture_output=True, text=True)
    return result.stdout


def _parse_pactl_records(text):
    """pactl list output is one block per device: an unindented header line
    ("Sink #61", "Source #61", ...) followed by indented "Key: value" lines
    until the next unindented header."""
    records = []
    current = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            if current is not None:
                records.append(current)
            current = {}
            continue
        if current is None:
            continue
        stripped = line.strip()
        key, sep, value = stripped.partition(":")
        if sep:
            current[key.strip()] = value.strip()
    if current is not None:
        records.append(current)
    return records


def _list_audio_devices_linux():
    """Mirrors obs-studio's linux-pulseaudio plugin exactly (see
    pulse_input_info/pulse_output_info in plugins/linux-pulseaudio/pulse-
    input.c): desktop-audio device IDs are a sink's monitor *source* name,
    and mic device IDs are any source that isn't itself a sink's monitor."""
    outputs, inputs = [], []
    if not shutil.which("pactl"):
        return outputs, inputs
    for rec in _parse_pactl_records(_run_pactl(["list", "sinks"])):
        monitor_source = rec.get("Monitor Source")
        description = rec.get("Description")
        if description and monitor_source and monitor_source != "n/a":
            outputs.append({"name": description, "device_id": monitor_source})
    for rec in _parse_pactl_records(_run_pactl(["list", "sources"])):
        monitor_of_sink = rec.get("Monitor of Sink")
        name, description = rec.get("Name"), rec.get("Description")
        if name and description and (not monitor_of_sink or monitor_of_sink == "n/a"):
            inputs.append({"name": description, "device_id": name})
    return outputs, inputs


def list_audio_devices():
    """Returns {"output": [...], "input": [...]}, each a list of
    {"name", "device_id"} starting with a synthetic "Default" entry
    (device_id "default", OBS's own default-device sentinel)."""
    system = platform.system()
    outputs, inputs = [], []
    try:
        if system == "Windows":
            outputs, inputs = _list_audio_devices_windows()
        elif system == "Linux":
            outputs, inputs = _list_audio_devices_linux()
    except Exception:
        outputs, inputs = [], []
    default = {"name": "Default", "device_id": "default"}
    return {"output": [default] + outputs, "input": [default] + inputs}


def load_audio_devices():
    """Returns {"output": device_id, "input": device_id} currently saved in
    the scene collection, "default" if unset or not found."""
    data = load_scene_collection()
    result = {"output": "default", "input": "default"}
    if not data:
        return result
    for flow, scene_key in AUDIO_DEVICE_SCENE_KEYS.items():
        entry = data.get(scene_key)
        if entry:
            result[flow] = entry.get("settings", {}).get("device_id", "default") or "default"
    return result


def save_audio_devices(output_device_id, input_device_id):
    """Patches the desktop-audio and mic sources' device_id directly in the
    scene collection JSON, same file/pattern save_capture_monitor uses for
    the display's monitor_id.

    Logs each flow individually (rather than one pass/fail message) since
    "audio sources didn't get created" is most often one of: the scene
    collection JSON missing entirely (fresh/broken OBS profile), or only one
    of DesktopAudioDevice1/AuxAudioDevice1 present because a prior manual
    edit in OBS removed a mixer track - either of which silently no-ops for
    just that one flow otherwise."""
    print(f"Scene collection: {SCENE_COLLECTION_JSON}")
    data = load_scene_collection()
    if not data:
        print("  Scene collection not found or unreadable - can't set audio devices.", file=sys.stderr)
        return
    chosen = {"output": output_device_id, "input": input_device_id}
    updated = False
    for flow, scene_key in AUDIO_DEVICE_SCENE_KEYS.items():
        entry = data.get(scene_key)
        device_id = chosen[flow]
        if entry is None:
            print(
                f"  '{scene_key}' ({flow}) not found in scene collection - "
                f"OBS has no {flow} audio source to point at device '{device_id}'.",
                file=sys.stderr,
            )
            continue
        if not device_id:
            print(f"  Skipping '{scene_key}' ({flow}): no device_id given.", file=sys.stderr)
            continue
        entry.setdefault("settings", {})["device_id"] = device_id
        updated = True
        print(f"  '{scene_key}' ({flow}) -> device_id '{device_id}'.")
    if not updated:
        print("No audio device entries were updated in the scene collection.", file=sys.stderr)
        return
    save_scene_collection(data)
    print("Scene collection JSON written.")


def load_replay_buffer_seconds():
    defaults = 60
    if not os.path.exists(BASIC_INI):
        return defaults
    with open(BASIC_INI, "r", encoding="utf-8") as f:
        lines = f.readlines()
    start, end = find_ini_section(lines, "AdvOut")
    if start is None:
        return defaults
    for line in lines[start:end]:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "RecRBTime":
            try:
                return int(value.strip())
            except ValueError:
                return defaults
    return defaults


def save_replay_buffer_seconds(seconds):
    if not os.path.exists(BASIC_INI):
        print(f"Config file not found: {BASIC_INI}", file=sys.stderr)
        return
    update_ini_section(BASIC_INI, "AdvOut", {"RecRBTime": int(seconds)})
    print(f"Replay buffer length set to {int(seconds)}s.")


def compute_video_bitrate_for_filesize(target_mb, duration_sec, audio_kbps=AUDIO_BITRATE_KBPS):
    """kbps such that `duration_sec` seconds of video+audio lands near
    `target_mb` MB (decimal, as Discord counts them: 1 MB = 8000 kbit).
    Muxing/container overhead is small (~1-2%) and ignored - this is a
    planning estimate, not an exact target."""
    duration_sec = max(1, duration_sec)
    video_kbps = (target_mb * KBIT_PER_MB / duration_sec) - audio_kbps
    return max(MIN_VIDEO_BITRATE_KBPS, int(video_kbps))


def recommended_recording_bitrate(width, height):
    """Default recording bitrate for a width x height output: 18000 kbps at
    1080p, scaled by pixel count, rounded to the bitrate step and clamped
    to the allowed range (e.g. 720p -> 8000, 1440p -> 32000, 4K -> max)."""
    kbps = REFERENCE_BITRATE_KBPS * (width * height) / REFERENCE_PIXELS
    kbps = round(kbps / BITRATE_STEP_KBPS) * BITRATE_STEP_KBPS
    return max(BITRATE_MIN_KBPS, min(BITRATE_MAX_KBPS, int(kbps)))


def bitrate_for_new_resolution(current_kbps, old_resolution, new_resolution):
    """The recording bitrate to offer after the output resolution changes:
    the new resolution's recommendation while current_kbps is still the old
    one's (i.e. the user never picked their own), otherwise current_kbps
    unchanged."""
    if current_kbps == recommended_recording_bitrate(*old_resolution):
        return recommended_recording_bitrate(*new_resolution)
    return current_kbps


def compute_filesize_for_bitrate(video_kbps, duration_sec, audio_kbps=AUDIO_BITRATE_KBPS):
    """Inverse of compute_video_bitrate_for_filesize: estimated MB for
    `duration_sec` seconds of video+audio at a given (directly-chosen)
    video bitrate."""
    duration_sec = max(1, duration_sec)
    return (video_kbps + audio_kbps) * duration_sec / KBIT_PER_MB


def load_recording_bitrate():
    if not os.path.exists(RECORD_ENCODER_JSON):
        return 18000
    try:
        with open(RECORD_ENCODER_JSON, "r", encoding="utf-8") as f:
            return int(json.load(f).get("bitrate", 18000))
    except (OSError, ValueError, TypeError):
        return 18000


def save_recording_bitrate(kbps):
    """Recording (including the replay buffer) runs the obs_x264 encoder in
    Advanced mode, whose bitrate lives in this per-profile JSON rather than
    basic.ini - unlike Simple mode's VBitrate/RecQuality, which this profile
    doesn't use."""
    data = {}
    if os.path.exists(RECORD_ENCODER_JSON):
        try:
            with open(RECORD_ENCODER_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    data["bitrate"] = int(kbps)
    write_json_atomic(RECORD_ENCODER_JSON, data)
    print(f"Recording bitrate set to {kbps} kbps.")


def configure_video_quality():
    displays = list_displays()
    duration = load_replay_buffer_seconds()

    if displays:
        print("Detected displays:")
        for i, d in enumerate(displays):
            tag = " (primary)" if d.get("primary") else ""
            print(f"  {i}: {d['name']} - {d['width']}x{d['height']}{tag}")
        if not ask_yes_no("Change display/resolution/FPS/recording bitrate?", default=False):
            return
        saved_idx = load_capture_monitor_index()
        default_idx = saved_idx if saved_idx is not None else next(
            (i for i, d in enumerate(displays) if d.get("primary")), 0
        )
        raw = input(f"Display index [{default_idx}]: ").strip()
        try:
            idx = int(raw) if raw else default_idx
        except ValueError:
            idx = default_idx
        idx = max(0, min(idx, len(displays) - 1))
        display = displays[idx]
    else:
        if not ask_yes_no("Change resolution/FPS/recording bitrate?", default=False):
            return
        idx = None
        vs = load_video_settings()
        display = {"width": vs["width"], "height": vs["height"]}

    choices = resolution_choices(display["width"], display["height"])
    print("Resolutions:")
    for i, (w, h) in enumerate(choices):
        print(f"  {i}: {w}x{h}")
    raw = input("Resolution index [0]: ").strip()
    try:
        r_idx = int(raw) if raw else 0
    except ValueError:
        r_idx = 0
    r_idx = max(0, min(r_idx, len(choices) - 1))
    out_width, out_height = choices[r_idx]

    raw = input(f"FPS {FPS_CHOICES} [30]: ").strip()
    try:
        fps = int(raw) if raw else 30
    except ValueError:
        fps = 30

    raw = input(
        f"Replay buffer length in seconds - how far back a saved clip can reach "
        f"({REPLAY_BUFFER_MIN_SECONDS}-{REPLAY_BUFFER_MAX_SECONDS}) [{duration}]: "
    ).strip()
    try:
        duration = int(raw) if raw else duration
    except ValueError:
        pass
    duration = max(REPLAY_BUFFER_MIN_SECONDS, min(duration, REPLAY_BUFFER_MAX_SECONDS))

    saved = load_video_settings()
    default_bitrate = bitrate_for_new_resolution(
        load_recording_bitrate(), (saved["width"], saved["height"]), (out_width, out_height)
    )
    raw = input(
        f"Recording bitrate in kbps ({BITRATE_MIN_KBPS}-{BITRATE_MAX_KBPS}; "
        f"{recommended_recording_bitrate(out_width, out_height)} recommended for {out_width}x{out_height}) "
        f"[{default_bitrate}]: "
    ).strip()
    try:
        bitrate = int(raw) if raw else default_bitrate
    except ValueError:
        bitrate = default_bitrate
    bitrate = max(BITRATE_MIN_KBPS, min(bitrate, BITRATE_MAX_KBPS))
    print(
        f"Recording at {bitrate} kbps - a full {duration}s clip is "
        f"~{compute_filesize_for_bitrate(bitrate, duration):.0f}MB."
    )

    save_video_settings(display["width"], display["height"], out_width, out_height, fps)
    if idx is not None:
        save_capture_monitor(idx, display.get("device_id"))
    save_recording_bitrate(bitrate)
    save_replay_buffer_seconds(duration)


def configure_audio_devices():
    devices = list_audio_devices()
    current = load_audio_devices()
    if not ask_yes_no("Change desktop audio / mic device?", default=False):
        return

    def pick(flow_label, choices, current_id):
        print(f"{flow_label} devices:")
        current_idx = next(
            (i for i, d in enumerate(choices) if d["device_id"] == current_id), 0
        )
        for i, d in enumerate(choices):
            print(f"  {i}: {d['name']}")
        raw = input(f"{flow_label} index [{current_idx}]: ").strip()
        try:
            idx = int(raw) if raw else current_idx
        except ValueError:
            idx = current_idx
        idx = max(0, min(idx, len(choices) - 1))
        return choices[idx]["device_id"]

    output_id = pick("Desktop audio", devices["output"], current["output"])
    input_id = pick("Mic", devices["input"], current["input"])
    save_audio_devices(output_id, input_id)


# ---------------------------------------------------------------------------
# Tray widget
# ---------------------------------------------------------------------------

def check_tray_prereqs():
    """Returns (ok, warning_message_or_None).

    The tray icon talks StatusNotifierItem/dbusmenu directly over D-Bus (see
    tray-widget/sni_tray.py) instead of going through PyGObject/
    libayatana-appindicator, so there's nothing to install on Linux either -
    dbus-next is a pure-Python pip dependency like the rest. The one thing
    setup still can't do for the user is start a tray *host*: GNOME Shell
    ships with none at all unless an extension adds one.
    """
    if platform.system() != "Linux":
        return True, None

    try:
        result = subprocess.run(
            [
                "dbus-send", "--session", "--dest=org.freedesktop.DBus",
                "--type=method_call", "--print-reply", "/org/freedesktop/DBus",
                "org.freedesktop.DBus.NameHasOwner",
                "string:org.kde.StatusNotifierWatcher",
            ],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True, None

    if result.returncode != 0 or "boolean false" not in result.stdout:
        return True, None

    return False, (
        "No system tray host found on this session (nothing implements "
        "StatusNotifierWatcher), so the tray icon won't show anywhere.\n"
        "On GNOME, install and enable the \"AppIndicator and KStatusNotifierItem "
        "Support\" extension: https://extensions.gnome.org/extension/615/appindicator-support/\n"
        "KDE Plasma, XFCE, and MATE normally have a tray host built in - if you "
        "still see this there, check your panel includes a system tray widget."
    )


def install_tray_widget():
    system = platform.system()
    pip_args = [sys.executable, "-m", "pip", "install"]
    if system != "Windows":
        # --target instead of --user: keeps dependencies inside the project
        # folder instead of the user's home directory. run.py adds this to
        # PYTHONPATH when starting the tray widget.
        deps_dir = os.path.join(APP_DIR, "macOS" if system == "Darwin" else "Linux", "tray-deps")
        os.makedirs(deps_dir, exist_ok=True)
        pip_args += ["--target", deps_dir]
    print("Installing tray widget dependencies...")
    install_requirements(pip_args, TRAY_REQUIREMENTS)

    if system != "Windows":
        # Pillow's compiled extension in these deps is tied to sys.executable's
        # specific ABI (e.g. cp312) - pin it so run.py keeps using this same
        # interpreter later, even if the system's plain "python3" moves on to
        # a new version in the meantime (a routine distro upgrade, which would
        # otherwise silently break the tray widget - see run.py's
        # _resolve_tray_python()). Windows doesn't need this: it ships its own
        # bundled embeddable Python, already pinned to this distribution.
        with open(os.path.join(deps_dir, TRAY_PYTHON_PIN_FILENAME), "w", encoding="utf-8") as f:
            f.write(os.path.realpath(sys.executable))

    print("Tray widget ready - Run will start it alongside OBS automatically.")


def set_up_tray_widget():
    """The tray widget is required - without a tray host it's installed
    anyway (it shows up once one is running), with a warning."""
    ok, warning = check_tray_prereqs()
    if not ok:
        print(warning)
    install_tray_widget()


# ---------------------------------------------------------------------------
# Clip hotkey (the "Save Replay Buffer" keybinding), stored the same way
# OBS itself persists hotkeys: a JSON blob in one basic.ini key.
# ---------------------------------------------------------------------------

# The format helpers and basic.ini reader/writer live in obs_config (the
# tray widget and its settings dialog use them too).
_hotkey_label_to_obs_key = obs_config.hotkey_label_to_obs_key
_obs_key_to_label = obs_config.obs_key_to_label
_describe_hotkey = obs_config.describe_hotkey
load_clip_hotkey = obs_config.load_clip_hotkey


def save_clip_hotkey(ctrl, alt, shift, key_label):
    try:
        obs_config.save_clip_hotkey(ctrl, alt, shift, key_label)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return
    print(f"Clip hotkey (Save Replay Buffer) set to: {_describe_hotkey(ctrl, alt, shift, key_label)}")


def configure_clip_hotkey():
    ctrl0, alt0, shift0, key0 = load_clip_hotkey()
    print(f"Current clip hotkey: {_describe_hotkey(ctrl0, alt0, shift0, key0)}")
    if not ask_yes_no("Change the clip hotkey?", default=False):
        return
    key_label = input(f"Key (A-Z, 0-9, F1-F24, ~) [{key0}]: ").strip() or key0
    ctrl = ask_yes_no("Ctrl?", default=ctrl0)
    alt = ask_yes_no("Alt?", default=alt0)
    shift = ask_yes_no("Shift?", default=shift0)
    try:
        save_clip_hotkey(ctrl, alt, shift, key_label)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)


# ---------------------------------------------------------------------------
# Clip sound: volume + which .wav plays when a clip is saved. Volume lives
# in the scene collection JSON alongside replay_buffer_save_sound.py's
# other settings; the .wav itself is just a fixed-name file in
# App/Shared/Scripts that portable_launch_helper.py always points the
# script at, so "replacing" it is a plain copy over that fixed name.
# ---------------------------------------------------------------------------

def load_clip_sound_enabled():
    data = load_scene_collection()
    entry = find_script_entry(data, REPLAY_SOUND_SCRIPT_NAME) if data else None
    if not entry:
        return True
    return bool(entry.get("settings", {}).get("sound_enabled", True))


def save_clip_sound_enabled(enabled):
    data = load_scene_collection()
    entry = find_script_entry(data, REPLAY_SOUND_SCRIPT_NAME) if data else None
    if not entry:
        print(
            f"replay_buffer_save_sound.py entry not found in {SCENE_COLLECTION_JSON} - "
            "can't save sound-enabled state.",
            file=sys.stderr,
        )
        return
    entry.setdefault("settings", {})["sound_enabled"] = bool(enabled)
    save_scene_collection(data)
    print(f"Clip sound {'enabled' if enabled else 'disabled'}.")


def load_clip_sound_volume():
    data = load_scene_collection()
    entry = find_script_entry(data, REPLAY_SOUND_SCRIPT_NAME) if data else None
    if not entry:
        return 0.5
    try:
        return float(entry.get("settings", {}).get("audio_volume", 0.5))
    except (TypeError, ValueError):
        return 0.5


def save_clip_sound_volume(volume):
    data = load_scene_collection()
    entry = find_script_entry(data, REPLAY_SOUND_SCRIPT_NAME) if data else None
    if not entry:
        print(
            f"replay_buffer_save_sound.py entry not found in {SCENE_COLLECTION_JSON} - "
            "can't save volume.",
            file=sys.stderr,
        )
        return
    entry.setdefault("settings", {})["audio_volume"] = max(0.0, min(1.0, float(volume)))
    save_scene_collection(data)
    print(f"Clip sound volume set to: {entry['settings']['audio_volume']:.0%}")


def replace_clip_sound_wav(source_path):
    if not source_path or not os.path.exists(source_path):
        print(f"File not found: {source_path}", file=sys.stderr)
        return
    obs_config.replace_clip_sound_wav(source_path)
    print(f"Clip sound file replaced with: {source_path}")


# ---------------------------------------------------------------------------
# OBS websocket (used by the tray widget and any external tools that drive
# OBS remotely) - port/password live in the plugin's own config.json, which
# ships with a fixed baseline value. These let the wizard show and change
# them instead of leaving every install stuck on the shipped default.
# ---------------------------------------------------------------------------

def play_clip_sound(volume, wav_path=None):
    """Best-effort local preview of the clip sound - see obs_config.play_clip_sound."""
    try:
        obs_config.play_clip_sound(volume, wav_path)
    except Exception as exc:
        print(f"Could not play sound: {exc}", file=sys.stderr)


def configure_clip_sound():
    enabled = ask_yes_no("Play a sound when a clip is saved?", default=load_clip_sound_enabled())
    save_clip_sound_enabled(enabled)

    volume = load_clip_sound_volume()
    if ask_yes_no(f"Change the clip sound volume (currently {volume:.0%})?", default=False):
        raw = input(f"Volume 0-100 [{round(volume * 100)}]: ").strip()
        try:
            volume = (int(raw) / 100) if raw else volume
        except ValueError:
            print("Not a number - keeping the current volume.", file=sys.stderr)
        save_clip_sound_volume(volume)

    if ask_yes_no("Replace the clip sound .wav file?", default=False):
        path = input("Path to .wav file: ").strip()
        if path:
            replace_clip_sound_wav(path)


# ---------------------------------------------------------------------------
# Clip re-export: Scripts/clip_reexport.py saves a smaller copy of every clip
# (last N seconds, sized for an upload limit) next to the original, using the
# ffmpeg installed by ensure_ffmpeg() below. Its settings live in the scene
# collection like the other scripts'. The recording itself is unaffected.
# ---------------------------------------------------------------------------

REEXPORT_SCRIPT_NAME = "clip_reexport.py"
DEFAULT_REEXPORT_SETTINGS = {
    "reexport_enabled": False,
    "target_mb": float(DEFAULT_TARGET_FILESIZE_MB),
    "crop_seconds": 30,
}


def load_clip_reexport():
    """{"reexport_enabled", "target_mb", "crop_seconds"}, defaults filled in."""
    data = load_scene_collection()
    entry = find_script_entry(data, REEXPORT_SCRIPT_NAME) if data else None
    settings = dict(DEFAULT_REEXPORT_SETTINGS)
    if entry:
        saved = entry.get("settings", {})
        settings.update({k: saved[k] for k in settings if k in saved})
    return settings


def _reexport_script_entry(data):
    """clip_reexport.py's scripts-tool entry in the scene collection, added
    if it isn't there yet."""
    entry = find_script_entry(data, REEXPORT_SCRIPT_NAME)
    if entry is None:
        entry = {"path": os.path.join(SHARED_DIR, "Scripts", REEXPORT_SCRIPT_NAME), "settings": {}}
        data.setdefault("modules", {}).setdefault("scripts-tool", []).append(entry)
    return entry


def save_clip_reexport(enabled, target_mb, crop_seconds):
    data = load_scene_collection()
    if not data:
        print(f"Scene collection not found: {SCENE_COLLECTION_JSON}", file=sys.stderr)
        return
    entry = _reexport_script_entry(data)
    entry.setdefault("settings", {}).update({
        "reexport_enabled": bool(enabled),
        "target_mb": max(1.0, float(target_mb)),
        "crop_seconds": max(1, int(crop_seconds)),
    })
    save_scene_collection(data)
    if enabled:
        print(f"Clip re-export on: last {int(crop_seconds)}s of each clip, ~{float(target_mb):g} MB copy.")
    else:
        print("Clip re-export off.")


# ---------------------------------------------------------------------------
# Split audio tracks: the replay buffer records 6 audio tracks instead of
# one - see obs_config's APP_SLOT_* notes for the layout. Track 1 stays the
# full mix, so the clip itself is unchanged; Scripts/clip_reexport.py copies
# every track into a "-tracks.mkv" next to it and strips the clip back to
# the mix. The app slots (tracks 4-6) are filled in by the tray widget.
# ---------------------------------------------------------------------------

ALL_TRACKS_MASK = 0b111111
SPLIT_TRACKS_MIXERS = {"DesktopAudioDevice1": 0b101, "AuxAudioDevice1": 0b011}  # mix+desktop, mix+mic
SHIPPED_MIXERS = 255
# Per-platform app capture: Windows' own (Windows 10 2004+), and on Linux
# the obs-pipewire-audio-capture plugin built into ClipEm's OBS (see
# Dev/build/build-obs-from-source-for-linux.sh). macOS gets the mic/desktop
# tracks only.
APP_SLOT_SOURCE_IDS = {
    "Windows": "wasapi_process_output_capture",
    "Linux": "pipewire_audio_application_capture",
}
WINDOWS_APP_CAPTURE_MIN_BUILD = 19041


def app_slot_source_id(system=None):
    system = system or platform.system()
    if system == "Windows":
        try:
            if sys.getwindowsversion().build < WINDOWS_APP_CAPTURE_MIN_BUILD:
                return None
        except AttributeError:
            pass
    return APP_SLOT_SOURCE_IDS.get(system)


def _new_app_slot_source(name, source_id, track, owner):
    return {
        "prev_ver": 520093698,
        "name": name,
        "uuid": str(uuid.uuid4()),
        "id": source_id,
        "versioned_id": source_id,
        "settings": obs_config.app_slot_settings(source_id, owner),
        "mixers": 1 << (track - 1),  # only its own track - never the mix
        "sync": 0,
        "flags": 0,
        "volume": 1.0,
        "balance": 0.5,
        "enabled": True,
        "muted": False,
        "push-to-mute": False,
        "push-to-mute-delay": 0,
        "push-to-talk": False,
        "push-to-talk-delay": 0,
        "hotkeys": {},
        "deinterlace_mode": 0,
        "deinterlace_field_order": 0,
        "monitoring_type": 0,
        "private_settings": {},
    }


def _new_scene_item(source, item_id):
    return {
        "name": source["name"],
        "source_uuid": source["uuid"],
        "visible": True,
        "locked": True,
        "rot": 0.0,
        "align": 5,
        "bounds_type": 0,
        "bounds_align": 0,
        "crop_left": 0,
        "crop_top": 0,
        "crop_right": 0,
        "crop_bottom": 0,
        "id": item_id,
        "group_item_backup": False,
        "pos": {"x": 0.0, "y": 0.0},
        "scale": {"x": 1.0, "y": 1.0},
        "bounds": {"x": 0.0, "y": 0.0},
        "scale_filter": "disable",
        "blend_method": "default",
        "blend_type": "normal",
        "show_transition": {"duration": 0},
        "hide_transition": {"duration": 0},
        "private_settings": {},
    }


def _current_scene(data):
    name = data.get("current_scene")
    for source in data.get("sources", []):
        if source.get("id") == "scene" and source.get("name") == name:
            return source
    return next((s for s in data.get("sources", []) if s.get("id") == "scene"), None)


def _apply_app_slots(data, source_id, state):
    """Makes the scene collection's app slot sources match source_id and
    state (who owns which slot); source_id None removes them."""
    slot_names = set(obs_config.APP_SLOT_SOURCE_NAMES)
    sources = data.setdefault("sources", [])
    existing = {s["name"]: s for s in sources if s.get("name") in slot_names}
    scene = _current_scene(data)
    scene_settings = scene.setdefault("settings", {}) if scene else {}
    items = scene_settings.setdefault("items", []) if scene else []

    if source_id is None:
        data["sources"] = [s for s in sources if s.get("name") not in slot_names]
        for other in data["sources"]:
            other_settings = other.get("settings", {}) if other.get("id") == "scene" else {}
            if "items" in other_settings:
                other_settings["items"] = [i for i in other_settings["items"] if i.get("name") not in slot_names]
        return

    for index, name in enumerate(obs_config.APP_SLOT_SOURCE_NAMES):
        track = obs_config.APP_SLOT_FIRST_TRACK + index
        owner = state["slots"].get(name)
        source = existing.get(name)
        if source is None or source.get("id") != source_id:
            if source is not None:
                sources.remove(source)
            source = _new_app_slot_source(name, source_id, track, owner)
            sources.append(source)
        else:
            source["settings"] = {**source.get("settings", {}), **obs_config.app_slot_settings(source_id, owner)}
            source["mixers"] = 1 << (track - 1)
            source["monitoring_type"] = 0
        if scene is not None:
            item = next((i for i in items if i.get("name") == name), None)
            if item is None:
                next_id = max([scene_settings.get("id_counter", 0)] + [i.get("id", 0) for i in items]) + 1
                scene_settings["id_counter"] = next_id
                items.append(_new_scene_item(source, next_id))
            else:
                item["source_uuid"] = source["uuid"]


def load_split_tracks():
    return obs_config.load_split_tracks()


def save_split_tracks(enabled):
    data = load_scene_collection()
    if not data:
        print(f"Scene collection not found: {SCENE_COLLECTION_JSON}", file=sys.stderr)
        return
    enabled = bool(enabled)
    source_id = app_slot_source_id() if enabled else None
    for scene_key, mixers in SPLIT_TRACKS_MIXERS.items():
        if scene_key in data:
            data[scene_key]["mixers"] = mixers if enabled else SHIPPED_MIXERS
    _apply_app_slots(data, source_id, obs_config.load_app_tracks())
    _reexport_script_entry(data).setdefault("settings", {})["split_tracks_enabled"] = enabled
    save_scene_collection(data)
    if os.path.exists(BASIC_INI):
        update_ini_section(BASIC_INI, "AdvOut", {"RecTracks": ALL_TRACKS_MASK if enabled else 1})
    if not enabled:
        print("Split audio tracks off.")
    elif source_id:
        print("Split audio tracks on: mix, mic, desktop and up to 3 apps on their own tracks.")
    else:
        print("Split audio tracks on: mix, mic and desktop on their own tracks (no per-app capture here).")


def load_app_tracks():
    return obs_config.load_app_tracks()


def set_app_slot_pinned(slot_name, pinned):
    state = obs_config.load_app_tracks()
    owner = state["slots"].get(slot_name)
    if owner:
        owner["pinned"] = bool(pinned)
        obs_config.save_app_tracks(state)


def reset_app_tracks():
    """Forgets every app's track: the next apps heard get the slots afresh."""
    state = obs_config.empty_app_tracks()
    obs_config.save_app_tracks(state)
    data = load_scene_collection()
    if data and any(s.get("name") in obs_config.APP_SLOT_SOURCE_NAMES for s in data.get("sources", [])):
        _apply_app_slots(data, app_slot_source_id(), state)
        save_scene_collection(data)
    print("App tracks reset.")


def ffmpeg_path():
    """The bundled ffmpeg for this platform (None on macOS, which uses PATH)."""
    system = platform.system()
    if system == "Windows":
        return os.path.join(APP_DIR, "Windows", "ffmpeg", "bin", "ffmpeg.exe")
    if system == "Linux":
        return os.path.join(APP_DIR, "Linux", "ffmpeg", "bin", "ffmpeg")
    return None


def ensure_ffmpeg():
    """Installs ClipEm's own ffmpeg build (Dev/build/build-ffmpeg.sh, official
    FFmpeg + x264 source) into App/<platform>/ffmpeg if it isn't there yet.
    Best-effort: only clip re-export needs it, so a failure is reported
    rather than stopping setup."""
    exe = ffmpeg_path()
    if exe is None:
        if not shutil.which("ffmpeg"):
            print("Clip re-export needs ffmpeg on macOS - install it with `brew install ffmpeg`.")
        return
    if os.path.isfile(exe):
        return
    system = platform.system()
    platform_key = "windows" if system == "Windows" else "linux"
    platform_dir = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
    archive = os.path.join(platform_dir, "ffmpeg-download" + (".zip" if system == "Windows" else ".tar.gz"))
    try:
        print("Fetching ffmpeg (used to save smaller copies of clips)...")
        entry = _fetch_verified(platform_key, "ffmpeg", archive)
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                updater.validate_zip(zf, root="ffmpeg")
                zf.extractall(platform_dir)
        else:
            import tarfile

            with tarfile.open(archive) as tf:
                tf.extractall(platform_dir, filter="data")
        os.chmod(exe, 0o755)
        obs_install.record_installed_sha256("ffmpeg", entry)
        record_local_checksums([os.path.dirname(os.path.dirname(exe))], "ffmpeg")
        print("ffmpeg installed.")
    except Exception as exc:
        print(f"Could not install ffmpeg ({exc}) - clip re-export won't work until Setup is re-run.",
              file=sys.stderr)
    finally:
        if os.path.exists(archive):
            os.remove(archive)


def ensure_editor_window():
    """Installs the clip editor's window (editor_window.py: a pinned
    Electron) into App/<platform>/editor-window if it isn't there yet.
    Best-effort: only the clip editor needs it."""
    if editor_window.installed():
        return
    try:
        print("Fetching the clip editor's window (about 130 MB)...")
        entry = editor_window.install(_fetch_verified)
        obs_install.record_installed_sha256(editor_window.SHA256_COMPONENT, entry)
        record_local_checksums([editor_window.runtime_dir()], "editor window")
        print("Clip editor window installed.")
    except Exception as exc:
        print(f"Could not install the clip editor's window ({exc}) - the clip editor won't open until "
              "Setup is re-run.", file=sys.stderr)


def configure_clip_reexport():
    current = load_clip_reexport()
    enabled = ask_yes_no(
        "Also save a smaller copy of each clip (e.g. to fit Discord's upload limit)?",
        default=current["reexport_enabled"],
    )
    target_mb, crop_seconds = current["target_mb"], current["crop_seconds"]
    if enabled:
        raw = input(f"Target size of the copy in MB [{target_mb:g}]: ").strip()
        try:
            target_mb = float(raw) if raw else target_mb
        except ValueError:
            print("Not a number - keeping the current size.", file=sys.stderr)
        raw = input(f"Keep the last how many seconds of the clip [{crop_seconds}]: ").strip()
        try:
            crop_seconds = int(raw) if raw else crop_seconds
        except ValueError:
            print("Not a number - keeping the current length.", file=sys.stderr)
    save_clip_reexport(enabled, target_mb, crop_seconds)


def save_obs_websocket_settings(port, password):
    config = obs_config.save_obs_websocket_settings(port, password)
    print(f"OBS websocket set to port {config['server_port']} with a new password.")


def configure_obs_websocket():
    current = load_obs_websocket_settings()
    print(f"OBS websocket - port {current['port']} (password is set; shown in the tray widget's Settings)")
    if not ask_yes_no("Change the OBS websocket port or password?", default=False):
        return
    raw_port = input(f"Port [{current['port']}]: ").strip()
    try:
        port = int(raw_port) if raw_port else current["port"]
    except ValueError:
        print("Not a number - keeping the current port.", file=sys.stderr)
        port = current["port"]
    password = current["password"]
    if ask_yes_no("Generate a new random password?", default=False):
        password = generate_obs_websocket_password()
    else:
        raw_password = input("New password (blank keeps the current one): ").strip()
        if raw_password:
            password = raw_password
    try:
        save_obs_websocket_settings(port, password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)


# ---------------------------------------------------------------------------
# Checksum integrity check - catches a corrupted/incomplete copy of this
# portable folder (a partial USB or cloud-sync copy, an interrupted git
# checkout, a corrupted download) by comparing the shipped files against a
# baseline manifest. Two manifests feed it:
#   - App/Shared/checksums.json - shipped in every release zip, covers the
#     code/scripts/assets under App/Shared (not OBS-Config or run_config.json,
#     which Setup rewrites per install). Regenerated by
#     Dev/release/generate_checksums.py (maintainer-only tooling, outside this repo).
#   - App/checksums_local.json - gitignored, per-machine. Windows/macOS
#     fetch OBS fresh per install (nothing committed to compare against),
#     so record_local_checksums() hashes it right after a first-time
#     download/extract, giving this machine's own copy a baseline to be
#     checked against on every later run.
# ---------------------------------------------------------------------------

LOCAL_CHECKSUMS_PATH = obs_install.LOCAL_CHECKSUMS_PATH
# Also called by ClipEmSetupLinux.sh after it fetches OBS.
record_local_checksums = obs_install.record_local_checksums
record_installed_sha256 = obs_install.record_installed_sha256


def _load_checksum_manifest(path):
    return load_json(path, {}) or {}


def verify_checksums():
    """Compares the shipped files (shared config/scripts/tray-widget, the
    committed Linux OBS build, and - once recorded - the fetched
    Windows/macOS OBS build) against the checksum manifests above. Returns
    (ok, message); ok is None if there's nothing to check against yet
    (e.g. a dev checkout that hasn't generated a manifest) rather than
    treating that as a failure."""
    # The shipped manifest is keyed relative to App/Shared, the local one
    # (record_local_checksums) relative to App/ - resolve each against its
    # own base rather than merging the two key spaces.
    manifest = {}
    for base_dir, path in ((SHARED_DIR, CHECKSUMS_MANIFEST_PATH), (APP_DIR, LOCAL_CHECKSUMS_PATH)):
        for rel_path, expected_hash in _load_checksum_manifest(path).items():
            if base_dir == SHARED_DIR and rel_path in updater.PRESERVED_RELATIVE_PATHS:
                continue  # user-replaceable (e.g. a custom clip sound) - not corruption
            manifest[os.path.relpath(os.path.join(base_dir, rel_path), APP_DIR).replace(os.sep, "/")] = expected_hash
    if not manifest:
        return None, "No checksum manifest found (App/Shared/checksums.json) - skipping integrity check."

    mismatched = []
    missing = []
    for rel_path, expected_hash in manifest.items():
        full_path = os.path.join(APP_DIR, rel_path)
        if not os.path.exists(full_path):
            missing.append(rel_path)
        elif sha256_of_file(full_path) != expected_hash:
            mismatched.append(rel_path)

    if not mismatched and not missing:
        return True, f"Checksum check passed - {len(manifest)} files verified."

    lines = [
        f"Checksum check found problems: {len(mismatched)} changed, "
        f"{len(missing)} missing (of {len(manifest)} files checked)."
    ]
    for rel_path in mismatched[:10]:
        lines.append(f"  changed:  {rel_path}")
    for rel_path in missing[:10]:
        lines.append(f"  missing:  {rel_path}")
    if len(mismatched) + len(missing) > 20:
        lines.append("  ...")
    lines.append(
        "This usually means this copy of the folder is incomplete or corrupted "
        "(a partial USB/cloud-sync copy, or an interrupted git checkout) - "
        "consider re-copying/re-cloning this distribution."
    )
    return False, "\n".join(lines)


def report_checksums():
    """Runs verify_checksums() and prints its result - shared by both the
    GUI wizard's provisioning log and the text-based fallback, so the
    message shows up during Setup either way."""
    ok, message = verify_checksums()
    print(f"\n{message}" if ok else message, file=sys.stderr if ok is False else sys.stdout)


# ---------------------------------------------------------------------------
# Per-platform OBS provisioning
# ---------------------------------------------------------------------------

_fetch_verified = obs_install.fetch_verified


def setup_windows():
    """ClipEm's OBS in App/Windows, downloaded if it's missing. Raises if it
    can't be installed."""
    windows_dir = obs_install.platform_dir("Windows")
    if os.path.exists(obs_install.obs_exe("Windows")):
        print("OBS binaries already present.")
        # Installs made before debug symbols were skipped still have them.
        obs_install.remove_debug_files(windows_dir)
        obs_install.warn_if_windows_vc_runtime_missing(windows_dir)
        return
    obs_install.install_obs("Windows")


def ensure_windows_pip():
    """The embeddable Python distribution ships without pip and with
    site-packages disabled by default - needed for the tray widget's
    `pip install`. Bootstrap both if a fresh interpreter was just fetched."""
    check = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if check.returncode == 0:
        return
    windows_python.enable_site(os.path.dirname(sys.executable))
    print("Bootstrapping pip for the embedded Python...")
    windows_python.bootstrap_pip(sys.executable)


own_pth_path = windows_python.own_pth_path


def _tcltk_log(message):
    print(f"  [tkinter] {message}")


def ensure_windows_tkinter():
    """The embeddable Windows Python deliberately ships without tkinter (no
    tcl/tk runtime, no _tkinter.pyd - see the python.org docs on the
    embeddable distribution) so the GUI setup wizard can't run on it as-is.
    Pulls just those pieces out of python.org's tcltk.msi for the same
    version (windows_python.install_tcltk). Idempotent - skipped once the
    pieces are in place, which they already are for installs made with
    ClipEmSetupWindows.exe (it bundles them - Dev/installer/windows/
    extract_tcltk.py), so this is the fallback for from-source checkouts
    (Setup_Windows.bat) and older installs.

    Every step is logged with a [tkinter] prefix; on failure, setup falls
    back to the text-based wizard.
    """
    python_root = find_windows_python_root()
    if not python_root:
        _tcltk_log("no embedded Python found - skipping GUI setup")
        return False

    lib_dir = os.path.join(python_root, "Lib")
    if windows_python.tcltk_installed(python_root):
        windows_python.add_lib_to_pth(python_root)
        if lib_dir not in sys.path:
            sys.path.append(lib_dir)
        return True

    # The manifest's tcltk.msi is built for WINDOWS_PYTHON_VERSION - its
    # _tkinter.pyd won't load into any other Python (e.g. an install whose
    # Python hasn't been updated yet).
    wanted = tuple(int(part) for part in WINDOWS_PYTHON_VERSION.split(".")[:2])
    if sys.version_info[:2] != wanted:
        _tcltk_log(
            f"this Python is {sys.version_info[0]}.{sys.version_info[1]}, the published tcl/tk is for "
            f"{WINDOWS_PYTHON_VERSION} - skipping GUI setup (update ClipEm to upgrade Python)"
        )
        return False

    print("Fetching tcl/tk so the graphical setup wizard can run (one-time, ~3.5MB)...")
    try:
        if not windows_python.install_tcltk(python_root):
            return False
    except Exception as exc:
        _tcltk_log(f"failed: {type(exc).__name__}: {exc}")
        print(f"Could not set up tkinter ({exc}) - falling back to the text-based setup.", file=sys.stderr)
        return False
    # The ._pth change only applies to future runs - this interpreter
    # already built sys.path from it, so add Lib here too.
    windows_python.add_lib_to_pth(python_root)
    if lib_dir not in sys.path:
        sys.path.append(lib_dir)
    ok = windows_python.verify_tkinter(sys.executable, python_root)
    print("tcl/tk installed." if ok else "tkinter bootstrap incomplete - GUI setup unavailable.")
    return ok


def configure_tcltk_env(python_root):
    """No registry entries exist for a portable Python, so Tcl/Tk can't
    find their script libraries on their own - point them at the copies
    ensure_windows_tkinter placed alongside python.exe."""
    tcl_lib = os.path.join(python_root, "tcl", "tcl8.6")
    tk_lib = os.path.join(python_root, "tcl", "tk8.6")
    if os.path.isdir(tcl_lib):
        os.environ["TCL_LIBRARY"] = tcl_lib
    if os.path.isdir(tk_lib):
        os.environ["TK_LIBRARY"] = tk_lib


def setup_macos():
    obs_app = os.path.join(APP_DIR, "macOS", "OBS.app")
    if os.path.isdir(obs_app):
        print("OBS.app already present.")
        return

    artifact = "obs_arm64" if platform.machine() == "arm64" else "obs_x86_64"
    dmg_path = os.path.join(APP_DIR, "macOS", "obs-download.dmg")
    os.makedirs(os.path.dirname(dmg_path), exist_ok=True)
    _fetch_verified("macos", artifact, dmg_path)

    mount_point = subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
    print("Mounting disk image...")
    subprocess.run(["hdiutil", "attach", dmg_path, "-nobrowse", "-mountpoint", mount_point], check=True)

    print("Copying OBS.app...")
    subprocess.run(["cp", "-R", os.path.join(mount_point, "OBS.app"), obs_app], check=True)

    subprocess.run(["hdiutil", "detach", mount_point, "-quiet"], check=True)
    os.remove(dmg_path)

    print("Clearing quarantine attribute so Gatekeeper doesn't block direct launches...")
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", obs_app])

    record_local_checksums([obs_app], "macOS OBS.app")
    print("OBS.app installed.")


def setup_linux():
    """Makes sure ClipEm's own OBS build is in App/Linux/obs-install,
    downloading it if it's missing. ClipEm never uses a system-wide OBS.
    Returns True if OBS is present and setup should continue, False to stop."""
    exe = obs_install.obs_exe("Linux")
    if os.path.exists(exe):
        print(f"OBS already installed ({exe}).")
        return True
    try:
        obs_install.install_obs("Linux")
    except obs_install.ObsInstallError as exc:
        print(f"Could not install ClipEm's OBS ({exc}). Check your connection and run "
              "ClipEmSetupLinux.sh again.", file=sys.stderr)
        return False
    return True


# The committed scene JSON's "Display Capture" source is "monitor_capture" -
# a plugin that only exists on Windows (see CAPTURE_SOURCE_MONITOR_KEYS). On
# Linux the equivalent working source is OBS's PipeWire portal capture
# (plugins/linux-pipewire, built into the from-source OBS build fetched at
# install time), whose id "pipewire-screen-capture-source" is what
# OBS's UI labels "Screen Capture (PipeWire)" - see screencast-portal.c's
# screencast_portal_capture_info (the unified, non-OBSOLETE variant; the
# separate desktop/window-only ids are legacy).
LINUX_CAPTURE_SOURCE_ID = "pipewire-screen-capture-source"
LINUX_CAPTURE_SOURCE_NAME = "Screen Capture (PipeWire)"


def ensure_linux_capture_source():
    """Swaps the shipped monitor_capture "Display Capture" source for
    PipeWire's portal-based screen capture, in place (same uuid, so the
    scene item referencing it - and its position/scale/bounds - survive
    untouched). Idempotent: a no-op once already swapped, and a no-op if
    the shipped source isn't there to swap (e.g. re-run after a user has
    since replaced it themselves in OBS).

    Unlike monitor_capture, this source has no monitor index/id to
    preconfigure here: xdg-desktop-portal's ScreenCast portal always
    prompts the user once, interactively, to pick a monitor or window -
    that's a portal security boundary, not something an application (or
    this setup script) can bypass - and OBS remembers the choice itself
    via a RestoreToken it fills in after that first grant. So
    save_capture_monitor()/configure_video_quality() correctly leave this
    source alone (it's not in CAPTURE_SOURCE_MONITOR_KEYS): there's
    nothing left for Setup to configure once this swap is done.
    """
    data = load_scene_collection()
    if not data:
        return

    sources = data.get("sources", [])
    if any(s.get("id") == LINUX_CAPTURE_SOURCE_ID for s in sources):
        return  # already migrated

    swapped_uuid = None
    for source in sources:
        if source.get("id") != "monitor_capture":
            continue
        source["id"] = LINUX_CAPTURE_SOURCE_ID
        source["versioned_id"] = LINUX_CAPTURE_SOURCE_ID
        source["name"] = LINUX_CAPTURE_SOURCE_NAME
        source["settings"] = {"ShowCursor": True}
        swapped_uuid = source.get("uuid")

    if not swapped_uuid:
        return

    # Scene items cache the source's name for display - keep it in sync so
    # the Sources list doesn't keep showing the old label.
    for source in sources:
        if source.get("id") != "scene":
            continue
        for item in source.get("settings", {}).get("items", []):
            if item.get("source_uuid") == swapped_uuid:
                item["name"] = LINUX_CAPTURE_SOURCE_NAME

    save_scene_collection(data)
    print(
        f"Display capture switched to {LINUX_CAPTURE_SOURCE_NAME} - OBS will "
        "prompt you once to pick a monitor/window the first time it runs."
    )


# Same Windows-vs-Linux source-id problem as monitor_capture above, but for
# the Desktop Audio / Mic tracks: the shipped scene collection was captured
# on Windows, so DesktopAudioDevice1/AuxAudioDevice1 carry the WASAPI plugin
# ids. OBS's Linux build has no wasapi plugin at all, so it can't instantiate
# either source - it just silently drops both, leaving the Audio Mixer dock
# empty no matter what device_id save_audio_devices() writes into them.
LINUX_AUDIO_SOURCE_IDS = {
    "DesktopAudioDevice1": ("wasapi_output_capture", "pulse_output_capture"),
    "AuxAudioDevice1": ("wasapi_input_capture", "pulse_input_capture"),
}


def ensure_linux_audio_source_ids():
    """Swaps the shipped Desktop Audio/Mic entries' WASAPI source ids for
    PulseAudio's equivalents, in place. The existing settings.device_id
    ("default", or a real device_id from a previous save_audio_devices()
    call) is left as-is - "default" is valid for both plugins, and a
    Windows-specific device_id string just won't match anything on Linux
    until the user reselects a device. Idempotent: a no-op once already
    swapped, or if the entry isn't there."""
    data = load_scene_collection()
    if not data:
        return
    changed = False
    for scene_key, (windows_id, linux_id) in LINUX_AUDIO_SOURCE_IDS.items():
        entry = data.get(scene_key)
        if not entry or entry.get("id") != windows_id:
            continue
        entry["id"] = linux_id
        entry["versioned_id"] = linux_id
        changed = True
    if not changed:
        return
    save_scene_collection(data)
    print("Desktop Audio/Mic source types switched to PulseAudio for Linux.")


# ---------------------------------------------------------------------------
# Post-setup: relocate Setup*/Run* scripts out of the root, install the
# Uninstall* scripts into the root (opposite direction - they start out
# tucked away and only appear once there's actually something to
# uninstall), add a native shortcut that runs run.py directly.
# ---------------------------------------------------------------------------

SETUP_SCRIPT_NAMES = ["Setup_Windows.bat", "SetupMac.command", "Setup_Linux.sh"]
STALE_RUN_SCRIPT_NAMES = ["RunWindows.bat", "RunMac.command", "RunLinux.sh"]
UNINSTALL_SCRIPT_NAMES = {"Windows": "UninstallAutostartWindows.bat", "Linux": "UninstallAutostartLinux.sh"}
UNINSTALL_SCRIPTS_SRC_DIR = os.path.join(APP_DIR, "Setup-Uninstall_Scripts")


def relocate_setup_scripts():
    setup_files_dir = UNINSTALL_SCRIPTS_SRC_DIR
    os.makedirs(setup_files_dir, exist_ok=True)

    for name in SETUP_SCRIPT_NAMES:
        src = os.path.join(ROOT_DIR, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(setup_files_dir, name))

    # Superseded by the native shortcut below - it invokes run.py directly.
    for name in STALE_RUN_SCRIPT_NAMES:
        stale = os.path.join(setup_files_dir, name)
        if os.path.exists(stale):
            os.remove(stale)


def install_uninstall_scripts():
    """Copies (not moves - App/Setup-Uninstall_Scripts/ stays as the
    template) the Uninstall script for *this* platform into the root, but
    only once setup has actually completed. It lives tucked away under
    App/ until then so there's nothing named "Uninstall..." sitting next
    to Setup* scripts before there's anything installed to uninstall -
    and only the current platform's copy ever lands in the root, not the
    other OS's."""
    name = UNINSTALL_SCRIPT_NAMES.get(platform.system())
    if not name:
        return
    src = os.path.join(UNINSTALL_SCRIPTS_SRC_DIR, name)
    dest = os.path.join(ROOT_DIR, name)
    if os.path.exists(src) and not os.path.exists(dest):
        shutil.copy2(src, dest)
        if name.endswith(".sh"):
            os.chmod(dest, 0o755)


def declutter_and_create_shortcut():
    ensure_clip_folder_exists()
    relocate_setup_scripts()
    install_uninstall_scripts()
    autostart.create_shortcut()


def launch_app():
    """Starts run.py in the background - the same interpreter/working
    directory the created shortcut uses. Backs the finish page's
    "Automatically run OBS after closing this" option, so the user doesn't
    have to go find and click the shortcut Setup just created."""
    run_py = os.path.join(INSTALLER_DIR, "run.py")
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, run_py], cwd=ROOT_DIR, **kwargs)


# ---------------------------------------------------------------------------

def run_provisioning():
    """The GUI wizard's provisioning step - same per-platform OBS/Python
    work as main_cli, just wrapped so its print() output can be captured
    into the wizard's log box instead of a console. Returns False only
    when setup should stop early (Linux with no OBS available yet)."""
    system = platform.system()
    if system == "Windows":
        setup_windows()
        ensure_windows_capture_monitor()
        ensure_ffmpeg()
        ensure_editor_window()
        report_checksums()
        return True
    if system == "Darwin":
        setup_macos()
        ensure_ffmpeg()
        ensure_editor_window()
        report_checksums()
        return True
    if system == "Linux":
        ok = setup_linux()
        if ok:
            ensure_linux_capture_source()
            ensure_linux_audio_source_ids()
            ensure_ffmpeg()
            ensure_editor_window()
            report_checksums()
        return ok
    return True


def try_launch_gui(system):
    """Attempts the graphical wizard; returns False (falling back to the
    text-based flow in main_cli) if tkinter isn't usable on this machine."""
    if system == "Windows":
        python_root = find_windows_python_root()
        if not python_root or not ensure_windows_tkinter():
            return False
        configure_tcltk_env(python_root)

    try:
        import gui
    except Exception as exc:
        print(f"Graphical setup unavailable ({exc}); using the text-based setup instead.")
        if system == "Linux" and isinstance(exc, ImportError):
            print(
                "  Install tkinter's system package to enable it next time, e.g.:\n"
                "    Arch/CachyOS:   sudo pacman -S tk\n"
                "    Debian/Ubuntu:  sudo apt install python3-tk\n"
                "    Fedora:         sudo dnf install python3-tkinter"
            )
        return False

    controller = {
        "system": system,
        "obs_running": obs_running,
        "close_obs": close_running_obs,
        "obs_running_message": OBS_RUNNING_MESSAGE,
        "version": get_clipem_version(),
        "default_clip_folder": os.path.join(ROOT_DIR, "Clips"),
        "provision": run_provisioning,
        "check_tray_prereqs": check_tray_prereqs,
        "set_clip_folder": set_clip_folder,
        "list_displays": list_displays,
        "resolution_choices": resolution_choices,
        "load_video_settings": load_video_settings,
        "save_video_settings": save_video_settings,
        "load_capture_monitor_index": load_capture_monitor_index,
        "save_capture_monitor": save_capture_monitor,
        "list_audio_devices": list_audio_devices,
        "load_audio_devices": load_audio_devices,
        "save_audio_devices": save_audio_devices,
        "ensure_mic_filter_deps": mic_filter.ensure_mic_filter_deps,
        "measure_audio_level_db": mic_filter.measure_audio_level_db,
        "compute_mic_filter_values": mic_filter.compute_mic_filter_values,
        "has_mic_filter_been_configured": lambda: mic_filter.has_mic_filter_been_configured(
            SCENE_COLLECTION_JSON, AUDIO_DEVICE_SCENE_KEYS["input"]
        ),
        "save_mic_filter_levels": lambda noise_floor_db, speech_level_db: mic_filter.save_mic_filter_levels(
            SCENE_COLLECTION_JSON, AUDIO_DEVICE_SCENE_KEYS["input"], noise_floor_db, speech_level_db
        ),
        "noise_floor_sample_seconds": mic_filter.NOISE_FLOOR_SAMPLE_SECONDS,
        "speech_sample_seconds": mic_filter.SPEECH_SAMPLE_SECONDS,
        "load_replay_buffer_seconds": load_replay_buffer_seconds,
        "save_replay_buffer_seconds": save_replay_buffer_seconds,
        "load_recording_bitrate": load_recording_bitrate,
        "save_recording_bitrate": save_recording_bitrate,
        "compute_video_bitrate_for_filesize": compute_video_bitrate_for_filesize,
        "compute_filesize_for_bitrate": compute_filesize_for_bitrate,
        "bitrate_for_new_resolution": bitrate_for_new_resolution,
        "default_target_filesize_mb": DEFAULT_TARGET_FILESIZE_MB,
        "fps_choices": FPS_CHOICES,
        "replay_buffer_min_seconds": REPLAY_BUFFER_MIN_SECONDS,
        "replay_buffer_max_seconds": REPLAY_BUFFER_MAX_SECONDS,
        "replay_buffer_step_seconds": REPLAY_BUFFER_STEP_SECONDS,
        "bitrate_min_kbps": BITRATE_MIN_KBPS,
        "bitrate_max_kbps": BITRATE_MAX_KBPS,
        "bitrate_step_kbps": BITRATE_STEP_KBPS,
        "install_tray": install_tray_widget,
        "load_clip_hotkey": load_clip_hotkey,
        "save_clip_hotkey": save_clip_hotkey,
        "load_clip_sound_enabled": load_clip_sound_enabled,
        "save_clip_sound_enabled": save_clip_sound_enabled,
        "load_clip_sound_volume": load_clip_sound_volume,
        "save_clip_sound_volume": save_clip_sound_volume,
        "replace_clip_sound_wav": replace_clip_sound_wav,
        "play_clip_sound": play_clip_sound,
        "load_clip_reexport": load_clip_reexport,
        "save_clip_reexport": save_clip_reexport,
        "load_split_tracks": load_split_tracks,
        "save_split_tracks": save_split_tracks,
        "load_app_tracks": load_app_tracks,
        "set_app_slot_pinned": set_app_slot_pinned,
        "reset_app_tracks": reset_app_tracks,
        "app_slot_source_id": app_slot_source_id,
        "load_obs_websocket_settings": load_obs_websocket_settings,
        "save_obs_websocket_settings": save_obs_websocket_settings,
        "generate_obs_websocket_password": generate_obs_websocket_password,
        "load_run_config": load_run_config,
        "save_run_config": save_run_config,
        "apply_boot_autostart": apply_boot_autostart,
        "find_other_autostart_entries": autostart.find_other_autostart_entries,
        "describe_autostart_entry": autostart.describe_autostart_entry,
        "remove_autostart_entries": autostart.remove_autostart_entries,
        "load_update_config": updater.load_update_config,
        "change_update_config": updater.change_update_config,
        "finalize": declutter_and_create_shortcut,
        "launch_app": launch_app,
    }
    gui.run_wizard(controller)
    return True


def main_cli(system):
    offer_to_close_obs()
    if system == "Windows":
        setup_windows()
        ensure_windows_pip()
        ensure_windows_capture_monitor()
    elif system == "Darwin":
        setup_macos()
    elif system == "Linux":
        if not setup_linux():
            return
        ensure_linux_capture_source()
        ensure_linux_audio_source_ids()

    ensure_ffmpeg()
    ensure_editor_window()
    report_checksums()
    configure_clip_folder()
    configure_video_quality()
    configure_audio_devices()
    configure_clip_hotkey()
    configure_clip_sound()
    configure_clip_reexport()
    configure_obs_websocket()
    set_up_tray_widget()
    configure_autostart()
    declutter_and_create_shortcut()

    print("\nSetup complete. Use the shortcut in this folder to start OBS.")


def main():
    system = platform.system()
    print(f"=== ClipEm setup ({system}) ===")

    if system not in ("Windows", "Darwin", "Linux"):
        sys.exit(f"Unsupported platform: {system}")

    if system == "Windows":
        ensure_windows_pip()

    if try_launch_gui(system):
        return
    main_cli(system)


if __name__ == "__main__":
    main()
