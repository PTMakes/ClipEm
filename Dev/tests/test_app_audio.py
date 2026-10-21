"""Per-app split audio tracks (tray-widget/app_audio.py): which app is on
which track, and keeping each app on its track for good."""

import json
import os
import sys
import types

from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)

import app_audio  # noqa: E402
import obs_config  # noqa: E402

DAY = 86400
BUFFER = 60


def fresh(rules=()):
    """3 auto tracks, plus a rule track per (name, apps) in `rules` at the front."""
    state = obs_config.empty_app_tracks()
    state["tracks"] = [obs_config.new_app_track(name, apps) for name, apps in rules] + state["tracks"]
    return state


def owners(state):
    return [t["owner"]["exe"] if t["owner"] else None for t in state["tracks"]]


def ids(state, *indexes):
    return [state["tracks"][i]["id"] for i in indexes]


def test_new_apps_fill_tracks_in_order_and_overflow_goes_nowhere():
    state = fresh()
    changed = app_audio.assign_tracks(
        state, [("Game.exe", "Game"), ("Spotify.exe", "Spotify"), ("Discord.exe", "Discord"), ("Edge.exe", "Edge")],
        1000, BUFFER,
    )
    assert changed == ids(state, 0, 1, 2)
    assert owners(state) == ["Game.exe", "Spotify.exe", "Discord.exe"]


def test_an_owner_never_changes_track_even_with_free_tracks():
    state = fresh()
    app_audio.assign_tracks(state, [("Game.exe", "Game"), ("Spotify.exe", "Spotify")], 1000, BUFFER)
    # Game closes; Spotify keeps playing alone - and Game comes back later.
    assert app_audio.assign_tracks(state, [("Spotify.exe", "Spotify")], 2000, BUFFER) == []
    assert app_audio.assign_tracks(state, [("game.EXE", "Game"), ("Spotify.exe", "Spotify")], 3000, BUFFER) == []
    assert owners(state) == ["Game.exe", "Spotify.exe", None]
    assert state["tracks"][0]["owner"]["last_heard"] == 3000


def test_owners_keep_their_tracks_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_config, "APP_TRACKS_PATH", str(tmp_path / "app_tracks.json"))
    state = fresh()
    app_audio.assign_tracks(state, [("Game.exe", "Game"), ("Spotify.exe", "Spotify")], 1000, BUFFER)
    obs_config.save_app_tracks(state)

    reloaded = obs_config.load_app_tracks()  # a new tray process
    assert app_audio.assign_tracks(reloaded, [("Spotify.exe", "Spotify"), ("Game.exe", "Game")], 5000, BUFFER) == []
    assert owners(reloaded) == ["Game.exe", "Spotify.exe", None]
    assert [t["id"] for t in reloaded["tracks"]] == [t["id"] for t in state["tracks"]]


def test_a_track_is_only_reused_after_its_owner_expires():
    state = fresh()
    app_audio.assign_tracks(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    app_audio.assign_tracks(state, [("b.exe", "b"), ("c.exe", "c")], 10 * DAY, BUFFER)
    assert app_audio.assign_tracks(state, [("new.exe", "new")], 13 * DAY, BUFFER) == []
    # a: unheard for over 14 days - its track goes to the newcomer...
    assert app_audio.assign_tracks(state, [("new.exe", "new")], 14 * DAY + 1, BUFFER) == ids(state, 0)
    # ...and when a comes back it gets no other track (Desktop track only).
    assert app_audio.assign_tracks(state, [("a.exe", "a")], 15 * DAY, BUFFER) == []
    assert owners(state) == ["new.exe", "b.exe", "c.exe"]


def test_a_heard_owner_is_never_expired_by_a_newcomer_in_the_same_check():
    state = fresh()
    app_audio.assign_tracks(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    later = 30 * DAY
    app_audio.assign_tracks(state, [("new.exe", "new"), ("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], later, BUFFER)
    assert owners(state) == ["a.exe", "b.exe", "c.exe"]


def test_pinned_tracks_never_expire():
    state = fresh()
    app_audio.assign_tracks(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    for track in state["tracks"]:
        track["owner"]["pinned"] = True
    assert app_audio.assign_tracks(state, [("new.exe", "new")], 365 * DAY, BUFFER) == []


def test_a_long_replay_buffer_delays_expiry():
    state = fresh()
    state["expiry_days"] = 1
    app_audio.assign_tracks(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    assert app_audio.assign_tracks(state, [("new.exe", "new")], DAY + 1, 2 * DAY) == []
    assert app_audio.assign_tracks(state, [("new.exe", "new")], 2 * DAY + 1, 2 * DAY) == ids(state, 0)


def test_ignored_apps_never_get_a_track():
    state = fresh()
    assert app_audio.assign_tracks(state, [("obs64.exe", "OBS"), ("paplay", "paplay")], 0, BUFFER) == []


def test_the_clip_editor_never_gets_a_track():
    state = fresh()
    apps = [("clipem-editor", "ClipEm Editor"), ("ClipEm Editor.exe", "ClipEm Editor"), ("electron", "ClipEm Editor")]
    assert app_audio.assign_tracks(state, apps, 0, BUFFER) == []
    text = json.dumps([{"corked": False, "properties": {"application.process.binary": "clipem-editor",
                                                        "application.name": "ClipEm Editor"}}])
    assert app_audio.parse_pactl_sink_inputs(text) == []


def test_a_track_the_editor_took_before_it_was_ignored_is_freed():
    state = fresh()
    app_audio.assign_tracks(state, [("spotify", "Spotify")], 0, BUFFER)
    state["tracks"][1]["owner"] = {"exe": "clipem-editor", "label": "ClipEm Editor", "last_heard": 0, "pinned": True}
    assert app_audio.drop_ignored_owners(state) == ids(state, 1)
    assert state["tracks"][1]["owner"] is None and state["tracks"][0]["owner"]["exe"] == "spotify"


def test_rule_tracks_take_their_apps_and_leave_auto_tracks_alone():
    state = fresh([("Voice", ["Discord.exe", "teamspeak"])])
    assert state["tracks"][0]["apps"] == ["discord", "teamspeak"]
    changed = app_audio.assign_tracks(
        state, [("discord", "Discord"), ("TeamSpeak.exe", "TeamSpeak"), ("game", "Game")], 0, BUFFER)
    assert changed == ids(state, 1)  # only the game needed a track
    assert owners(state) == [None, "game", None, None]


def test_an_owner_that_gets_a_rule_goes_back_to_the_rule_track():
    state = fresh()
    app_audio.assign_tracks(state, [("spotify", "Spotify")], 0, BUFFER)
    state["tracks"].insert(0, obs_config.new_app_track("Music", ["spotify"]))
    streams = [app_audio.Stream("spotify", "spotify", "Spotify", ("pa", 1, "s.monitor"), False)]
    targets = app_audio.track_targets(state, streams)
    assert targets[state["tracks"][0]["id"]][1] == [("pa", 1, "s.monitor")]
    assert targets[state["tracks"][1]["id"]][1] == []  # not recorded twice
    assert app_audio.assign_tracks(state, [("spotify", "Spotify")], 5, BUFFER) == []


def test_track_targets_record_every_stream_of_an_app_paused_or_not():
    state = fresh([("Voice", ["discord", "teamspeak"])])
    app_audio.assign_tracks(state, [("spotify", "Spotify")], 0, BUFFER)
    stream = app_audio.Stream
    streams = [
        stream("discord", "Discord", "Discord", ("pa", 1, "m"), False),
        stream("teamspeak", "ts", "TeamSpeak", ("pa", 2, "m"), True),
        stream("spotify", "spotify", "Spotify", ("pa", 3, "m"), True),
        stream("spotify", "spotify", "Spotify", ("pa", 4, "m"), False),
        stream("browser", "browser", "Browser", ("pa", 5, "m"), False),
    ]
    targets = app_audio.track_targets(state, streams)
    voice, spotify, *others = (targets[t["id"]] for t in state["tracks"])
    assert voice == (("discord", "teamspeak"), [("pa", 1, "m"), ("pa", 2, "m")])
    assert spotify == (("spotify",), [("pa", 3, "m"), ("pa", 4, "m")])
    assert others == [((), []), ((), [])]  # the browser has no track


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


def test_an_owner_with_a_library_label_picks_up_the_real_name():
    state = fresh()
    app_audio.assign_tracks(state, [("Discord", "WEBRTC VoiceEngine")], 0, BUFFER)
    assert app_audio.assign_tracks(state, [("Discord", "Discord")], 10, BUFFER) == ids(state, 0)
    assert state["tracks"][0]["owner"]["label"] == "Discord"
    assert app_audio.assign_tracks(state, [("Discord", "Other")], 20, BUFFER) == []


def test_load_app_tracks_tolerates_a_bad_file(tmp_path, monkeypatch):
    path = tmp_path / "app_tracks.json"
    monkeypatch.setattr(obs_config, "APP_TRACKS_PATH", str(path))
    path.write_text('{"tracks": [{"id": "x", "apps": ["Foo.exe", "foo", ""], "name": "Foo"},'
                    ' {"id": "x", "owner": {"exe": "x.exe", "last_heard": "soon"}}, 5]}')
    state = obs_config.load_app_tracks()
    assert state["tracks"][0] == {"id": "x", "name": "Foo", "apps": ["foo"], "owner": None}
    assert state["tracks"][1]["id"] != "x"  # ids are unique
    assert state["tracks"][1]["owner"] == {"exe": "x.exe", "label": "x.exe", "last_heard": 0.0, "pinned": False}
    assert len(state["tracks"]) == 2
    path.write_text("not json")
    assert [t["apps"] for t in obs_config.load_app_tracks()["tracks"]] == [[], [], []]


def test_old_slot_files_become_auto_tracks_with_the_same_owners(tmp_path, monkeypatch):
    path = tmp_path / "app_tracks.json"
    monkeypatch.setattr(obs_config, "APP_TRACKS_PATH", str(path))
    path.write_text(json.dumps({"expiry_days": 7, "slots": {
        "ClipEm App A": None,
        "ClipEm App B": {"exe": "spotify", "label": "Spotify", "last_heard": 5, "pinned": True},
    }}))
    state = obs_config.load_app_tracks()
    assert state["expiry_days"] == 7
    assert owners(state) == [None, "spotify", None]
    assert state["tracks"][1]["owner"]["pinned"] is True


def test_track_labels_and_the_capture_check():
    rule = obs_config.new_app_track("Voice", ["discord"])
    unnamed = obs_config.new_app_track("", ["a", "b"])
    auto = obs_config.new_app_track()
    assert obs_config.track_label(rule, 4) == "Voice" and obs_config.track_label(unnamed, 4) == "a, b"
    assert obs_config.track_label(auto, 6) == "Track-6"
    auto["owner"] = {"exe": "x", "label": "Spotify", "last_heard": 0, "pinned": False}
    assert obs_config.track_label(auto, 6) == "Spotify"
    assert obs_config.app_capture_supported("Linux", which=lambda n: n) is True
    assert obs_config.app_capture_supported("Linux", which=lambda n: None if n == "parec" else n) is False
    assert obs_config.app_capture_supported("Darwin") is False


class FakeRecorder:
    def __init__(self):
        self.targets = []

    def set_targets(self, targets):
        self.targets.append(targets)


def make_monitor(tmp_path, monkeypatch, streams, enabled=True):
    monkeypatch.setattr(obs_config, "APP_TRACKS_PATH", str(tmp_path / "app_tracks.json"))
    clock = {"t": 1000.0}
    recorder = FakeRecorder()
    monitor = app_audio.AppTrackMonitor(
        recorder, list_streams=lambda: streams, clock=lambda: clock["t"], monotonic=lambda: clock["t"],
        buffer_seconds=lambda: BUFFER, interval_seconds=0, enabled=lambda: enabled,
    )
    return monitor, recorder, clock


def stream(name, handle, corked=False):
    return app_audio.Stream(name, name, name.title(), handle, corked)


def test_monitor_assigns_saves_and_hands_the_recorder_its_streams(tmp_path, monkeypatch):
    streams = [stream("spotify", ("pa", 1, "m"))]
    monitor, recorder, _clock = make_monitor(tmp_path, monkeypatch, streams)
    monitor.tick()
    state = obs_config.load_app_tracks()
    assert state["tracks"][0]["owner"]["exe"] == "spotify"
    assert recorder.targets[-1][state["tracks"][0]["id"]] == (("spotify",), [("pa", 1, "m")])
    assert recorder.targets[-1][state["tracks"][1]["id"]] == ((), [])


def test_monitor_does_nothing_when_tray_tracks_are_off(tmp_path, monkeypatch):
    monitor, recorder, _clock = make_monitor(tmp_path, monkeypatch, [stream("spotify", ("pa", 1, "m"))], enabled=False)
    monitor.tick()
    assert recorder.targets == [] and not os.path.exists(obs_config.APP_TRACKS_PATH)


def test_monitor_lets_go_of_everything_when_turned_off(tmp_path, monkeypatch):
    streams = [stream("spotify", ("pa", 1, "m"))]
    monitor, recorder, _clock = make_monitor(tmp_path, monkeypatch, streams)
    monitor.tick()
    monitor._enabled = lambda: False
    monitor.tick()
    assert recorder.targets[-1] == {}


def test_monitor_picks_up_edits_from_settings(tmp_path, monkeypatch):
    streams = [stream("spotify", ("pa", 1, "m")), stream("discord", ("pa", 2, "m"))]
    monitor, recorder, _clock = make_monitor(tmp_path, monkeypatch, streams)
    monitor.tick()
    state = obs_config.load_app_tracks()
    state["tracks"].insert(0, obs_config.new_app_track("Voice", ["discord"]))  # added in Settings
    obs_config.save_app_tracks(state)
    os.utime(obs_config.APP_TRACKS_PATH, (0, 0))  # a different mtime, however fast the test runs
    monitor.tick()
    voice = state["tracks"][0]["id"]
    assert recorder.targets[-1][voice] == (("discord",), [("pa", 2, "m")])
