"""Finding the apps that play sound (tray-widget/app_audio.py) and telling
the recorder about them."""

import json
import sys
import threading
import time
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


def test_clipems_own_clip_sound_is_never_an_app():
    """paplay is pacat under another name: PipeWire says binary "pacat", name
    "paplay" (as pactl showed for a clip's save sound, which then named the
    clip and got a track of its own)."""
    text = json.dumps([
        {"index": 747646, "corked": False, "properties": {
            "application.name": "paplay", "application.process.binary": "pacat", "node.name": "paplay",
            "media.name": "/home/pt/ClipEm/App/Shared/Sounds/clip.wav"}},
        {"index": 2, "corked": False, "properties": {"application.process.binary": "pw-cat", "application.name": "pw-play"}},
        {"index": 3, "corked": False, "properties": {"application.process.binary": "brave", "application.name": "Brave"}},
    ])
    assert [s.key for s in app_audio.parse_pactl_streams(text)] == ["brave"]
    assert app_audio.is_ignored("pacat") and app_audio.is_ignored("anything", "paplay")
    assert not app_audio.is_ignored("brave", "Brave") and not app_audio.is_ignored("game", "")


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


def test_a_setting_being_rewritten_by_obs_isnt_a_crash(monkeypatch):
    """OBS rewrites the scene collection as it saves or shuts down - and on
    Windows reading it then is refused (PermissionError)."""
    reads = iter([True] + [PermissionError("DefaultPTM.json")] * app_audio.UNREADABLE_WARN_CHECKS + [False])
    warnings, crashes = [], []
    monkeypatch.setattr(app_audio.log, "warning", warnings.append)
    monkeypatch.setattr(app_audio.log, "exception", crashes.append)

    def enabled():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    monitor, recorder = make_monitor([stream("spotify", ("pa", 1, "m"))])
    monitor._enabled = enabled
    monitor.tick()
    for _ in range(app_audio.UNREADABLE_WARN_CHECKS - 1):
        monitor.tick()
    assert len(recorder.targets) == app_audio.UNREADABLE_WARN_CHECKS and recorder.stopped == 0  # as it was
    assert warnings == [] and crashes == []
    monitor.tick()  # still unreadable: now it's worth saying, once
    assert len(warnings) == 1 and "DefaultPTM.json" in warnings[0]
    monitor.tick()  # readable again, and off
    assert recorder.stopped == 1 and crashes == []


def test_monitor_survives_not_being_able_to_list_apps():
    def broken():
        raise OSError("no pactl")

    recorder = FakeRecorder()
    monitor = app_audio.AppTrackMonitor(recorder, list_streams=broken, interval_seconds=0, enabled=lambda: True)
    monitor.tick()
    monitor.tick()
    assert recorder.targets == []


class Listing:
    """Streams that change while a monitor's thread runs; `asked` is set at each look."""

    def __init__(self, streams=()):
        self.streams = list(streams)
        self.asked = threading.Event()

    def __call__(self):
        self.asked.set()
        return list(self.streams)


def _wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def started_monitor(request, listing, record_interval_seconds=60, watch=None):
    recorder = FakeRecorder()
    monitor = app_audio.AppTrackMonitor(recorder, list_streams=listing, enabled=lambda: True,
                                        names_clips=lambda: False, publish=lambda app: None,
                                        record_interval_seconds=record_interval_seconds, watch=watch)
    monitor.start()
    request.addfinalizer(monitor.stop)
    return monitor, recorder


def test_a_new_app_is_recorded_without_the_poll_loop(request):
    """The poll loop waits on OBS - for minutes, while OBS restarts - but the
    replay buffer may already be running: an app that starts then must be
    recorded straight away, or its track misses the start of the clip."""
    listing = Listing()
    _monitor, recorder = started_monitor(request, listing, record_interval_seconds=0.02)
    assert _wait_for(lambda: recorder.targets)  # no tick() at all
    listing.streams = [stream("brave", ("pa", 7, "m"))]
    assert _wait_for(lambda: recorder.targets[-1] == {"brave": ("Brave", [("pa", 7, "m")])}, 0.5)


def test_the_os_saying_a_stream_started_records_it_at_once(request):
    wakes = []

    class Watch:
        stopped = False

        def __init__(self, wake):
            wakes.append(wake)

        def stop(self):
            Watch.stopped = True

    listing = Listing()
    monitor, recorder = started_monitor(request, listing, record_interval_seconds=60, watch=Watch)
    assert _wait_for(lambda: len(recorder.targets) == 1)
    listing.streams = [stream("brave", ("pa", 7, "m"))]
    wakes[0]()  # not the next timed check, a minute away
    assert _wait_for(lambda: len(recorder.targets) == 2, 0.5)
    assert recorder.targets[-1] == {"brave": ("Brave", [("pa", 7, "m")])}
    monitor.stop()
    assert Watch.stopped


def test_once_started_the_poll_loop_only_names_clips(request):
    published = []
    listing = Listing([stream("deadlock", ("pa", 1, "m"))])
    recorder = FakeRecorder()
    monitor = app_audio.AppTrackMonitor(recorder, list_streams=listing, enabled=lambda: True, interval_seconds=0,
                                        names_clips=lambda: True, publish=published.append,
                                        record_interval_seconds=60, watch=None)
    monitor.start()
    request.addfinalizer(monitor.stop)
    assert _wait_for(lambda: len(recorder.targets) == 1)
    monitor.tick()
    assert published == ["Deadlock"] and len(recorder.targets) == 1  # the thread records, not tick()


def test_a_stopped_monitor_records_nothing_more(request):
    listing = Listing()
    monitor, recorder = started_monitor(request, listing, record_interval_seconds=0.01)
    assert _wait_for(lambda: recorder.targets)
    monitor.stop()
    count = len(recorder.targets)
    time.sleep(0.05)
    assert len(recorder.targets) == count and recorder.stopped == 0  # the recorder is the tray's to stop


def test_a_crashing_check_doesnt_end_the_recording_thread(request):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug")
        return []

    recorder = FakeRecorder()
    monitor = app_audio.AppTrackMonitor(recorder, list_streams=lambda: [], enabled=lambda: True,
                                        names_clips=lambda: False, record_interval_seconds=0.01, watch=None)
    recorder.set_targets = lambda targets: flaky()
    monitor.start()
    request.addfinalizer(monitor.stop)
    assert _wait_for(lambda: len(calls) >= 2)


class FakeSubscribe:
    """`pactl subscribe`: prints `lines`, then exits (or waits to be killed)."""

    def __init__(self, lines, ends=True):
        self.lines = lines
        self.ends = ends
        self.killed = threading.Event()
        self.started = 0

    def __call__(self, cmd, **_kwargs):
        assert cmd == ["pactl", "subscribe"]
        self.started += 1
        fake = self

        class Process:
            def __init__(self):
                self.stdout = self._read()

            def _read(self):
                yield from fake.lines
                if not fake.ends:
                    fake.killed.wait(2)

            def kill(self):
                fake.killed.set()

            def poll(self):
                return 0 if fake.killed.is_set() else None

            def wait(self):
                return 0

        return Process()


def test_the_stream_watch_wakes_for_app_streams_only():
    wakes = []
    popen = FakeSubscribe(["Event 'new' on sink-input #71\n", "Event 'change' on sink #2\n",
                           "Event 'remove' on sink-input #70\n"], ends=False)
    watch = app_audio.StreamWatch(lambda: wakes.append(1), popen=popen)
    assert _wait_for(lambda: len(wakes) == 2)
    watch.stop()
    assert popen.killed.wait(1)
    watch._thread.join(1)
    assert not watch._thread.is_alive()


def test_the_stream_watch_starts_pactl_again_when_it_exits(monkeypatch):
    monkeypatch.setattr(app_audio, "STREAM_WATCH_RESTART_SECONDS", 0.01)
    popen = FakeSubscribe([])
    watch = app_audio.StreamWatch(lambda: None, popen=popen)
    assert _wait_for(lambda: popen.started >= 2)
    watch.stop()


def test_without_pactl_the_stream_watch_just_stops():
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("pactl")

    watch = app_audio.StreamWatch(lambda: None, popen=missing)
    watch._thread.join(1)
    assert not watch._thread.is_alive()
    watch.stop()


def test_only_linux_can_say_when_streams_start():
    assert app_audio.watch_streams(lambda: None, system="Windows") is None
    assert app_audio.RECORD_CHECK_SECONDS["Windows"] <= 0.5  # so a new app loses at most half a second


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
