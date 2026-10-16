"""Per-app split audio tracks (tray-widget/app_audio.py): which app owns
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

A, B, C = obs_config.APP_SLOT_SOURCE_NAMES
DAY = 86400
BUFFER = 60


def fresh():
    return obs_config.empty_app_tracks()


def owners(state):
    return {name: owner["exe"] if owner else None for name, owner in state["slots"].items()}


def test_new_apps_fill_slots_in_order_and_overflow_goes_nowhere():
    state = fresh()
    changed = app_audio.assign_slots(
        state, [("Game.exe", "Game"), ("Spotify.exe", "Spotify"), ("Discord.exe", "Discord"), ("Edge.exe", "Edge")],
        1000, BUFFER,
    )
    assert changed == [A, B, C]
    assert owners(state) == {A: "Game.exe", B: "Spotify.exe", C: "Discord.exe"}


def test_an_owner_never_changes_slot_even_with_free_slots():
    state = fresh()
    app_audio.assign_slots(state, [("Game.exe", "Game"), ("Spotify.exe", "Spotify")], 1000, BUFFER)
    # Game closes; Spotify keeps playing alone - and Game comes back later.
    assert app_audio.assign_slots(state, [("Spotify.exe", "Spotify")], 2000, BUFFER) == []
    assert app_audio.assign_slots(state, [("game.EXE", "Game"), ("Spotify.exe", "Spotify")], 3000, BUFFER) == []
    assert owners(state) == {A: "Game.exe", B: "Spotify.exe", C: None}
    assert state["slots"][A]["last_heard"] == 3000


def test_owners_keep_their_slots_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_config, "APP_TRACKS_PATH", str(tmp_path / "app_tracks.json"))
    state = fresh()
    app_audio.assign_slots(state, [("Game.exe", "Game"), ("Spotify.exe", "Spotify")], 1000, BUFFER)
    obs_config.save_app_tracks(state)

    reloaded = obs_config.load_app_tracks()  # a new tray process
    assert app_audio.assign_slots(reloaded, [("Spotify.exe", "Spotify"), ("Game.exe", "Game")], 5000, BUFFER) == []
    assert owners(reloaded) == {A: "Game.exe", B: "Spotify.exe", C: None}


def test_a_slot_is_only_reused_after_its_owner_expires():
    state = fresh()
    app_audio.assign_slots(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    app_audio.assign_slots(state, [("b.exe", "b"), ("c.exe", "c")], 10 * DAY, BUFFER)
    assert app_audio.assign_slots(state, [("new.exe", "new")], 13 * DAY, BUFFER) == []
    # a: unheard for over 14 days - its slot goes to the newcomer...
    assert app_audio.assign_slots(state, [("new.exe", "new")], 14 * DAY + 1, BUFFER) == [A]
    # ...and when a comes back it gets no other slot (Desktop track only).
    assert app_audio.assign_slots(state, [("a.exe", "a")], 15 * DAY, BUFFER) == []
    assert owners(state) == {A: "new.exe", B: "b.exe", C: "c.exe"}


def test_a_heard_owner_is_never_expired_by_a_newcomer_in_the_same_check():
    state = fresh()
    app_audio.assign_slots(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    later = 30 * DAY
    app_audio.assign_slots(state, [("new.exe", "new"), ("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], later, BUFFER)
    assert owners(state) == {A: "a.exe", B: "b.exe", C: "c.exe"}


def test_pinned_slots_never_expire():
    state = fresh()
    app_audio.assign_slots(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    for owner in state["slots"].values():
        owner["pinned"] = True
    assert app_audio.assign_slots(state, [("new.exe", "new")], 365 * DAY, BUFFER) == []


def test_a_long_replay_buffer_delays_expiry():
    state = fresh()
    state["expiry_days"] = 1
    app_audio.assign_slots(state, [("a.exe", "a"), ("b.exe", "b"), ("c.exe", "c")], 0, BUFFER)
    assert app_audio.assign_slots(state, [("new.exe", "new")], DAY + 1, 2 * DAY) == []
    assert app_audio.assign_slots(state, [("new.exe", "new")], 2 * DAY + 1, 2 * DAY) == [A]


def test_ignored_apps_never_get_a_slot():
    state = fresh()
    assert app_audio.assign_slots(state, [("obs64.exe", "OBS"), ("paplay", "paplay")], 0, BUFFER) == []


def test_parse_pactl_sink_inputs():
    text = json.dumps([
        {"corked": False, "properties": {"application.process.binary": "spotify", "application.name": "Spotify"}},
        {"corked": True, "properties": {"application.process.binary": "firefox", "application.name": "Firefox"}},
        {"corked": False, "properties": {"application.process.binary": "spotify", "application.name": "Spotify"}},
        {"corked": False, "properties": {"application.process.binary": "paplay"}},
        {"corked": False, "properties": {"application.name": "no binary"}},
        {"corked": False, "properties": {"application.process.binary": "Discord"}},
    ])
    assert app_audio.parse_pactl_sink_inputs(text) == [("spotify", "Spotify"), ("Discord", "Discord")]


def test_load_app_tracks_tolerates_a_bad_file(tmp_path, monkeypatch):
    path = tmp_path / "app_tracks.json"
    monkeypatch.setattr(obs_config, "APP_TRACKS_PATH", str(path))
    path.write_text('{"slots": {"ClipEm App A": {"exe": "x.exe", "last_heard": "soon"}, "Bogus": {}}}')
    state = obs_config.load_app_tracks()
    assert state["slots"][A] == {"exe": "x.exe", "label": "x.exe", "last_heard": 0.0, "pinned": False}
    assert set(state["slots"]) == {A, B, C}
    path.write_text("not json")
    assert obs_config.load_app_tracks() == fresh()


class FakeClient:
    def __init__(self, inputs):
        self.inputs = inputs
        self.set_calls = []

    def call(self, request, data=None):
        if request == "GetInputList":
            return {"inputs": self.inputs}
        if request == "SetInputSettings":
            self.set_calls.append((data["inputName"], data["inputSettings"]))
            return {}
        raise AssertionError(request)


def make_monitor(tmp_path, monkeypatch, apps):
    monkeypatch.setattr(obs_config, "APP_TRACKS_PATH", str(tmp_path / "app_tracks.json"))
    clock = {"t": 1000.0}
    monitor = app_audio.AppTrackMonitor(
        list_apps=lambda: apps, clock=lambda: clock["t"], monotonic=lambda: clock["t"],
        buffer_seconds=lambda: BUFFER, interval_seconds=0,
    )
    return monitor, clock


def test_monitor_retargets_the_new_owners_source_and_saves(tmp_path, monkeypatch):
    kind = "wasapi_process_output_capture"
    client = FakeClient([{"inputName": n, "inputKind": kind} for n in (A, B, C)] +
                        [{"inputName": "Mic/Aux", "inputKind": "wasapi_input_capture"}])
    monitor, _clock = make_monitor(tmp_path, monkeypatch, [("Spotify.exe", "Spotify")])
    monitor.tick(client)
    # First the saved (empty) owners are pushed to OBS, then Spotify's slot.
    assert [name for name, _ in client.set_calls] == [A, B, C, A]
    assert client.set_calls[-1][1] == {"window": "::Spotify.exe", "priority": 2, "clipem_label": "Spotify"}
    assert obs_config.load_app_tracks()["slots"][A]["exe"] == "Spotify.exe"

    client.set_calls.clear()
    monitor.tick(client)
    assert client.set_calls == []  # nothing new


def test_monitor_does_nothing_without_slot_sources(tmp_path, monkeypatch):
    client = FakeClient([{"inputName": "Mic/Aux", "inputKind": "pulse_input_capture"}])
    monitor, _clock = make_monitor(tmp_path, monkeypatch, [("spotify", "Spotify")])
    monitor.tick(client)
    assert client.set_calls == []
    assert not os.path.exists(obs_config.APP_TRACKS_PATH)


def test_monitor_picks_up_a_reset_from_setup(tmp_path, monkeypatch):
    kind = "pipewire_audio_application_capture"
    client = FakeClient([{"inputName": n, "inputKind": kind} for n in (A, B, C)])
    apps = [("spotify", "Spotify")]
    monitor, clock = make_monitor(tmp_path, monkeypatch, apps)
    monitor.tick(client)
    obs_config.save_app_tracks(obs_config.empty_app_tracks())  # Setup's Reset
    os.utime(obs_config.APP_TRACKS_PATH, (0, 0))  # a different mtime, however fast the test runs
    apps[:] = [("discord", "Discord")]
    client.set_calls.clear()
    monitor.tick(client)
    assert client.set_calls[-1] == (A, {"CaptureMode": 0, "MatchPriorty": 0, "ExceptApp": False,
                                        "TargetName": "discord", "clipem_label": "Discord"})
