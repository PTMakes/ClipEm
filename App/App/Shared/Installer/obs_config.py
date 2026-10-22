"""
Reads and writes the OBS config files ClipEm ships in App/Shared/OBS-Config:
the profile's basic.ini, the scene collection JSON, and obs-websocket's
config.json. Shared by setup.py, run.py, the tray widget and its settings
dialog so each file has one reader/writer. Every write is atomic (see
clipem_common.write_text_atomic).
"""

import json
import os
import platform
import re
import secrets
import shutil
import string
import sys

import clipem_common
from clipem_common import ROOT_DIR, SHARED_DIR, load_json, write_json_atomic, write_text_atomic

OBS_CONFIG_DIR = os.path.join(SHARED_DIR, "OBS-Config")
OBS_LOG_DIR = os.path.join(OBS_CONFIG_DIR, "logs")  # OBS runs with this as its config dir
BASIC_INI = os.path.join(OBS_CONFIG_DIR, "basic", "profiles", "DefaultPTM", "basic.ini")
RECORD_ENCODER_JSON = os.path.join(OBS_CONFIG_DIR, "basic", "profiles", "DefaultPTM", "recordEncoder.json")
SCENE_COLLECTION_JSON = os.path.join(OBS_CONFIG_DIR, "basic", "scenes", "DefaultPTM.json")
OBS_WEBSOCKET_CONFIG_PATH = os.path.join(OBS_CONFIG_DIR, "plugin_config", "obs-websocket", "config.json")

# The password OBS_WEBSOCKET_CONFIG_PATH ships with in the shared zip -
# every fresh install starts from this same file, so it's effectively
# public. ensure_obs_websocket_password() replaces it before OBS is ever
# started (run.py) or the settings are shown (setup/tray/settings dialog).
SHIPPED_OBS_WEBSOCKET_PASSWORD = "7zEHURe5Wly0CEtUB9A2"

DEFAULT_CLIPS_DIR = os.path.join(ROOT_DIR, "Clips")
# See portable_launch_helper.LEAKED_CLIPS_DIRS - read as "not set yet".
LEAKED_CLIPS_DIRS = ("/home/pt/Development/PTMClipEm/ClipEm/Clips",)


# ---------------------------------------------------------------------------
# basic.ini
# ---------------------------------------------------------------------------

def find_ini_section(lines, section_name):
    """Returns (start, end) line indices of an existing `[section]` block
    (end exclusive), or (None, None) if it isn't in the file yet."""
    header = f"[{section_name}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return None, None
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^\[.+\]\s*$", lines[i].strip())),
        len(lines),
    )
    return start, end


# path=None means BASIC_INI, looked up at call time (not bound as a default
# argument) so tests and callers can repoint BASIC_INI.
def read_ini_lines(path=None):
    with open(path or BASIC_INI, "r", encoding="utf-8") as f:
        return f.readlines()


def write_ini_lines(lines, path=None):
    write_text_atomic(path or BASIC_INI, "".join(lines))


def read_ini_section(section, path=None):
    """{key: value} for `[section]`, or {} if the file/section is missing."""
    path = path or BASIC_INI
    if not os.path.exists(path):
        return {}
    lines = read_ini_lines(path)
    start, end = find_ini_section(lines, section)
    if start is None:
        return {}
    values = {}
    for line in lines[start + 1:end]:
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def update_ini_section(path, section, updates):
    """Sets each key in `updates` within `[section]`, creating the section
    (or missing keys within it) if needed. Values are stringified."""
    lines = read_ini_lines(path)
    remaining = {k: str(v) for k, v in updates.items()}
    start, end = find_ini_section(lines, section)
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", f"[{section}]\n"] + [f"{k}={v}\n" for k, v in remaining.items()]
    else:
        for i in range(start + 1, end):
            key = lines[i].split("=", 1)[0].strip()
            if key in remaining:
                lines[i] = f"{key}={remaining.pop(key)}\n"
        if remaining:
            lines[end:end] = [f"{k}={v}\n" for k, v in remaining.items()]
    write_ini_lines(lines, path)


def load_clip_folder():
    """The folder clips are saved to (basic.ini's FilePath), or the default
    Clips folder next to App/ if it isn't set yet."""
    if not os.path.exists(BASIC_INI):
        return DEFAULT_CLIPS_DIR
    with open(BASIC_INI, "r", encoding="utf-8") as f:
        for line in f:
            key, sep, value = line.partition("=")
            value = value.strip()
            if (sep and key.strip() == "FilePath" and value and "{{" not in value
                    and value not in LEAKED_CLIPS_DIRS):
                return value.replace("\\\\", "\\")
    return DEFAULT_CLIPS_DIR


CLIP_FOLDER_INI_KEYS = ("FilePath", "RecFilePath", "FFFilePath")


def save_clip_folder(folder):
    """Points every basic.ini output path (simple, advanced and ffmpeg
    output) at folder. Doesn't create the folder itself."""
    if not os.path.exists(BASIC_INI):
        raise FileNotFoundError(f"Config file not found: {BASIC_INI}")
    ini_value = folder.replace("\\", "\\\\") if os.name == "nt" else folder
    lines = read_ini_lines()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0]
        if key in CLIP_FOLDER_INI_KEYS:
            lines[i] = f"{key}={ini_value}\n"
    write_ini_lines(lines)


# ---------------------------------------------------------------------------
# Scene collection
# ---------------------------------------------------------------------------

def load_scene_collection():
    """Parsed scene collection, or None if it's missing. A corrupt file
    raises, rather than being silently treated as empty and overwritten."""
    if not os.path.exists(SCENE_COLLECTION_JSON):
        return None
    return load_json_strict(SCENE_COLLECTION_JSON)


def load_json_strict(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_scene_collection(data):
    write_json_atomic(SCENE_COLLECTION_JSON, data)


def find_script_entry(data, script_name):
    for entry in (data or {}).get("modules", {}).get("scripts-tool", []):
        if os.path.basename(entry.get("path", "")) == script_name:
            return entry
    return None


# Discord's free upload limit, in decimal MB (it was 10 MB until ClipEm
# 1.3.7). Must match Scripts/clip_reexport.py's DEFAULT_TARGET_MB.
DISCORD_TARGET_MB = 20
OLD_DISCORD_TARGET_MB = 10
DISCORD_MIGRATED_KEY = "clipem_discord_20mb"


def migrate_discord_target_size():
    """Moves a smaller-copy target still at the old Discord limit (10 MB) to
    the new one, once - a 10 set again afterwards is the user's choice.
    Returns True if it changed anything. Must run while OBS is closed (OBS
    rewrites the scene collection on exit)."""
    data = load_scene_collection()
    entry = find_script_entry(data, "clip_reexport.py")
    if not entry:
        return False
    settings = entry.setdefault("settings", {})
    if settings.get(DISCORD_MIGRATED_KEY):
        return False
    changed = settings.get("target_mb") == OLD_DISCORD_TARGET_MB
    if changed:
        settings["target_mb"] = float(DISCORD_TARGET_MB)
    settings[DISCORD_MIGRATED_KEY] = True
    save_scene_collection(data)
    return changed


# ---------------------------------------------------------------------------
# obs-websocket
# ---------------------------------------------------------------------------

def generate_obs_websocket_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


def validate_port(port):
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(f"Port must be between 1 and 65535 (got {port}).")
    return port


def ensure_obs_websocket_password():
    """Replaces the shipped (public) websocket password with a random one,
    once. Returns the loaded config dict, or None if the file is missing."""
    config = load_json(OBS_WEBSOCKET_CONFIG_PATH)
    if not isinstance(config, dict):
        return None
    if config.get("server_password") in (None, "", SHIPPED_OBS_WEBSOCKET_PASSWORD):
        config["server_password"] = generate_obs_websocket_password()
        write_json_atomic(OBS_WEBSOCKET_CONFIG_PATH, config)
    return config


def load_obs_websocket_settings():
    config = ensure_obs_websocket_password()
    if config is None:
        raise FileNotFoundError(f"obs-websocket config not found: {OBS_WEBSOCKET_CONFIG_PATH}")
    return {"port": config["server_port"], "password": config["server_password"]}


def save_obs_websocket_settings(port, password):
    if not password:
        raise ValueError("Password can't be empty.")
    config = load_json_strict(OBS_WEBSOCKET_CONFIG_PATH)
    config["server_port"] = validate_port(port)
    config["server_password"] = password
    write_json_atomic(OBS_WEBSOCKET_CONFIG_PATH, config)
    return config


# ---------------------------------------------------------------------------
# Clip hotkey (the "Save Replay Buffer" keybinding), stored the way OBS
# itself persists output hotkeys: a JSON blob in basic.ini's [Hotkeys]
# "ReplayBuffer" key, read when OBS creates the replay buffer output.
# ---------------------------------------------------------------------------

HOTKEYS_INI_SECTION = "Hotkeys"
CLIP_HOTKEY_INI_KEY = "ReplayBuffer"
CLIP_HOTKEY_ACTION = "ReplayBuffer.Save"
DEFAULT_CLIP_HOTKEY = (False, True, False, "~")  # Alt+~
HOTKEY_KEY_CHOICES = (
    ["~"]
    + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    + [str(d) for d in range(10)]
    + [f"F{n}" for n in range(1, 13)]
)


def hotkey_label_to_obs_key(label):
    normalized = label.strip().upper()
    aliases = {"~": "OBS_KEY_ASCIITILDE", "TILDE": "OBS_KEY_ASCIITILDE"}
    if normalized in aliases:
        return aliases[normalized]
    if re.fullmatch(r"F(1[0-9]|2[0-4]|[1-9])", normalized):
        return f"OBS_KEY_{normalized}"
    if len(normalized) == 1 and (normalized.isalpha() or normalized.isdigit()):
        return f"OBS_KEY_{normalized}"
    raise ValueError(f"Unrecognized hotkey: {label!r}")


def obs_key_to_label(obs_key):
    name = obs_key.replace("OBS_KEY_", "")
    if name == "ASCIITILDE":
        return "~"
    return name


def describe_hotkey(ctrl, alt, shift, key_label):
    """e.g. "Ctrl+Shift+F9"."""
    mods = "+".join(m for m, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift)) if on)
    return f"{mods}+{key_label}" if mods else key_label


def load_clip_hotkey():
    """(ctrl, alt, shift, key_label) for the clip hotkey from basic.ini, or
    DEFAULT_CLIP_HOTKEY if the file/key is missing or unparseable."""
    if not os.path.exists(BASIC_INI):
        return DEFAULT_CLIP_HOTKEY
    lines = read_ini_lines()
    start, end = find_ini_section(lines, HOTKEYS_INI_SECTION)
    if start is None:
        return DEFAULT_CLIP_HOTKEY
    for line in lines[start:end]:
        key, sep, value = line.partition("=")
        if sep and key.strip() == CLIP_HOTKEY_INI_KEY:
            try:
                combos = json.loads(value.strip()).get(CLIP_HOTKEY_ACTION, [])
                if not combos:
                    return DEFAULT_CLIP_HOTKEY
                combo = combos[0]
                return (
                    bool(combo.get("control", combo.get("ctrl", False))),
                    bool(combo.get("alt", False)),
                    bool(combo.get("shift", False)),
                    obs_key_to_label(combo.get("key", "OBS_KEY_ASCIITILDE")),
                )
            except (ValueError, AttributeError):
                return DEFAULT_CLIP_HOTKEY
    return DEFAULT_CLIP_HOTKEY


def save_clip_hotkey(ctrl, alt, shift, key_label):
    """Writes the clip hotkey into basic.ini's [Hotkeys] section. Raises
    ValueError for an unrecognized key, FileNotFoundError without basic.ini."""
    if not os.path.exists(BASIC_INI):
        raise FileNotFoundError(f"Config file not found: {BASIC_INI}")
    combo = {"key": hotkey_label_to_obs_key(key_label)}
    if ctrl:
        combo["control"] = True
    if alt:
        combo["alt"] = True
    if shift:
        combo["shift"] = True
    new_line = f"{CLIP_HOTKEY_INI_KEY}={json.dumps({CLIP_HOTKEY_ACTION: [combo]})}\n"

    lines = read_ini_lines()
    start, end = find_ini_section(lines, HOTKEYS_INI_SECTION)
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", f"[{HOTKEYS_INI_SECTION}]\n", new_line]
    else:
        for i in range(start, end):
            key, sep, _value = lines[i].partition("=")
            if sep and key.strip() == CLIP_HOTKEY_INI_KEY:
                lines[i] = new_line
                break
        else:
            lines.insert(end, new_line)
    write_ini_lines(lines)


# ---------------------------------------------------------------------------
# Stuck-screen detection (the tray widget's - see tray-widget/stuck_screen.py)
# ---------------------------------------------------------------------------

# Where the setting lived back when the check was an OBS script.
STUCK_CHECK_SCRIPT_NAME = "stuck_output_check.py"


def load_stuck_check_enabled():
    """From run_config.json - not an OBS file, so it can change any time.
    Installs from before the check moved into the tray widget still have it
    in the old script's settings, which count until it's first saved here."""
    run_config = load_json(clipem_common.RUN_CONFIG_PATH)
    if isinstance(run_config, dict) and "stuck_check_enabled" in run_config:
        return bool(run_config["stuck_check_enabled"])
    try:
        entry = find_script_entry(load_scene_collection(), STUCK_CHECK_SCRIPT_NAME)
    except (OSError, ValueError):
        entry = None
    if not entry:
        return True
    return bool(entry.get("settings", {}).get("stuck_check_enabled", True))


def save_stuck_check_enabled(enabled):
    config = clipem_common.load_run_config()
    config["stuck_check_enabled"] = bool(enabled)
    clipem_common.save_run_config(config)


def load_black_check_enabled():
    """The tray widget's black-screen detection (run_config.json)."""
    run_config = load_json(clipem_common.RUN_CONFIG_PATH)
    if isinstance(run_config, dict) and "black_check_enabled" in run_config:
        return bool(run_config["black_check_enabled"])
    return True


def load_disk_space_warnings_enabled():
    """The tray widget's low-disk-space and large-recording notifications
    (run_config.json - see tray-widget/disk_space.py)."""
    run_config = load_json(clipem_common.RUN_CONFIG_PATH)
    if isinstance(run_config, dict) and "disk_space_warnings_enabled" in run_config:
        return bool(run_config["disk_space_warnings_enabled"])
    return True


# ---------------------------------------------------------------------------
# The display Setup pointed screen capture at (run_config.json
# "capture_display"), so the tray's recovery can point it back there - unless
# the user has since picked another display in OBS itself ("user_changed",
# set by the tray when OBS reports that change). PipeWire has none: its
# display is picked in the desktop's own portal dialog, not by Setup.
# ---------------------------------------------------------------------------

def save_setup_capture_display(source_id, settings):
    """Called by Setup whenever it picks the capture's display. `settings`:
    just the display keys it wrote (e.g. {"monitor": 1, "monitor_id": ...})."""
    config = clipem_common.load_run_config()
    config["capture_display"] = {"source_id": source_id, "settings": dict(settings), "user_changed": False}
    clipem_common.save_run_config(config)


def load_setup_capture_display():
    """{"source_id", "settings", "user_changed"}, or None if Setup never
    picked a display."""
    record = clipem_common.load_run_config().get("capture_display")
    if not isinstance(record, dict) or not isinstance(record.get("settings"), dict) or not record["settings"]:
        return None
    return record


def set_capture_display_user_changed(user_changed):
    config = clipem_common.load_run_config()
    record = config.get("capture_display")
    if not isinstance(record, dict) or bool(record.get("user_changed")) == bool(user_changed):
        return
    record["user_changed"] = bool(user_changed)
    clipem_common.save_run_config(config)


# ---------------------------------------------------------------------------
# Settings waiting for OBS to be closed
#
# OBS reads the websocket and hotkey settings only at startup and has no
# way to change them while running (obs-websocket has no request for
# either). So settings changed while OBS is running
# are queued here instead, and applied by apply_pending_obs_settings() once
# OBS is closed: right away by the tray's "restart OBS" flow, or otherwise
# by run.py just before it next starts OBS.
# ---------------------------------------------------------------------------

PENDING_OBS_SETTINGS_PATH = os.path.join(SHARED_DIR, "pending_obs_settings.json")


# ---------------------------------------------------------------------------
# Split audio tracks: with it on, OBS records 3 tracks - 1 the full mix, 2
# the mic, 3 desktop audio - and the tray widget (tray-widget/app_recorder.py)
# records every app that plays sound itself. When a clip is saved, each app
# that was heard during it becomes a track of the clip's "-tracks.mkv" from
# track 4 on, named after the app (tray-widget/tray_tracks.py). Nothing is
# configured: apps get a track per clip, in the order they were first heard.
# ---------------------------------------------------------------------------

FIXED_TRACK_TITLES = ("Mix", "Mic", "Desktop")  # what OBS records itself
FIRST_APP_TRACK = len(FIXED_TRACK_TITLES) + 1  # 1-based: the first app track
# Built by Dev/build/build-app-capture.sh when a release is made (the source
# is Dev/app-capture) and shipped with the shared files.
APP_CAPTURE_HELPER_PATH = os.path.join(SHARED_DIR, "Tools", "clipem-app-capture.exe")
WINDOWS_APP_CAPTURE_MIN_BUILD = 19041  # process loopback capture: Windows 10 2004


def app_key(exe):
    """How apps are told apart: the executable's name, lowercase, without ".exe"."""
    name = os.path.basename(exe or "").lower()
    return name[:-4] if name.endswith(".exe") else name


def app_capture_supported(system=None, which=shutil.which):
    """Whether the tray can record apps here: Linux needs pactl and parec
    (PulseAudio or PipeWire's pulse server), Windows 10 2004+ needs
    ClipEm's capture helper. macOS has no per-app capture."""
    system = system or platform.system()
    if system == "Linux":
        return bool(which("pactl") and which("parec"))
    if system == "Windows":
        try:
            if sys.getwindowsversion().build < WINDOWS_APP_CAPTURE_MIN_BUILD:
                return False
        except AttributeError:
            pass
        return os.path.isfile(APP_CAPTURE_HELPER_PATH)
    return False


def load_split_tracks():
    """Whether split audio tracks are on (clip_reexport.py's
    split_tracks_enabled) - turned on and off by setup.save_split_tracks."""
    data = load_scene_collection()
    entry = find_script_entry(data, "clip_reexport.py") if data else None
    return bool((entry or {}).get("settings", {}).get("split_tracks_enabled", False))


def load_clip_script_settings():
    """clip_reexport.py's saved settings (split_tracks_enabled,
    tray_tracks_enabled, reexport_enabled, target_mb, crop_seconds) - empty
    when the scene collection or the script's entry isn't there."""
    data = load_scene_collection()
    entry = find_script_entry(data, "clip_reexport.py") if data else None
    return dict((entry or {}).get("settings", {}))


def load_tray_tracks():
    """Whether the tray widget records the app tracks and finishes each clip
    (setup.save_split_tracks turns it on where app capture is supported)."""
    return bool(load_clip_script_settings().get("tray_tracks_enabled", False))


def app_capture_helper_missing(system=None):
    """True where the tray needs ClipEm's capture helper (Windows) and it
    isn't installed."""
    return (system or platform.system()) == "Windows" and not os.path.isfile(APP_CAPTURE_HELPER_PATH)


def migrate_split_tracks_layout():
    """Moves Split audio tracks to the current layout (OBS records Mix, Mic
    and Desktop, the tray records the apps) when it's on and either predates
    tray recording (no tray_tracks_enabled yet) or was saved with tray
    recording off only because per-app capture wasn't available then (a
    missing helper) and is now. Runs on every start, so shipping the helper
    later turns app tracks back on. Returns True if it changed anything.
    Must run while OBS is closed."""
    settings = load_clip_script_settings()
    if not settings.get("split_tracks_enabled"):
        return False
    if "tray_tracks_enabled" in settings and (settings["tray_tracks_enabled"] or not app_capture_supported()):
        return False
    import setup  # imported here: setup imports this module

    setup.save_split_tracks(True)
    return True


# ---------------------------------------------------------------------------
# Clip sound: Scripts/replay_buffer_save_sound.py's settings (in the scene
# collection) and the .wav it plays - a fixed file name that
# portable_launch_helper always points the script at, so choosing another
# sound is a plain copy over it (picked up on the next clip, no restart).
# ---------------------------------------------------------------------------

CLIP_SOUND_SCRIPT_NAME = "replay_buffer_save_sound.py"
CLIP_SOUND_WAV_PATH = os.path.join(SHARED_DIR, "Scripts", "replay_buffer_save_sound.wav")
DEFAULT_CLIP_SOUND = {"enabled": True, "volume": 0.5}
# The sounds shipped with ClipEm, by the name Settings and Setup show. Choosing
# one copies it over CLIP_SOUND_WAV_PATH (see replace_clip_sound_wav), which is
# never shipped itself: updates leave an install's sound alone, and a missing
# one (a new or reset install) starts as the default preset.
CLIP_SOUND_PRESETS = {
    "Scissors": "replay_buffer_save_sound-scissors.wav",
    "Flash": "replay_buffer_save_sound-flash.wav",
}
DEFAULT_CLIP_SOUND_PRESET = "Scissors"


def clip_sound_preset_path(name):
    return os.path.join(os.path.dirname(CLIP_SOUND_WAV_PATH), CLIP_SOUND_PRESETS[name])


def ensure_clip_sound_wav(scripts_dir=None):
    """Gives an install without a clip sound the default preset; one that
    has a sound (any, including a custom one) is left alone."""
    import shutil

    scripts_dir = scripts_dir or os.path.dirname(CLIP_SOUND_WAV_PATH)
    target = os.path.join(scripts_dir, os.path.basename(CLIP_SOUND_WAV_PATH))
    source = os.path.join(scripts_dir, CLIP_SOUND_PRESETS[DEFAULT_CLIP_SOUND_PRESET])
    if not os.path.exists(target) and os.path.exists(source):
        shutil.copyfile(source, target)


def active_clip_sound_name():
    """The preset the clip sound is a copy of, or None (a custom sound)."""
    try:
        with open(CLIP_SOUND_WAV_PATH, "rb") as f:
            current = f.read()
        for name in CLIP_SOUND_PRESETS:
            with open(clip_sound_preset_path(name), "rb") as f:
                if f.read() == current:
                    return name
    except OSError:
        pass
    return None


def load_clip_sound():
    """{"enabled": bool, "volume": 0.0-1.0} from the scene collection."""
    entry = find_script_entry(load_scene_collection(), CLIP_SOUND_SCRIPT_NAME)
    settings = (entry or {}).get("settings", {})
    try:
        volume = max(0.0, min(1.0, float(settings.get("audio_volume", DEFAULT_CLIP_SOUND["volume"]))))
    except (TypeError, ValueError):
        volume = DEFAULT_CLIP_SOUND["volume"]
    return {"enabled": bool(settings.get("sound_enabled", DEFAULT_CLIP_SOUND["enabled"])), "volume": volume}


def save_clip_sound(enabled, volume):
    data = load_scene_collection()
    entry = find_script_entry(data, CLIP_SOUND_SCRIPT_NAME)
    if not entry:
        raise RuntimeError(f"{CLIP_SOUND_SCRIPT_NAME} entry not found in the scene collection")
    settings = entry.setdefault("settings", {})
    settings["sound_enabled"] = bool(enabled)
    settings["audio_volume"] = max(0.0, min(1.0, float(volume)))
    save_scene_collection(data)


def replace_clip_sound_wav(source_path):
    """Copies a .wav over the clip sound. Raises ValueError if it isn't a
    WAV file (the players the script uses only handle WAV)."""
    with open(source_path, "rb") as f:
        header = f.read(12)
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError(f"{os.path.basename(source_path)} isn't a WAV file.")
    import shutil

    shutil.copyfile(source_path, CLIP_SOUND_WAV_PATH)


# Kept in step with the copy in Scripts/replay_buffer_save_sound.py, which
# runs inside OBS and can't import this module.
def wav_at_volume(path, volume):
    """The WAV file at path, as bytes, with its samples scaled by volume
    (0-1) - winsound has no volume control of its own. Integer PCM only;
    raises wave.Error for anything else (e.g. float samples)."""
    import array
    import io
    import wave

    volume = max(0.0, min(1.0, volume))
    with wave.open(path, "rb") as src:
        params = src.getparams()
        frames = src.readframes(params.nframes)
    width = params.sampwidth
    if width == 1:  # unsigned, centred on 128
        frames = bytes(128 + int((b - 128) * volume) for b in frames)
    elif width in (2, 4):
        samples = array.array("h" if width == 2 else "i", frames)
        frames = array.array(samples.typecode, (int(s * volume) for s in samples)).tobytes()
    elif width == 3:
        scaled = bytearray(len(frames))
        for i in range(0, len(frames) - 2, 3):
            sample = int.from_bytes(frames[i:i + 3], "little", signed=True)
            scaled[i:i + 3] = int(sample * volume).to_bytes(3, "little", signed=True)
        frames = bytes(scaled)
    else:
        raise wave.Error(f"unsupported sample width {width}")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return buf.getvalue()


def play_wav_windows(path, volume):
    """Plays path at volume without blocking. winsound can't play from
    memory asynchronously, so the scaled copy plays on its own thread; a
    WAV it can't scale plays as-is (at the system volume)."""
    import threading
    import wave
    import winsound

    try:
        data = wav_at_volume(path, volume)
    except (wave.Error, EOFError):
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return
    threading.Thread(target=winsound.PlaySound, args=(data, winsound.SND_MEMORY), daemon=True).start()


def play_clip_sound(volume, wav_path=None):
    """Plays the clip sound (or wav_path) now, the way
    replay_buffer_save_sound.py does inside OBS - it can't be called from
    here, since it imports obspython. Raises OSError if nothing can play it."""
    import platform
    import subprocess

    path = wav_path or CLIP_SOUND_WAV_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"Sound file not found: {path}")
    system = platform.system()
    if system == "Windows":
        play_wav_windows(path, volume)
    elif system == "Darwin":
        subprocess.Popen(["afplay", "-v", str(volume), path])
    else:
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        try:
            pa_volume = str(int(max(0.0, min(1.0, volume)) * 65536))
            subprocess.Popen(["paplay", f"--volume={pa_volume}", path], **quiet)
        except FileNotFoundError:
            subprocess.Popen(["aplay", path], **quiet)


def apply_obs_settings(settings):
    """Writes settings - "websocket" {"port", "password"}, "clip_hotkey"
    [ctrl, alt, shift, key], "clip_sound" {"enabled", "volume"},
    "clip_folder" (a path), "split_tracks" (bool), "clip_reexport"
    {"enabled", "target_mb", "crop_seconds"} (the "-small.mp4" copy), and
    Setup's recording settings - "video" {"base_width", "base_height",
    "output_width", "output_height", "fps"}, "capture_monitor" {"index",
    "device_id"}, "replay_buffer_seconds", "recording_bitrate",
    "audio_devices" {"output", "input"} and "mic_filter" {"noise_floor_db",
    "speech_level_db"} - to OBS's config files. Only safe while OBS isn't
    running.
    ("stuck_check_enabled" is only still accepted for queues left from
    before it stopped being an OBS setting.)"""
    if "websocket" in settings:
        save_obs_websocket_settings(settings["websocket"]["port"], settings["websocket"]["password"])
    if "clip_hotkey" in settings:
        save_clip_hotkey(*settings["clip_hotkey"])
    if "clip_sound" in settings:
        save_clip_sound(settings["clip_sound"]["enabled"], settings["clip_sound"]["volume"])
    if "clip_folder" in settings:
        save_clip_folder(settings["clip_folder"])
    if "split_tracks" in settings:
        # Imported here: setup imports this module, and its split-tracks
        # writer edits the scene collection's audio sources too.
        import setup

        setup.save_split_tracks(settings["split_tracks"])
    if "clip_reexport" in settings:
        import setup

        reexport = settings["clip_reexport"]
        setup.save_clip_reexport(reexport["enabled"], reexport["target_mb"], reexport["crop_seconds"])
    if any(key in settings for key in SETUP_RECORDING_KEYS):
        import setup

        if "video" in settings:
            video = settings["video"]
            setup.save_video_settings(video["base_width"], video["base_height"], video["output_width"],
                                      video["output_height"], video["fps"])
        if "capture_monitor" in settings:
            setup.save_capture_monitor(settings["capture_monitor"]["index"], settings["capture_monitor"].get("device_id"))
        if "replay_buffer_seconds" in settings:
            setup.save_replay_buffer_seconds(settings["replay_buffer_seconds"])
        if "recording_bitrate" in settings:
            setup.save_recording_bitrate(settings["recording_bitrate"])
        if "audio_devices" in settings:
            setup.save_audio_devices(settings["audio_devices"]["output"], settings["audio_devices"]["input"])
        if "mic_filter" in settings:
            setup.save_mic_filter(settings["mic_filter"]["noise_floor_db"], settings["mic_filter"]["speech_level_db"])
    if "stuck_check_enabled" in settings:
        save_stuck_check_enabled(settings["stuck_check_enabled"])


# Setup's recording settings, which Settings queues too (see apply_obs_settings).
SETUP_RECORDING_KEYS = ("video", "capture_monitor", "replay_buffer_seconds", "recording_bitrate",
                        "audio_devices", "mic_filter")


def load_pending_obs_settings():
    pending = load_json(PENDING_OBS_SETTINGS_PATH)
    return pending if isinstance(pending, dict) else {}


def queue_obs_settings(settings):
    """Merges settings into the pending file (newer values win)."""
    pending = load_pending_obs_settings()
    pending.update(settings)
    write_json_atomic(PENDING_OBS_SETTINGS_PATH, pending)


def apply_pending_obs_settings():
    """Applies and clears queued settings. Returns what was applied ({} if
    nothing was queued). Only call while OBS isn't running."""
    pending = load_pending_obs_settings()
    if pending:
        apply_obs_settings(pending)
    try:
        os.remove(PENDING_OBS_SETTINGS_PATH)
    except OSError:
        pass
    return pending
