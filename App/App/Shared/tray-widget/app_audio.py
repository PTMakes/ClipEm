"""Per-app audio tracks for "Split audio tracks" (see obs_config's APP_SLOT_*
notes and setup.save_split_tracks).

Driven from the tray's poll loop: every CHECK_INTERVAL_SECONDS, asks the OS
which apps are playing sound right now and gives each one that has never
had a slot a free one, pointing that slot's OBS source at it over
obs-websocket. Who owns which slot is kept in obs_config.APP_TRACKS_PATH, so
an app always lands on the same track:

- An app that owns a slot only ever records to that slot. Its source stays
  pointed at it even while the app is closed (OBS reattaches it when the app
  comes back), and it's never given a different slot.
- A new app only gets a slot nobody has had yet, or one whose owner hasn't
  been heard for expiry_days (and not pinned in Setup). That owner then
  drops to the Desktop track only - it doesn't move to another slot.
- With no such slot, the new app is on the Desktop track only.

Apps playing sound: on Windows, the active WASAPI sessions on the default
output device (pycaw); on Linux, PulseAudio/PipeWire sink inputs that aren't
corked (`pactl`), labelled with the Flatpak's .desktop name when there is one. macOS has no app slots.
"""

import json
import logging
import os
import platform
import subprocess
import threading
import time

import obs_config
from obsws_client import ObsWebSocketError

log = logging.getLogger("tray")

CHECK_INTERVAL_SECONDS = 3
# last_heard is only written to disk this often while nothing else changes -
# expiry is measured in days, so minute-level precision is plenty.
LAST_HEARD_SAVE_SECONDS = 60

# Never given a slot: OBS itself, ClipEm's own clip sound players, and
# system/notification sound helpers. Compared case-insensitively, minus any
# ".exe".
IGNORED_APPS = {
    "obs", "obs64", "obs32", "audiodg", "paplay", "aplay", "pw-play", "pw-cat",
    "canberra-gtk-play", "speech-dispatcher", "sd_espeak-ng", "pipewire",
    "wireplumber", "pulseaudio", "python", "python3", "pythonw",
}

_SYSTEM = platform.system()


def _app_key(exe):
    name = os.path.basename(exe or "").lower()
    return name[:-4] if name.endswith(".exe") else name


# What some apps put in application.name - the audio library's or engine's
# name, not the app's. Never used as a track label. Compared case-insensitively.
GENERIC_APP_NAMES = {
    "chromium", "chromium input", "chrome", "webrtc voiceengine", "playback",
    "alsa plug-in", "pulseaudio", "pipewire", "openal soft", "sdl application",
    "fmod", "wine", "electron",
}

DESKTOP_DIRS = [
    os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                 "flatpak", "exports", "share", "applications"),
    "/var/lib/flatpak/exports/share/applications",
]
_desktop_names = {}


def desktop_entry_name(app_id, dirs=None):
    """The Name= of a Flatpak's .desktop file (e.g. "com.discordapp.Discord"
    -> "Discord"), or None."""
    if app_id in _desktop_names:
        return _desktop_names[app_id]
    name = None
    for directory in dirs or DESKTOP_DIRS:
        try:
            with open(os.path.join(directory, f"{app_id}.desktop"), encoding="utf-8") as f:
                in_entry = False
                for line in f:
                    line = line.strip()
                    if line.startswith("["):
                        in_entry = line == "[Desktop Entry]"
                    elif in_entry and line.startswith("Name="):
                        name = line[len("Name="):].strip() or None
                        break
        except OSError:
            continue
        if name:
            break
    _desktop_names[app_id] = name
    return name


def _app_label(props, binary):
    """A track label: the Flatpak's .desktop name, else application.name
    unless it's a library's name (Discord says "WEBRTC VoiceEngine"), else
    the binary."""
    app_id = props.get("pipewire.access.portal.app_id")
    name = app_id and desktop_entry_name(app_id)
    if name:
        return name
    app_name = props.get("application.name")
    if app_name and app_name.lower() not in GENERIC_APP_NAMES:
        return app_name
    return binary


def parse_pactl_sink_inputs(text, clients_text=None):
    """[(binary, label)] for the uncorked sink inputs in `pactl -f json
    list sink-inputs` output, one entry per app. clients_text (`pactl -f
    json list clients`) fills in what native PipeWire streams leave out:
    Spotify's has no application.process.binary, its client does.

    The "binary" is what the OBS plugin's TargetName is matched against -
    it accepts the stream's or its client's binary, application.name or
    node.name, so falling back to those still targets the app."""
    clients = {}
    for client in json.loads(clients_text or "[]"):
        clients[str(client.get("index"))] = client.get("properties") or {}
    apps = []
    seen = set()
    for sink_input in json.loads(text or "[]"):
        if sink_input.get("corked"):
            continue
        # The stream's own properties win; its client's fill the gaps.
        props = {**clients.get(str(sink_input.get("client")), {}), **(sink_input.get("properties") or {})}
        binary = props.get("application.process.binary") or props.get("application.name") or props.get("node.name")
        if not binary or _app_key(binary) in IGNORED_APPS or _app_key(binary) in seen:
            continue
        seen.add(_app_key(binary))
        apps.append((binary, _app_label(props, binary)))
    return apps


def _pactl_json(what):
    result = subprocess.run(
        ["pactl", "-f", "json", "list", what],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"pactl exited with {result.returncode}")
    return result.stdout


def _linux_active_apps():
    sink_inputs = _pactl_json("sink-inputs")
    try:
        clients = _pactl_json("clients")
    except (OSError, subprocess.SubprocessError):
        clients = None
    return parse_pactl_sink_inputs(sink_inputs, clients)


_com_initialized = threading.local()


def _windows_active_apps():
    import comtypes
    from pycaw.pycaw import AudioUtilities

    # COM is per thread, and the poll loop isn't the thread comtypes was
    # imported on.
    if not getattr(_com_initialized, "done", False):
        comtypes.CoInitialize()
        _com_initialized.done = True
    audio_session_state_active = 1
    apps = []
    seen = set()
    for session in AudioUtilities.GetAllSessions():
        process = session.Process
        if process is None or session.State != audio_session_state_active:
            continue
        exe = process.name()
        if _app_key(exe) in IGNORED_APPS or _app_key(exe) in seen:
            continue
        seen.add(_app_key(exe))
        label = session.DisplayName
        if not label or label.startswith("@"):  # "@%SystemRoot%\..." resource strings
            label = os.path.splitext(exe)[0]
        apps.append((exe, label))
    return apps


def list_active_audio_apps(system=None):
    """[(exe, label)] for apps playing sound right now."""
    system = system or _SYSTEM
    if system == "Windows":
        return _windows_active_apps()
    if system == "Linux":
        return _linux_active_apps()
    return []


def _claimable_slot(state, now, buffer_seconds):
    """A slot nobody has had yet (first in A, B, C order), else the expired,
    unpinned one heard from longest ago, else None."""
    slots = state["slots"]
    for name in obs_config.APP_SLOT_SOURCE_NAMES:
        if slots.get(name) is None:
            return name
    # Never take a slot while its owner's audio could still be in the replay
    # buffer - that clip would have two apps on one track.
    min_silence = max(state["expiry_days"] * 86400, buffer_seconds)
    expired = [
        (owner["last_heard"], name) for name, owner in slots.items()
        if not owner.get("pinned") and now - owner["last_heard"] > min_silence
    ]
    return min(expired)[1] if expired else None


def assign_slots(state, active_apps, now, buffer_seconds):
    """Updates state in place for the apps playing sound now. Returns the
    slot names whose owner changed (their OBS sources need retargeting)."""
    slots = state["slots"]
    owned = {_app_key(owner["exe"]): name for name, owner in slots.items() if owner}
    # Owners first, so one heard right now never looks expired below.
    changed = []
    newcomers = []
    for exe, label in active_apps:
        name = owned.get(_app_key(exe))
        if name:
            slots[name]["last_heard"] = now
            # Owners saved before labels skipped library names ("WEBRTC
            # VoiceEngine" for Discord) pick up the app's real name.
            if slots[name]["label"].lower() in GENERIC_APP_NAMES and label != slots[name]["label"]:
                slots[name]["label"] = label
                changed.append(name)
        elif _app_key(exe) not in IGNORED_APPS:
            newcomers.append((exe, label))

    for exe, label in newcomers:
        if _app_key(exe) in owned:  # listed twice
            continue
        name = _claimable_slot(state, now, buffer_seconds)
        if name is None:
            continue
        previous = slots[name]
        if previous:
            owned.pop(_app_key(previous["exe"]), None)
            log.info(f"App tracks: {previous['label']} hasn't been heard for {state['expiry_days']} days - "
                     f"its track goes to {label}.")
        slots[name] = {"exe": exe, "label": label, "last_heard": now, "pinned": False}
        owned[_app_key(exe)] = name
        changed.append(name)
    return changed


def replay_buffer_seconds():
    try:
        return int(obs_config.read_ini_section("AdvOut").get("RecRBTime", 60))
    except (OSError, ValueError):
        return 60


class AppTrackMonitor:
    """Call tick(client) from the poll loop; forget() after reconnecting.
    Does nothing unless Setup created the app slot sources."""

    def __init__(self, list_apps=list_active_audio_apps, clock=time.time, monotonic=time.monotonic,
                 load_state=obs_config.load_app_tracks, save_state=obs_config.save_app_tracks,
                 buffer_seconds=replay_buffer_seconds, interval_seconds=CHECK_INTERVAL_SECONDS):
        self._list_apps = list_apps
        self._clock = clock
        self._monotonic = monotonic
        self._load_state = load_state
        self._save_state = save_state
        self._buffer_seconds = buffer_seconds
        self._interval = interval_seconds
        self._next_check = None
        self._last_saved = None
        self._slot_kinds = None  # slot source name -> inputKind, once synced with OBS
        self._detect_failed = False
        self._state_mtime = None
        self._state = None

    def forget(self):
        """After a reconnect: OBS may have restarted, re-check its sources."""
        self._slot_kinds = None
        self._next_check = None

    def tick(self, client):
        now = self._monotonic()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + self._interval
        try:
            self._tick(client)
        except ObsWebSocketError as exc:
            log.warning(f"App track check failed: {exc}")
        except Exception:
            # A bug here must not look like a lost OBS connection to the
            # tray's poll loop (which would drop and reconnect, silently).
            log.exception("App track check crashed")

    def _reload_state_if_changed(self):
        """Setup's Reset / Pin write the state file while the tray runs."""
        try:
            mtime = os.path.getmtime(obs_config.APP_TRACKS_PATH)
        except OSError:
            mtime = None
        if self._state is None or mtime != self._state_mtime:
            self._state = self._load_state()
            self._state_mtime = mtime
            return True
        return False

    def _save(self):
        self._save_state(self._state)
        try:
            self._state_mtime = os.path.getmtime(obs_config.APP_TRACKS_PATH)
        except OSError:
            self._state_mtime = None
        self._last_saved = self._monotonic()

    def _tick(self, client):
        reloaded = self._reload_state_if_changed()
        if self._slot_kinds is None or reloaded:
            inputs = client.call("GetInputList").get("inputs", [])
            self._slot_kinds = {
                i["inputName"]: i["inputKind"] for i in inputs
                if i.get("inputName") in obs_config.APP_SLOT_SOURCE_NAMES
            }
            # Make OBS match the saved owners - e.g. after Setup's Reset.
            for name in self._slot_kinds:
                self._retarget(client, name)
        if not self._slot_kinds:
            return

        try:
            active = self._list_apps()
        except Exception as exc:
            if not self._detect_failed:
                log.warning(f"Can't tell which apps are playing sound ({exc}) - app tracks won't fill in.")
                self._detect_failed = True
            return
        self._detect_failed = False

        changed = assign_slots(self._state, active, self._clock(), self._buffer_seconds())
        for name in changed:
            owner = self._state["slots"][name]
            log.info(f"App tracks: {owner['label']} ({owner['exe']}) now records to {name}.")
            self._retarget(client, name)
        owners = {_app_key(owner["exe"]) for owner in self._state["slots"].values() if owner}
        owner_heard = any(_app_key(exe) in owners for exe, _label in active)
        if changed or (owner_heard and (
                self._last_saved is None or self._monotonic() - self._last_saved >= LAST_HEARD_SAVE_SECONDS)):
            self._save()

    def _retarget(self, client, name):
        kind = self._slot_kinds.get(name)
        if not kind:
            return
        client.call("SetInputSettings", {
            "inputName": name,
            "inputSettings": obs_config.app_slot_settings(kind, self._state["slots"].get(name)),
            "overlay": True,
        })
