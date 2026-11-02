"""Per-app audio tracks for "Split audio tracks" (see obs_config's notes on
the layout).

Driven from the tray's poll loop: every CHECK_INTERVAL_SECONDS, asks the OS
which apps have an audio stream and tells the recorder (app_recorder.py) to
record each one. Nothing is assigned or remembered: when a clip is saved,
every app that made sound during it becomes a track, named after the app.

Streams are recorded whether or not they're paused, so the start of an app's
sound isn't lost.

The same check tells clip names (Scripts/clip_naming.py) which game or app is
active: ActiveApp below, which on KDE and Windows also goes by what's on the screen OBS
is capturing (kde_windows.py, win_windows.py).

Apps playing sound: on Windows, the WASAPI sessions on the default output
device (pycaw); on Linux, PulseAudio/PipeWire sink inputs (`pactl`), labelled
with the Flatpak's .desktop name when there is one. macOS has no app tracks.
"""

import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from collections import namedtuple

import obs_config

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Scripts"))
import clip_naming  # noqa: E402 - Scripts/clip_naming.py

log = logging.getLogger("tray")

CHECK_INTERVAL_SECONDS = 3

# Never given a track: OBS itself, ClipEm's own clip sound players and its
# clip editor window (editor_window.EXE_NAMES - its preview would otherwise
# land on a track of the next clip), and system/notification sound helpers.
# Compared case-insensitively, minus any ".exe".
IGNORED_APPS = {
    "obs", "obs64", "obs32", "audiodg", "paplay", "aplay", "pw-play", "pw-cat",
    "canberra-gtk-play", "speech-dispatcher", "sd_espeak-ng", "pipewire",
    "wireplumber", "pulseaudio", "python", "python3", "pythonw",
    "clipem-editor", "clipem editor",
    "clipem-app-capture",  # the Windows capture helper has its own audio session
}
# The same by the name an app gives its sound (application.name / the
# session's display name), for streams whose binary doesn't say.
IGNORED_APP_LABELS = {"clipem editor"}

_SYSTEM = platform.system()


_app_key = obs_config.app_key

# One audio stream of an app. handle: what the recorder captures it with (it
# doesn't look inside) - on Linux ("pa", sink input index, the sink's monitor
# source), on Windows ("win", pid of the app's main process). pid: the process
# playing it, where the OS says.
Stream = namedtuple("Stream", "key exe label handle corked pid", defaults=(None,))


def is_ignored(exe, label=None):
    return _app_key(exe) in IGNORED_APPS or (label or "").strip().lower() in IGNORED_APP_LABELS


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
        pid = str(props.get("application.process.id") or "")
        streams.append(Stream(_app_key(binary), binary, label, handle, bool(sink_input.get("corked")),
                              int(pid) if pid.isdigit() else None))
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
        streams[key] = Stream(key, exe, label, ("win", _windows_root_pid(process)), corked, process.pid)
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


def app_targets(streams):
    """{app key: (label, [stream handles])} - what the recorder captures,
    in the order the apps are listed."""
    targets = {}
    for stream in streams:
        label, handles = targets.setdefault(stream.key, (stream.label, []))
        if stream.handle not in handles:
            handles.append(stream.handle)
    return targets


# What's playing beside the game, so least likely to be what a clip is of:
# browsers, then these chat and music apps. Compared like IGNORED_APPS.
BROWSER_APPS = {"firefox", "firefox-bin", "librewolf", "zen", "zen-bin", "chrome", "google-chrome", "chromium",
                "brave", "brave-browser", "msedge", "opera", "vivaldi", "vivaldi-bin"}
BACKGROUND_APPS = {"discord", "discordcanary", "discordptb", "vesktop", "spotify", "teamspeak", "teamspeak3",
                   "ts3client_linux_amd64", "mumble", "slack", "teams", "ms-teams", "zoom", "skype", "telegram",
                   "telegram-desktop", "signal", "signal-desktop", "element", "whatsapp", "vlc", "mpv", "steam"}
# Desktop parts a window can be focused on without being what a clip is of.
SHELL_WINDOWS = {"plasmashell", "krunner", "kwin_wayland", "kwin_x11", "xwaylandvideobridge", "spectacle",
                 "explorer", "applicationframehost", "searchhost", "startmenuexperiencehost",
                 "shellexperiencehost", "textinputhost", "lockapp"}
# Window classes that say nothing about the app: Proton's are "steam_app_<id>".
# The window's title is the game's name for these.
GENERIC_WINDOW_CLASSES = ("steam_app_", "steam_proton", "gamescope", "wine")
# An app stays active this long after its last sound, or leaving the screen:
# games go quiet too, and get alt-tabbed out of.
ACTIVE_HOLD_SECONDS = 60
# How likely each kind of app is to be what a clip is of, best first.
RANK_FULLSCREEN, RANK_FOCUSED_SOUND, RANK_SOUND, RANK_FOCUSED, RANK_BROWSER, RANK_BACKGROUND = range(6)


def window_label(window, stream):
    """What to call a window's app: its title if its class says nothing (or
    its report says to), the label its sound has, or the end of its class
    ("org.kde.dolphin")."""
    app, title = window["app"], str(window.get("title") or "").strip()
    generic = app.lower().startswith(GENERIC_WINDOW_CLASSES) or app.lower().endswith(".exe")
    if title and (generic or window.get("use_title")):
        return title
    return stream.label if stream else app.rsplit(".", 1)[-1]


class ActiveApp:
    """Which app a clip saved now is of. Of what's been seen in the last
    ACTIVE_HOLD_SECONDS: a fullscreen window on the captured screen, then its
    focused window if that's playing sound, then any app playing sound, then
    that focused window even if it's silent, then a browser, then chat or
    music - and of equals, the window seen last, or the app playing longest."""

    def __init__(self):
        self._heard = {}  # app key: [label, first heard, last heard]
        self._shown = {}  # app key: [label, rank, last seen]

    @staticmethod
    def _rank(key):
        return RANK_BACKGROUND if key in BACKGROUND_APPS else RANK_BROWSER if key in BROWSER_APPS else RANK_SOUND

    def _see_windows(self, windows, playing, now):
        for order, window in enumerate(windows):
            key = _app_key(window["app"])
            if key in SHELL_WINDOWS or is_ignored(key, window.get("title")):
                continue
            stream = next((s for s in playing if s.key == key or (s.pid and s.pid == window.get("pid"))), None)
            if stream:
                key = stream.key
            if window.get("fullscreen"):
                rank = RANK_FULLSCREEN
            elif key in BACKGROUND_APPS:
                continue  # chat doesn't become the game by being clicked on
            elif stream:
                rank = RANK_FOCUSED_SOUND
            elif key in BROWSER_APPS:
                continue
            else:
                rank = RANK_FOCUSED
            # The higher of two windows seen together counts as seen later.
            self._shown[key] = [window_label(window, stream), rank, now + order * 1e-6]

    def update(self, streams, now, windows=()):
        """Returns the active app's label, or None. windows: what's on the
        captured screen (kde_windows.py), bottom to top."""
        self._heard = {key: entry for key, entry in self._heard.items() if now - entry[2] <= ACTIVE_HOLD_SECONDS}
        self._shown = {key: entry for key, entry in self._shown.items() if now - entry[2] <= ACTIVE_HOLD_SECONDS}
        playing = [stream for stream in streams if not stream.corked]
        for stream in playing:
            entry = self._heard.setdefault(stream.key, [stream.label, now, now])
            entry[0], entry[2] = stream.label, now
        self._see_windows(windows, playing, now)
        ranked = [(rank, -seen, 0.0, label) for label, rank, seen in self._shown.values()]
        ranked += [(self._rank(key), 0.0, first, label) for key, (label, first, _last) in self._heard.items()]
        return min(ranked, key=lambda entry: entry[:3])[3] if ranked else None


def replay_buffer_seconds():
    try:
        return int(obs_config.read_ini_section("AdvOut").get("RecRBTime", 60))
    except (OSError, ValueError):
        return 60


class AppTrackMonitor:
    """Call tick() from the poll loop. Records apps only if `enabled()` - split
    audio tracks are on and this system can record apps - and publishes the
    active app only if `names_clips()`. recorder: app_recorder.AppRecorder (or
    anything with set_targets(targets) and stop())."""

    def __init__(self, recorder, list_streams=list_audio_streams, monotonic=time.monotonic,
                 interval_seconds=CHECK_INTERVAL_SECONDS,
                 enabled=lambda: obs_config.load_tray_tracks() and obs_config.app_capture_supported(),
                 names_clips=lambda: clip_naming.load()["app"], publish=clip_naming.publish_active_app,
                 windows=tuple):
        self._recorder = recorder
        self._list_streams = list_streams
        self._monotonic = monotonic
        self._interval = interval_seconds
        self._enabled = enabled
        self._next_check = None
        self._detect_failed = False
        self._was_enabled = False
        self._names_clips = names_clips
        self._publish = publish
        self._windows = windows
        self._active = ActiveApp()
        self._published = ""  # None is a value: nothing's active

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

    def _publish_active(self, app):
        if app != self._published:
            self._published = app
            self._publish(app)

    def _tick(self):
        enabled, names_clips = self._enabled(), self._names_clips()
        if self._was_enabled and not enabled:
            self._recorder.stop()
        self._was_enabled = enabled
        if not names_clips:
            self._publish_active(None)
        if not (enabled or names_clips):
            return
        try:
            streams = self._list_streams()
        except Exception as exc:
            if not self._detect_failed:
                log.warning(f"Can't tell which apps are playing sound ({exc}) - no app tracks or app names in clips.")
                self._detect_failed = True
            return
        self._detect_failed = False
        if enabled:
            self._recorder.set_targets(app_targets(streams))
        if names_clips:
            self._publish_active(self._active.update(streams, self._monotonic(), self._windows()))
