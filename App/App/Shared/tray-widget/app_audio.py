"""Per-app audio tracks for "Split audio tracks" (see obs_config's notes on
app_tracks.json and setup.save_split_tracks).

Driven from the tray's poll loop: every CHECK_INTERVAL_SECONDS, asks the OS
which apps have an audio stream, works out which track each belongs on and
tells the recorder (app_recorder.py) which streams to record for each track:

- A rule track lists the apps that always go on it (Setup's / the settings
  page's track list) - however many, e.g. "Voice" = Discord + TeamSpeak.
- An auto track (no apps listed) goes to the first app heard that has no rule
  and no track yet, and that app keeps it - across clips, restarts and
  reboots. The owner is only replaced once it hasn't been heard for
  expiry_days (and isn't pinned) - and then it drops to the Desktop track
  only, it doesn't move to another track.
- An app with neither is on the Desktop track only.

"Heard" means a stream that isn't paused. Streams are recorded whether or not
they're paused, so the start of an app's sound isn't lost.

Apps playing sound: on Windows, the WASAPI sessions on the default output
device (pycaw); on Linux, PulseAudio/PipeWire sink inputs (`pactl`), labelled
with the Flatpak's .desktop name when there is one. macOS has no app tracks.
"""

import json
import logging
import os
import platform
import subprocess
import threading
import time
from collections import namedtuple

import obs_config

log = logging.getLogger("tray")

CHECK_INTERVAL_SECONDS = 3
# last_heard is only written to disk this often while nothing else changes -
# expiry is measured in days, so minute-level precision is plenty.
LAST_HEARD_SAVE_SECONDS = 60

# Never given a track: OBS itself, ClipEm's own clip sound players and its
# clip editor window (editor_window.EXE_NAMES - its preview would otherwise
# land on a track of the next clip), and system/notification sound helpers.
# Compared case-insensitively, minus any ".exe".
IGNORED_APPS = {
    "obs", "obs64", "obs32", "audiodg", "paplay", "aplay", "pw-play", "pw-cat",
    "canberra-gtk-play", "speech-dispatcher", "sd_espeak-ng", "pipewire",
    "wireplumber", "pulseaudio", "python", "python3", "pythonw",
    "clipem-editor", "clipem editor",
}
# The same by the name an app gives its sound (application.name / the
# session's display name), for streams whose binary doesn't say.
IGNORED_APP_LABELS = {"clipem editor"}

_SYSTEM = platform.system()


_app_key = obs_config.app_key

# One audio stream of an app. handle: what the recorder captures it with (it
# doesn't look inside) - on Linux ("pa", sink input index, the sink's monitor
# source), on Windows ("win", pid of the app's main process).
Stream = namedtuple("Stream", "key exe label handle corked")


def is_ignored(exe, label=None):
    return _app_key(exe) in IGNORED_APPS or (label or "").strip().lower() in IGNORED_APP_LABELS


def drop_ignored_owners(state):
    """Frees auto tracks owned by apps that are ignored now (e.g. the clip
    editor, which got one before it was). Returns the freed track ids."""
    freed = []
    for track in state["tracks"]:
        owner = track["owner"]
        if owner and is_ignored(owner["exe"], owner["label"]):
            log.info(f"App tracks: {owner['label']} is ignored now - its track is free again.")
            track["owner"] = None
            freed.append(track["id"])
    return freed


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


def parse_pactl_streams(text, clients_text=None, sinks_text=None):
    """[Stream] for every sink input in `pactl -f json list sink-inputs`
    output, paused or not. clients_text (`pactl -f json list clients`) fills
    in what native PipeWire streams leave out: Spotify's has no
    application.process.binary, its client does. sinks_text (`list sinks`)
    names the monitor source each stream's sound can be recorded from.

    The "binary" is what apps are matched by - the stream's or its client's
    binary, falling back to application.name or node.name."""
    clients = {}
    for client in json.loads(clients_text or "[]"):
        clients[str(client.get("index"))] = client.get("properties") or {}
    monitors = {}
    for sink in json.loads(sinks_text or "[]"):
        monitors[sink.get("index")] = (sink.get("monitor_source") or sink.get("monitor_source_name")
                                       or f"{sink.get('name')}.monitor")
    streams = []
    for sink_input in json.loads(text or "[]"):
        # The stream's own properties win; its client's fill the gaps.
        props = {**clients.get(str(sink_input.get("client")), {}), **(sink_input.get("properties") or {})}
        binary = props.get("application.process.binary") or props.get("application.name") or props.get("node.name")
        if not binary:
            continue
        # An app updated while it runs reports its binary as "brave (deleted)".
        binary = binary.removesuffix(" (deleted)")
        label = _app_label(props, binary)
        if is_ignored(binary, label) or is_ignored(binary, props.get("application.name")):
            continue
        handle = ("pa", sink_input.get("index"), monitors.get(sink_input.get("sink")))
        streams.append(Stream(_app_key(binary), binary, label, handle, bool(sink_input.get("corked"))))
    return streams


def active_apps(streams):
    """[(exe, label)], one per app with a stream that isn't paused."""
    apps = {}
    for stream in streams:
        if not stream.corked and stream.key not in apps:
            apps[stream.key] = (stream.exe, stream.label)
    return list(apps.values())


def parse_pactl_sink_inputs(text, clients_text=None):
    """[(binary, label)] for the uncorked sink inputs, one entry per app."""
    return active_apps(parse_pactl_streams(text, clients_text))


def _pactl_json(what):
    result = subprocess.run(
        ["pactl", "-f", "json", "list", what],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"pactl exited with {result.returncode}")
    return result.stdout


def _linux_streams():
    sink_inputs = _pactl_json("sink-inputs")
    try:
        clients = _pactl_json("clients")
    except (OSError, subprocess.SubprocessError):
        clients = None
    try:
        sinks = _pactl_json("sinks")
    except (OSError, subprocess.SubprocessError):
        sinks = None
    return parse_pactl_streams(sink_inputs, clients, sinks)


_com_initialized = threading.local()


def _windows_root_pid(process):
    """The pid to record an app by: the topmost ancestor running the same
    program (Chromium plays sound from a child process), since process
    loopback capture takes in the process's children too."""
    name = process.name().lower()
    root = process
    while True:
        try:
            parent = root.parent()
            if parent is None or parent.name().lower() != name:
                return root.pid
            root = parent
        except Exception:
            return root.pid


def _windows_streams():
    import comtypes
    from pycaw.pycaw import AudioUtilities

    # COM is per thread, and the poll loop isn't the thread comtypes was
    # imported on.
    if not getattr(_com_initialized, "done", False):
        comtypes.CoInitialize()
        _com_initialized.done = True
    audio_session_state_active = 1
    streams = {}
    for session in AudioUtilities.GetAllSessions():
        process = session.Process
        if process is None:
            continue
        exe = process.name()
        label = session.DisplayName
        if not label or label.startswith("@"):  # "@%SystemRoot%\\..." resource strings
            label = os.path.splitext(exe)[0]
        if is_ignored(exe, label):
            continue
        key = _app_key(exe)
        corked = session.State != audio_session_state_active
        previous = streams.get(key)
        if previous and not previous.corked:
            continue
        if previous and corked:
            continue
        streams[key] = Stream(key, exe, label, ("win", _windows_root_pid(process)), corked)
    return list(streams.values())


def list_audio_streams(system=None):
    """[Stream] for every app with audio on this system, paused or not."""
    system = system or _SYSTEM
    if system == "Windows":
        return _windows_streams()
    if system == "Linux":
        return _linux_streams()
    return []


def list_active_audio_apps(system=None):
    """[(exe, label)] for apps playing sound right now."""
    return active_apps(list_audio_streams(system))


def _claimable_track(state, now, buffer_seconds):
    """An auto track nobody has had yet (first in order), else the expired,
    unpinned one heard from longest ago, else None."""
    auto = [t for t in state["tracks"] if not t["apps"]]
    for track in auto:
        if track["owner"] is None:
            return track
    # Never take a track while its owner's audio could still be in the replay
    # buffer - that clip would have two apps on one track.
    min_silence = max(state["expiry_days"] * 86400, buffer_seconds)
    expired = [
        (t["owner"]["last_heard"], index, t) for index, t in enumerate(auto)
        if not t["owner"].get("pinned") and now - t["owner"]["last_heard"] > min_silence
    ]
    return min(expired, key=lambda e: e[:2])[2] if expired else None


def rule_track_for(state, key):
    return next((t for t in state["tracks"] if key in t["apps"]), None)


def assign_tracks(state, active, now, buffer_seconds):
    """Updates state in place for the apps playing sound now. Returns the ids
    of auto tracks whose owner changed."""
    owned = {_app_key(t["owner"]["exe"]): t for t in state["tracks"] if t["owner"] and not t["apps"]}
    changed = []
    newcomers = []
    for exe, label in active:
        key = _app_key(exe)
        track = owned.get(key)
        if track:
            owner = track["owner"]
            owner["last_heard"] = now
            # Owners saved before labels skipped library names ("WEBRTC
            # VoiceEngine" for Discord) pick up the app's real name.
            if owner["label"].lower() in GENERIC_APP_NAMES and label != owner["label"]:
                owner["label"] = label
                changed.append(track["id"])
        elif not is_ignored(exe, label) and rule_track_for(state, key) is None:
            newcomers.append((exe, label))

    for exe, label in newcomers:
        key = _app_key(exe)
        if key in owned:  # listed twice
            continue
        track = _claimable_track(state, now, buffer_seconds)
        if track is None:
            continue
        previous = track["owner"]
        if previous:
            owned.pop(_app_key(previous["exe"]), None)
            log.info(f"App tracks: {previous['label']} hasn't been heard for {state['expiry_days']} days - "
                     f"its track goes to {label}.")
        track["owner"] = {"exe": exe, "label": label, "last_heard": now, "pinned": False}
        owned[key] = track
        changed.append(track["id"])
    return changed


def track_targets(state, streams):
    """{track id: (signature, [stream handles])} - what the recorder should
    capture for each track. The signature changes when the apps a track is
    for do, which tells the recorder to drop what it has of the old ones."""
    targets = {}
    for track in state["tracks"]:
        if track["apps"]:
            keys = tuple(track["apps"])
        elif track["owner"]:
            keys = (_app_key(track["owner"]["exe"]),)
        else:
            keys = ()
        handles = []
        for stream in streams:
            if stream.key in keys and stream.handle not in handles:
                # An app on a rule track and (stale) on an auto track too is
                # recorded on the rule track only.
                if not track["apps"] and rule_track_for(state, stream.key) is not None:
                    continue
                handles.append(stream.handle)
        targets[track["id"]] = (keys, handles)
    return targets


def replay_buffer_seconds():
    try:
        return int(obs_config.read_ini_section("AdvOut").get("RecRBTime", 60))
    except (OSError, ValueError):
        return 60


class AppTrackMonitor:
    """Call tick() from the poll loop. Does nothing unless `enabled()` - split
    audio tracks are on and this system can record apps. recorder:
    app_recorder.AppRecorder (or anything with set_targets(targets))."""

    def __init__(self, recorder, list_streams=list_audio_streams, clock=time.time, monotonic=time.monotonic,
                 load_state=obs_config.load_app_tracks, save_state=obs_config.save_app_tracks,
                 buffer_seconds=replay_buffer_seconds, interval_seconds=CHECK_INTERVAL_SECONDS,
                 enabled=lambda: obs_config.load_tray_tracks() and obs_config.app_capture_supported()):
        self._recorder = recorder
        self._list_streams = list_streams
        self._clock = clock
        self._monotonic = monotonic
        self._load_state = load_state
        self._save_state = save_state
        self._buffer_seconds = buffer_seconds
        self._interval = interval_seconds
        self._enabled = enabled
        self._next_check = None
        self._last_saved = None
        self._detect_failed = False
        self._state_mtime = None
        self._state = None
        self._was_enabled = False

    def state(self):
        """The current assignments (the same dict tick() updates), or None."""
        return self._state

    def forget(self):
        """After a reconnect: check again straight away."""
        self._next_check = None

    def tick(self):
        now = self._monotonic()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + self._interval
        try:
            self._tick()
        except Exception:
            # A bug here must not look like a lost OBS connection to the
            # tray's poll loop (which would drop and reconnect, silently).
            log.exception("App track check crashed")

    def _reload_state_if_changed(self):
        """Setup's Reset / Pin and the settings page write the state file
        while the tray runs."""
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

    def _tick(self):
        enabled = bool(self._enabled())
        if not enabled:
            if self._was_enabled:
                self._recorder.set_targets({})
            self._was_enabled = False
            return
        self._was_enabled = True
        reloaded = self._reload_state_if_changed()
        if reloaded and drop_ignored_owners(self._state):
            self._save()

        try:
            streams = self._list_streams()
        except Exception as exc:
            if not self._detect_failed:
                log.warning(f"Can't tell which apps are playing sound ({exc}) - app tracks won't fill in.")
                self._detect_failed = True
            return
        self._detect_failed = False

        active = active_apps(streams)
        changed = assign_tracks(self._state, active, self._clock(), self._buffer_seconds())
        for track_id in changed:
            track = next(t for t in self._state["tracks"] if t["id"] == track_id)
            owner = track["owner"]
            log.info(f"App tracks: {owner['label']} ({owner['exe']}) now records to track "
                     f"{self._state['tracks'].index(track) + obs_config.FIRST_APP_TRACK}.")
        owners = {_app_key(t["owner"]["exe"]) for t in self._state["tracks"] if t["owner"] and not t["apps"]}
        owner_heard = any(_app_key(exe) in owners for exe, _label in active)
        if changed or (owner_heard and (
                self._last_saved is None or self._monotonic() - self._last_saved >= LAST_HEARD_SAVE_SECONDS)):
            self._save()
        self._recorder.set_targets(track_targets(self._state, streams))
