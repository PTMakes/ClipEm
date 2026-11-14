"""The tray widget's missing-audio detection (tray-widget/mic_check.py) -
the mic and the desktop audio - driven by a fake obs-websocket client."""

import sys
import types

import pytest
from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)
import mic_check  # noqa: E402

MIC = "Mic/Aux"
DESKTOP = "Desktop Audio"


class Input:
    def __init__(self, device_id="default"):
        self.tracks = {"1": True, "2": True}
        self.volume_db = -6.0
        self.muted = False
        self.settings = {"device_id": device_id, "use_device_timing": False}
        self.filters = []


class FakeObs:
    def __init__(self):
        self.inputs = {MIC: Input(), DESKTOP: Input()}
        self.calls = []

    @property
    def mic(self):
        return self.inputs[MIC]

    @property
    def desktop(self):
        return self.inputs[DESKTOP]

    def call(self, request_type, request_data=None, timeout=10):
        self.calls.append((request_type, request_data))
        if request_type == "GetSpecialInputs":
            return {"desktop1": DESKTOP, "desktop2": None, "mic1": MIC, "mic2": None}
        source = self.inputs[request_data.get("inputName") or request_data.get("sourceName")]
        if request_type == "GetInputAudioTracks":
            return {"inputAudioTracks": dict(source.tracks)}
        if request_type == "SetInputAudioTracks":
            source.tracks.update(request_data["inputAudioTracks"])
            return {}
        if request_type == "GetInputVolume":
            return {"inputVolumeDb": source.volume_db}
        if request_type == "SetInputVolume":
            source.volume_db = request_data["inputVolumeDb"]
            return {}
        if request_type == "GetInputMute":
            return {"inputMuted": source.muted}
        if request_type == "GetInputSettings":
            return {"inputSettings": dict(source.settings)}
        if request_type == "SetInputSettings":
            source.settings.update(request_data["inputSettings"])
            return {}
        if request_type == "GetSourceFilterList":
            return {"filters": source.filters}
        raise AssertionError(request_type)

    def requested(self, request_type, name=MIC):
        return [data for kind, data in self.calls
                if kind == request_type and (data or {}).get("inputName", name) == name]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class World:
    """What's really there, outside OBS: the mic device's level and the apps
    playing sound."""

    def __init__(self):
        self.mic_level = 0.0
        self.apps = []
        self.probes = []

    def device_peak(self, device_id, flow):
        self.probes.append((device_id, flow))
        return self.mic_level

    def heard_apps(self, output_device):
        self.probes.append((output_device, "apps"))
        return list(self.apps)


@pytest.fixture
def setup():
    obs, clock, notifications, world = FakeObs(), Clock(), [], World()
    check = mic_check.MicCheck(notifications.append, clock=clock, device_peak=world.device_peak,
                               heard_apps=world.heard_apps)
    check.world = world
    return obs, clock, notifications, check


def meters(mic=None, desktop=None):
    inputs = []
    for name, level in ((MIC, mic), (DESKTOP, desktop)):
        if level is not None:
            inputs.append({"inputName": name, "inputLevelsMul": [[level, level, level], [level, level, level]]})
    return {"inputs": inputs}


def run(check, obs, clock, seconds, level=None, desktop=0.05, writing=True):
    """Ticks every 10 s for `seconds`, with the mic's meter at `level` and
    the desktop's at `desktop` (None: no meter events for it)."""
    for _ in range(int(seconds // 10)):
        clock.now += 10
        check.on_volume_meters(meters(level, desktop))
        check.tick(obs, lambda: True, writing)


# Watching starts at the first tick, so DEAD_SECONDS of silence takes one tick more.
DEAD = mic_check.DEAD_SECONDS + 10
RETRY = mic_check.RETRY_SECONDS
# From the first fix to the first alert: every try, each given RETRY_SECONDS.
ALL_TRIES = mic_check.FIX_ATTEMPTS * RETRY


def reopened(obs, name=MIC):
    return [data for data in obs.requested("SetInputSettings", name) if data["inputSettings"] != {"device_id": "default"}]


# -- the mic -------------------------------------------------------------------

def test_a_working_mic_is_left_alone(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, 600, level=0.01)
    assert notifications == []
    assert not obs.requested("SetInputSettings") and not obs.requested("SetInputVolume")
    assert check.world.probes == []  # nothing is measured while OBS hears it


def test_mic_off_the_mix_track_is_put_back(setup):
    obs, clock, notifications, check = setup
    obs.mic.tracks["1"] = False
    run(check, obs, clock, 10, level=0.01)
    assert obs.mic.tracks["1"] is True
    assert len(notifications) == 1 and "fixed" in notifications[0]


def test_mic_turned_almost_all_the_way_down_is_turned_back_up(setup):
    obs, clock, notifications, check = setup
    obs.mic.volume_db = -45.0
    run(check, obs, clock, 10, level=0.01)
    assert obs.mic.volume_db == mic_check.RESTORE_VOLUME_DB
    assert len(notifications) == 1


def test_a_silent_mic_is_tried_several_times_before_the_user_is_told(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD, level=0.0)
    assert len(reopened(obs)) == 1 and notifications == []  # reopening it is tried first
    run(check, obs, clock, ALL_TRIES - 10, level=0.0)
    assert len(reopened(obs)) == mic_check.FIX_ATTEMPTS and notifications == []
    run(check, obs, clock, 10, level=0.0)
    assert len(notifications) == 1 and "No sound" in notifications[0]
    assert "not even background noise" in notifications[0]  # the device itself delivers nothing
    assert check.world.probes == [("default", "input")]


def test_a_mic_on_a_particular_device_is_switched_to_the_default_input(setup):
    obs, clock, notifications, check = setup
    obs.mic.settings["device_id"] = "usb-headset"
    run(check, obs, clock, DEAD + RETRY, level=0.0)
    assert obs.mic.settings["device_id"] == "default"
    assert notifications == []
    run(check, obs, clock, 10, level=0.02)  # the default input works
    assert len(notifications) == 1 and "switched it to your current default input" in notifications[0]
    run(check, obs, clock, 600, level=0.02)
    assert len(notifications) == 1


def test_a_mic_that_stays_silent_is_reported_again_and_again(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD + ALL_TRIES, level=0.0)
    assert len(notifications) == 1
    run(check, obs, clock, mic_check.ALERT_REPEAT_SECONDS - 10, level=0.0)
    assert len(notifications) == 1
    run(check, obs, clock, 10, level=0.0)
    assert len(notifications) == 2 and notifications[1].startswith("Still no sound")
    run(check, obs, clock, 2 * mic_check.ALERT_REPEAT_SECONDS, level=0.0)
    assert len(notifications) == 4
    # ...and it's reopened again with every reminder.
    assert len(reopened(obs)) == mic_check.FIX_ATTEMPTS + len(notifications)


def test_a_mic_that_comes_back_after_the_user_was_told_says_so(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD + ALL_TRIES, level=0.0)
    run(check, obs, clock, 10, level=0.02)
    assert len(notifications) == 2 and "working again" in notifications[1]
    run(check, obs, clock, 600, level=0.02)
    assert len(notifications) == 2


def test_a_mic_with_no_meter_events_at_all_counts_as_silent(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD, level=None)
    assert len(reopened(obs)) == 1


def test_sound_after_reopening_clears_it(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, DEAD, level=0.0)
    run(check, obs, clock, 30, level=0.02)
    run(check, obs, clock, mic_check.DEAD_SECONDS - 10, level=0.0)
    assert notifications == [] and len(reopened(obs)) == 1
    run(check, obs, clock, 20, level=0.0)  # silent long enough again: tried again from the start
    assert len(reopened(obs)) == 2 and notifications == []


def test_a_gated_mic_in_a_quiet_room_is_left_alone(setup):
    """A noise gate keeps a working mic silent in OBS between words - the
    device itself still hears the room."""
    obs, clock, notifications, check = setup
    obs.mic.filters = [{"filterKind": "noise_gate_filter", "filterEnabled": True}]
    check.world.mic_level = 0.002
    run(check, obs, clock, 1200, level=0.0)
    assert notifications == [] and not obs.requested("SetInputSettings")


def test_a_gated_mic_whose_device_is_silent_is_still_fixed(setup):
    obs, clock, notifications, check = setup
    obs.mic.filters = [{"filterKind": "noise_gate_filter", "filterEnabled": True}]
    run(check, obs, clock, DEAD, level=0.0)
    assert len(reopened(obs)) == 1


def test_a_mic_whose_device_has_sound_obs_isnt_getting_is_fixed(setup):
    obs, clock, notifications, check = setup
    check.world.mic_level = 0.01  # no gate: OBS should hear this
    run(check, obs, clock, DEAD + ALL_TRIES, level=0.0)
    assert len(notifications) == 1 and "not even background noise" not in notifications[0]


def test_silence_while_not_saving_clips_isnt_watched(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, 600, level=0.0, writing=False)
    run(check, obs, clock, mic_check.DEAD_SECONDS - 10, level=0.0)
    assert obs.calls and not obs.requested("SetInputSettings")


def test_a_muted_mic_is_only_reported_once(setup):
    obs, clock, notifications, check = setup
    obs.mic.muted = True
    run(check, obs, clock, 600, level=0.0)
    assert len(notifications) == 1 and "muted" in notifications[0]
    assert not obs.requested("SetInputSettings")


def test_off_in_settings_does_nothing(setup):
    obs, clock, notifications, check = setup
    obs.mic.tracks["1"] = False
    clock.now += 10
    check.tick(obs, lambda: False, True)
    assert obs.calls == [] and notifications == []


# -- desktop audio ---------------------------------------------------------------

DESKTOP_SILENT = mic_check.DESKTOP_SILENT_SECONDS + 10


def test_silent_desktop_audio_with_nothing_playing_is_a_quiet_pc(setup):
    obs, clock, notifications, check = setup
    run(check, obs, clock, 1200, level=0.01, desktop=0.0)
    assert notifications == [] and not obs.requested("SetInputSettings", DESKTOP)


def test_desktop_audio_that_hears_nothing_while_an_app_plays_is_fixed_then_reported(setup):
    obs, clock, notifications, check = setup
    check.world.apps = ["Deadlock"]
    run(check, obs, clock, DESKTOP_SILENT, level=0.01, desktop=0.0)
    assert len(reopened(obs, DESKTOP)) == 1 and notifications == []
    run(check, obs, clock, ALL_TRIES, level=0.01, desktop=0.0)
    assert len(reopened(obs, DESKTOP)) == mic_check.FIX_ATTEMPTS + 1  # every try, then again with the alert
    assert len(notifications) == 1 and "Deadlock is playing sound" in notifications[0]
    run(check, obs, clock, mic_check.ALERT_REPEAT_SECONDS, level=0.01, desktop=0.0)
    assert len(notifications) == 2 and notifications[1].startswith("Still no desktop audio")


def test_desktop_audio_on_a_particular_device_is_switched_to_the_current_output(setup):
    obs, clock, notifications, check = setup
    obs.desktop.settings["device_id"] = "hdmi-output"
    check.world.apps = ["Spotify", "Firefox"]
    run(check, obs, clock, DESKTOP_SILENT + RETRY, level=0.01, desktop=0.0)
    assert obs.desktop.settings["device_id"] == "default"
    assert ("hdmi-output", "apps") in check.world.probes  # measured against what it recorded
    run(check, obs, clock, 10, level=0.01, desktop=0.03)
    assert len(notifications) == 1 and "switched it to your current default output" in notifications[0]


def test_desktop_audio_that_comes_back_needs_no_more_fixing(setup):
    obs, clock, notifications, check = setup
    check.world.apps = ["Deadlock"]
    run(check, obs, clock, DESKTOP_SILENT, level=0.01, desktop=0.0)
    run(check, obs, clock, 600, level=0.01, desktop=0.03)
    assert len(reopened(obs, DESKTOP)) == 1 and notifications == []


def test_apps_are_only_measured_once_desktop_audio_is_silent(setup):
    obs, clock, notifications, check = setup
    check.world.apps = ["Deadlock"]
    run(check, obs, clock, 600, level=0.01, desktop=0.03)
    assert check.world.probes == []


def test_muted_desktop_audio_is_only_reported(setup):
    obs, clock, notifications, check = setup
    obs.desktop.muted = True
    check.world.apps = ["Deadlock"]
    run(check, obs, clock, 600, level=0.01, desktop=0.0)
    assert len(notifications) == 1 and "desktop audio" in notifications[0] and "muted" in notifications[0]
    assert not obs.requested("SetInputSettings", DESKTOP)


def test_a_second_desktop_input_that_hears_it_is_enough(setup):
    """Desktop Audio 2 recording another output: sound there is sound in clips."""
    obs, clock, notifications, check = setup
    obs.inputs["Desktop Audio 2"] = Input()
    original = obs.call

    def call(request_type, request_data=None, timeout=10):
        if request_type == "GetSpecialInputs":
            return {"desktop1": DESKTOP, "desktop2": "Desktop Audio 2", "mic1": MIC}
        return original(request_type, request_data, timeout)

    obs.call = call
    check.world.apps = ["Deadlock"]
    for _ in range(60):
        clock.now += 10
        check.on_volume_meters({"inputs": [{"inputName": "Desktop Audio 2", "inputLevelsMul": [[0.1, 0.1, 0.1]]}]})
        check.tick(obs, lambda: True, True)
    assert not obs.requested("SetInputSettings", DESKTOP) and not any("desktop" in n for n in notifications)
