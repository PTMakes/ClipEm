"""Finding the apps that play sound (tray-widget/app_audio.py) and telling
the recorder about them."""

import json
import sys
import types

import pytest
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


def _stream(key, label=None, corked=False):
    return app_audio.Stream(key, key, label or key, ("pa", 1, "m"), corked)


def test_the_active_app_is_the_game_not_the_music_or_chat():
    active = app_audio.ActiveApp()
    assert active.update([], 0) is None
    assert active.update([_stream("spotify", "Spotify")], 1) == "Spotify"
    assert active.update([_stream("spotify", "Spotify"), _stream("firefox", "Firefox")], 2) == "Firefox"
    assert active.update([_stream("spotify", "Spotify"), _stream("firefox", "Firefox"),
                          _stream("deadlock", "Deadlock")], 3) == "Deadlock"


def test_the_active_app_lasts_through_a_quiet_moment_but_not_for_ever():
    active = app_audio.ActiveApp()
    active.update([_stream("deadlock", "Deadlock")], 0)
    assert active.update([_stream("spotify", "Spotify")], app_audio.ACTIVE_HOLD_SECONDS) == "Deadlock"
    assert active.update([_stream("spotify", "Spotify")], app_audio.ACTIVE_HOLD_SECONDS + 1) == "Spotify"
    assert active.update([_stream("deadlock", "Deadlock", corked=True)], 200) is None


def test_the_monitor_publishes_the_active_app_without_split_tracks():
    published, recorder = [], FakeRecorder()
    clock = [0.0]
    streams = [_stream("deadlock", "Deadlock")]
    monitor = app_audio.AppTrackMonitor(recorder, list_streams=lambda: streams, monotonic=lambda: clock[0],
                                        enabled=lambda: False, names_clips=lambda: True, publish=published.append)
    monitor.tick()
    clock[0] += 3
    monitor.tick()
    assert published == ["Deadlock"]  # only when it changes
    assert not recorder.targets


def test_the_monitor_leaves_streams_alone_when_clips_arent_named_after_apps():
    published = []

    def listed():
        raise AssertionError("no need to ask")

    monitor = app_audio.AppTrackMonitor(FakeRecorder(), list_streams=listed, enabled=lambda: False,
                                        names_clips=lambda: False, publish=published.append)
    monitor.tick()
    assert published == [None]


def _window(app, fullscreen=False, active=False, title="", pid=None):
    return {"pid": pid, "app": app, "title": title, "screen": "DP-1", "fullscreen": fullscreen, "active": active}


def test_a_fullscreen_game_beats_everything_even_silent():
    active = app_audio.ActiveApp()
    streams = [_stream("spotify", "Spotify"), _stream("firefox", "Firefox"), _stream("other", "Other App")]
    assert active.update(streams, 0, [_window("mygame", fullscreen=True)]) == "mygame"


def test_a_focused_window_with_sound_beats_sound_alone():
    active = app_audio.ActiveApp()
    streams = [_stream("other", "Other App"), _stream("mygame", "My Game")]
    assert active.update(streams, 0, [_window("mygame", active=True)]) == "My Game"


def test_a_silent_focused_window_beats_music_but_not_another_app_playing():
    active = app_audio.ActiveApp()
    assert active.update([_stream("spotify", "Spotify")], 0, [_window("mygame", active=True)]) == "mygame"
    assert active.update([_stream("firefox", "Firefox")], 1, [_window("mygame", active=True)]) == "mygame"
    assert active.update([_stream("other", "Other App")], 2, [_window("mygame", active=True)]) == "Other App"


@pytest.mark.parametrize("app", ["discord", "spotify"])
def test_clicking_on_chat_doesnt_make_it_the_game(app):
    active = app_audio.ActiveApp()
    streams = [_stream(app, app.title()), _stream("mygame", "My Game")]
    assert active.update(streams, 0, [_window(app, active=True)]) == "My Game"


def test_a_focused_silent_browser_isnt_what_a_clip_is_of():
    assert app_audio.ActiveApp().update([], 0, [_window("firefox", active=True)]) is None


@pytest.mark.parametrize("app", ["plasmashell", "krunner", "obs", "clipem-editor"])
def test_the_desktop_and_clipem_arent_apps(app):
    assert app_audio.ActiveApp().update([], 0, [_window(app, fullscreen=True, active=True)]) is None


def test_a_protons_window_is_named_by_its_title():
    active = app_audio.ActiveApp()
    streams = [_stream("wine64-preloader", "wine64-preloader")._replace(pid=7)]
    windows = [_window("steam_app_1422450", fullscreen=True, title="Deadlock", pid=7)]
    assert active.update(streams, 0, windows) == "Deadlock"


def test_a_window_and_its_sound_are_matched_by_process():
    active = app_audio.ActiveApp()
    streams = [_stream("other", "Other App"), _stream("gamebin", "Nice Name")._replace(pid=7)]
    assert active.update(streams, 0, [_window("org.studio.game", active=True, pid=7)]) == "Nice Name"


def test_a_windows_class_is_shortened_when_nothing_else_names_it():
    assert app_audio.ActiveApp().update([], 0, [_window("org.kde.dolphin", active=True)]) == "dolphin"


def test_of_two_fullscreen_windows_the_one_seen_last_wins():
    active = app_audio.ActiveApp()
    active.update([], 0, [_window("first", fullscreen=True)])
    assert active.update([], 5, [_window("second", fullscreen=True)]) == "second"


def test_of_two_fullscreen_windows_seen_together_the_top_one_wins():
    windows = [_window("bottom", fullscreen=True), _window("top", fullscreen=True)]
    assert app_audio.ActiveApp().update([], 0, windows) == "top"


def test_a_window_can_ask_to_go_by_its_title():
    windows = [{**_window("cs2", fullscreen=True, title="Counter-Strike 2"), "use_title": True}]
    assert app_audio.ActiveApp().update([_stream("cs2", "cs2")], 0, windows) == "Counter-Strike 2"


@pytest.mark.parametrize("app", ["explorer", "applicationframehost", "searchhost"])
def test_the_windows_shell_isnt_an_app(app):
    assert app_audio.ActiveApp().update([], 0, [_window(app, active=True)]) is None


def test_a_fullscreen_game_lasts_through_an_alt_tab_but_not_for_ever():
    active = app_audio.ActiveApp()
    active.update([], 0, [_window("mygame", fullscreen=True)])
    streams = [_stream("spotify", "Spotify")]
    assert active.update(streams, app_audio.ACTIVE_HOLD_SECONDS, []) == "mygame"
    assert active.update(streams, app_audio.ACTIVE_HOLD_SECONDS + 1, []) == "Spotify"


def test_the_monitor_ranks_what_is_on_screen():
    published = []
    monitor = app_audio.AppTrackMonitor(FakeRecorder(), list_streams=lambda: [_stream("spotify", "Spotify")],
                                        enabled=lambda: False, names_clips=lambda: True, publish=published.append,
                                        windows=lambda: [_window("mygame", fullscreen=True)])
    monitor.tick()
    assert published == ["mygame"]


def test_streams_know_their_process():
    text = json.dumps([{"index": 1, "corked": False, "properties": {
        "application.process.binary": "game", "application.process.id": "4242"}}])
    assert app_audio.parse_pactl_streams(text)[0].pid == 4242
