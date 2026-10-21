"""Finding the apps that play sound (tray-widget/app_audio.py) and telling
the recorder about them."""

import json
import sys
import types

from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)

import app_audio  # noqa: E402
import obs_config  # noqa: E402


def test_parse_pactl_sink_inputs():
    text = json.dumps([
        {"corked": False, "properties": {"application.process.binary": "spotify", "application.name": "Spotify"}},
        {"corked": True, "properties": {"application.process.binary": "firefox", "application.name": "Firefox"}},
        {"corked": False, "properties": {"application.process.binary": "spotify", "application.name": "Spotify"}},
        {"corked": False, "properties": {"application.process.binary": "paplay"}},
        {"corked": False, "properties": {}},
        {"corked": False, "properties": {"application.process.binary": "Discord"}},
    ])
    assert app_audio.parse_pactl_sink_inputs(text) == [("spotify", "Spotify"), ("Discord", "Discord")]


def test_parse_pactl_falls_back_to_the_client_and_app_name(tmp_path, monkeypatch):
    monkeypatch.setattr(app_audio, "_desktop_names", {})
    monkeypatch.setattr(app_audio, "DESKTOP_DIRS", [str(tmp_path)])
    (tmp_path / "com.discordapp.Discord.desktop").write_text(
        "[Desktop Action new]\nName=New Window\n[Desktop Entry]\nType=Application\nName=Discord\n")
    text = json.dumps([
        # Spotify's native PipeWire stream: no binary, its client has one.
        {"corked": False, "client": "7", "properties": {"application.name": "Spotify", "node.name": "spotify"}},
        # Discord names its stream after WebRTC; its Flatpak's .desktop has the real name.
        {"corked": False, "client": "8", "properties": {"application.process.binary": "Discord",
                                                        "application.name": "WEBRTC VoiceEngine"}},
        {"corked": False, "properties": {"application.name": "Some Game"}},
        {"corked": False, "properties": {"application.process.binary": "chrome", "application.name": "Chromium"}},
    ])
    clients = json.dumps([
        {"index": 7, "properties": {"application.process.binary": "spotify",
                                    "pipewire.access.portal.app_id": "com.spotify.Client"}},
        {"index": 8, "properties": {"pipewire.access.portal.app_id": "com.discordapp.Discord"}},
    ])
    assert app_audio.parse_pactl_sink_inputs(text, clients) == [
        ("spotify", "Spotify"), ("Discord", "Discord"), ("Some Game", "Some Game"), ("chrome", "chrome"),
    ]


def test_parse_pactl_streams_keeps_paused_streams_and_names_the_monitor():
    text = json.dumps([
        {"index": 11, "sink": 2, "corked": True, "properties": {"application.process.binary": "firefox"}},
        {"index": 12, "sink": 2, "corked": False, "properties": {"application.process.binary": "firefox"}},
        {"index": 13, "sink": 9, "corked": False, "properties": {"application.process.binary": "game"}},
    ])
    sinks = json.dumps([{"index": 2, "name": "alsa_out", "monitor_source": "alsa_out.monitor"}])
    streams = app_audio.parse_pactl_streams(text, None, sinks)
    assert [(s.key, s.handle, s.corked) for s in streams] == [
        ("firefox", ("pa", 11, "alsa_out.monitor"), True),
        ("firefox", ("pa", 12, "alsa_out.monitor"), False),
        ("game", ("pa", 13, None), False),
    ]
    assert app_audio.active_apps(streams) == [("firefox", "firefox"), ("game", "game")]


def test_an_app_updated_while_it_runs_is_still_the_same_app():
    text = json.dumps([{"index": 1, "corked": False, "properties": {"application.process.binary": "brave (deleted)",
                                                                   "application.name": "Brave"}}])
    assert [(s.key, s.exe) for s in app_audio.parse_pactl_streams(text)] == [("brave", "brave")]


def test_the_clip_editor_is_never_recorded():
    text = json.dumps([{"corked": False, "properties": {"application.process.binary": "clipem-editor",
                                                        "application.name": "ClipEm Editor"}},
                       {"corked": False, "properties": {"application.process.binary": "obs64"}},
                       {"corked": False, "properties": {"application.process.binary": "paplay"}}])
    assert app_audio.parse_pactl_sink_inputs(text) == []


def test_app_targets_group_an_apps_streams_in_the_order_apps_appear():
    stream = app_audio.Stream
    streams = [
        stream("discord", "Discord", "Discord", ("pa", 1, "m"), False),
        stream("spotify", "spotify", "Spotify", ("pa", 3, "m"), True),
        stream("discord", "Discord", "Discord", ("pa", 2, "m"), True),
        stream("discord", "Discord", "Discord", ("pa", 1, "m"), False),
    ]
    assert app_audio.app_targets(streams) == {
        "discord": ("Discord", [("pa", 1, "m"), ("pa", 2, "m")]),
        "spotify": ("Spotify", [("pa", 3, "m")]),
    }
    assert list(app_audio.app_targets(streams)) == ["discord", "spotify"]


def test_app_capture_check():
    assert obs_config.app_capture_supported("Linux", which=lambda n: n) is True
    assert obs_config.app_capture_supported("Linux", which=lambda n: None if n == "parec" else n) is False
    assert obs_config.app_capture_supported("Darwin") is False


class FakeRecorder:
    def __init__(self):
        self.targets = []
        self.stopped = 0

    def set_targets(self, targets):
        self.targets.append(targets)

    def stop(self):
        self.stopped += 1


def stream(name, handle, corked=False):
    return app_audio.Stream(name, name, name.title(), handle, corked)


def make_monitor(streams, enabled=True):
    recorder = FakeRecorder()
    monitor = app_audio.AppTrackMonitor(recorder, list_streams=lambda: streams, interval_seconds=0,
                                        enabled=lambda: enabled)
    return monitor, recorder


def test_monitor_hands_the_recorder_every_app():
    monitor, recorder = make_monitor([stream("spotify", ("pa", 1, "m")), stream("discord", ("pa", 2, "m"), True)])
    monitor.tick()
    assert recorder.targets == [{"spotify": ("Spotify", [("pa", 1, "m")]), "discord": ("Discord", [("pa", 2, "m")])}]


def test_monitor_does_nothing_when_tray_tracks_are_off_and_lets_go_when_turned_off():
    monitor, recorder = make_monitor([stream("spotify", ("pa", 1, "m"))], enabled=False)
    monitor.tick()
    assert recorder.targets == [] and recorder.stopped == 0
    monitor._enabled = lambda: True
    monitor.tick()
    monitor._enabled = lambda: False
    monitor.tick()
    monitor.tick()
    assert len(recorder.targets) == 1 and recorder.stopped == 1


def test_monitor_survives_not_being_able_to_list_apps():
    def broken():
        raise OSError("no pactl")

    recorder = FakeRecorder()
    monitor = app_audio.AppTrackMonitor(recorder, list_streams=broken, interval_seconds=0, enabled=lambda: True)
    monitor.tick()
    monitor.tick()
    assert recorder.targets == []
