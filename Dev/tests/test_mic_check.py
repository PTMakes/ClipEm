"""The tray widget's missing-mic detection (tray-widget/mic_check.py),
driven by a fake obs-websocket client."""

import sys
import types

import pytest
from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)
import mic_check  # noqa: E402

MIC = "Mic/Aux"


class FakeObs:
    def __init__(self):
        self.tracks = {"1": True, "2": True}
        self.volume_db = -6.0
        self.muted = False
        self.settings = {"device_id": "default"}
        self.calls = []

    def call(self, request_type, request_data=None, timeout=10):
        self.calls.append((request_type, request_data))
        if request_type == "GetSpecialInputs":
            return {"desktop1": "Desktop Audio", "mic1": MIC, "mic2": None}
        assert request_data["inputName"] == MIC
        if request_type == "GetInputAudioTracks":
            return {"inputAudioTracks": dict(self.tracks)}
        if request_type == "SetInputAudioTracks":
            self.tracks.update(request_data["inputAudioTracks"])
            return {}
        if request_type == "GetInputVolume":
            return {"inputVolumeDb": self.volume_db}
        if request_type == "SetInputVolume":
            self.volume_db = request_data["inputVolumeDb"]
            return {}
        if request_type == "GetInputMute":
            return {"inputMuted": self.muted}
        if request_type == "GetInputSettings":
            return {"inputSettings": dict(self.settings)}
        if request_type == "SetInputSettings":
            return {}
        raise AssertionError(request_type)

    def requested(self, request_type):
        return [data for kind, data in self.calls if kind == request_type]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def setup():
    obs, clock, notifications = FakeObs(), Clock(), []
    check = mic_check.MicCheck(notifications.append, clock=clock)
    return obs, clock, notifications, check


def meters(level):
    return {"inputs": [{"inputName": MIC, "inputLevelsMul": [[level, level, level], [level, level, level]]}]}


# Watching starts at the first tick, so DEAD_SECONDS of silence takes one tick more.
DEAD = mic_check.DEAD_SECONDS + 10


def run(check, obs, clock, seconds, level=None, writing=True):
    """Ticks every 10 s for `seconds`, with the mic's meter at `level` (None: no meter events)."""
    for _ in range(int(seconds // 10)):
        clock.now += 10
        if level is not None:
            check.on_volume_meters(meters(level))
        check.tick(obs, lambda: True, writing)


def test_a_working_mic_is_left_alone(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, 600, level=0.01)
    assert notifications == []
    assert not obs.requested("SetInputSettings") and not obs.requested("SetInputVolume")


def test_mic_off_the_mix_track_is_put_back(setup):
    obs, clock, notifications, check = setup
    obs.tracks["1"] = False
    run(check, obs, clock, 10, level=0.01)
    assert obs.tracks["1"] is True
    assert len(notifications) == 1 and "fixed" in notifications[0]


def test_mic_turned_almost_all_the_way_down_is_turned_back_up(setup):
    obs, clock, notifications, check = setup
    obs.volume_db = -45.0
    run(check, obs, clock, 10, level=0.01)
    assert obs.volume_db == mic_check.RESTORE_VOLUME_DB
    assert len(notifications) == 1


def test_a_silent_mic_is_reopened_then_reported(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD, level=0.0)
    assert obs.requested("SetInputSettings") == [
        {"inputName": MIC, "inputSettings": {"device_id": "default"}, "overlay": True}]
    assert notifications == []  # reopening it is tried first
    run(check, obs, clock, DEAD, level=0.0)
    assert len(notifications) == 1 and "No sound" in notifications[0]
    run(check, obs, clock, 600, level=0.0)
    assert len(notifications) == 1  # not again until the cooldown is up
    assert len(obs.requested("SetInputSettings")) == 1


def test_a_mic_with_no_meter_events_at_all_counts_as_silent(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD)
    assert len(obs.requested("SetInputSettings")) == 1


def test_sound_after_reopening_clears_it(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD, level=0.0)
    run(check, obs, clock, 30, level=0.02)
    run(check, obs, clock, mic_check.DEAD_SECONDS - 10, level=0.0)
    assert notifications == []
    run(check, obs, clock, 20, level=0.0)  # silent long enough again: reopened again, not reported yet
    assert len(obs.requested("SetInputSettings")) == 2 and notifications == []


def test_silence_while_not_saving_clips_isnt_watched(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, 600, level=0.0, writing=False)
    run(check, obs, clock, mic_check.DEAD_SECONDS - 10, level=0.0)
    assert obs.calls and not obs.requested("SetInputSettings")


def test_a_muted_mic_is_only_reported_once(setup):
    obs, clock, notifications, check = setup
    obs.muted = True
    run(check, obs, clock, 600, level=0.0)
    assert len(notifications) == 1 and "muted" in notifications[0]
    assert not obs.requested("SetInputSettings")


def test_off_in_settings_does_nothing(setup):
    obs, clock, notifications, check = setup
    obs.tracks["1"] = False
    clock.now += 10
    check.tick(obs, lambda: False, True)
    assert obs.calls == [] and notifications == []
