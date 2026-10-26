"""The replay buffer's memory cap, the start/stop hotkeys and the recording
encoder (obs_config.py, setup.py, encoders.py)."""

import json

import encoders
import obs_config
import pytest
import setup


@pytest.fixture
def profile(fake_obs_config, monkeypatch):
    encoder_json = fake_obs_config / "recordEncoder.json"
    monkeypatch.setattr(obs_config, "RECORD_ENCODER_JSON", str(encoder_json))
    monkeypatch.setattr(setup, "RECORD_ENCODER_JSON", str(encoder_json))
    monkeypatch.setattr(setup, "BASIC_INI", obs_config.BASIC_INI)
    return encoder_json


@pytest.mark.parametrize("seconds, video, audio, expected", [
    (60, 18000, 160, 512),        # short: never below OBS's own default
    (300, 18000, 160, 896),       # 5 min at the default bitrate: 512 MB used to cut it to ~3m50s
    (600, 50000, 480, 4736),      # the longest, at the highest bitrate, 3 tracks
])
def test_replay_buffer_memory_holds_the_whole_length(seconds, video, audio, expected):
    size = obs_config.replay_buffer_size_mb(seconds, video, audio)
    assert size == expected
    assert size * 1024 ** 2 >= seconds * (video + audio) * 1000 / 8


def test_saving_the_length_or_bitrate_resizes_the_buffer(profile):
    setup.save_replay_buffer_seconds(300)
    setup.save_recording_bitrate(30000)
    adv = obs_config.read_ini_section("AdvOut")
    assert int(adv["RecRBSize"]) == obs_config.replay_buffer_size_mb(300, 30000, 160)


def test_toggle_hotkeys_round_trip(fake_obs_config):
    assert obs_config.load_toggle_hotkeys() == {"recording": None, "replay_buffer": None}
    obs_config.save_toggle_hotkey("recording", (True, False, True, "F9"))
    assert obs_config.load_toggle_hotkey("recording") == (True, False, True, "F9")
    hotkeys = obs_config.read_ini_section("Hotkeys")
    # Both halves of OBS's pair get the key, which OBS then treats as a toggle.
    assert hotkeys["OBSBasic.StartRecording"] == hotkeys["OBSBasic.StopRecording"]
    assert json.loads(hotkeys["OBSBasic.StartRecording"]) == {
        "bindings": [{"key": "OBS_KEY_F9", "control": True, "shift": True}]}
    assert "ReplayBuffer" in hotkeys  # the clip hotkey is left alone
    obs_config.save_toggle_hotkey("recording", None)
    assert obs_config.load_toggle_hotkey("recording") is None


def test_encoder_is_saved_with_its_own_settings(profile):
    setup.save_recording_bitrate(24000)
    setup.save_recording_encoder(encoders.VAAPI, "/dev/dri/renderD129")
    assert obs_config.read_ini_section("AdvOut")["RecEncoder"] == "ffmpeg_vaapi_tex"
    assert json.loads(profile.read_text()) == {"bitrate": 24000, "rate_control": "CBR",
                                               "vaapi_device": "/dev/dri/renderD129"}
    assert setup.load_recording_encoder() == (encoders.VAAPI, "/dev/dri/renderD129")


def test_a_hardware_encoder_thats_gone_falls_back(profile, monkeypatch):
    setup.save_recording_encoder(encoders.NVENC)
    monkeypatch.setattr(encoders, "available", lambda: [(encoders.X264, None)])
    assert "x264" in setup.ensure_recording_encoder_available()
    assert setup.load_recording_encoder() == (encoders.X264, None)
    assert setup.ensure_recording_encoder_available() is None


def test_a_vaapi_device_that_moved_is_followed(profile, monkeypatch):
    setup.save_recording_encoder(encoders.VAAPI, "/dev/dri/renderD128")
    monkeypatch.setattr(encoders, "available", lambda: [(encoders.VAAPI, "/dev/dri/renderD129"), (encoders.X264, None)])
    assert setup.ensure_recording_encoder_available()
    assert setup.load_recording_encoder() == (encoders.VAAPI, "/dev/dri/renderD129")


def test_windows_gpus_map_to_their_encoders(monkeypatch):
    monkeypatch.setattr(encoders, "windows_gpu_names",
                        lambda: ["NVIDIA GeForce RTX 4070", "Intel(R) UHD Graphics 770"])
    assert encoders.available("Windows") == [(encoders.NVENC, None), (encoders.QSV, None), (encoders.X264, None)]


def test_the_clip_hotkey_can_be_cleared(fake_obs_config):
    obs_config.save_clip_hotkey(True, True, False, obs_config.NO_HOTKEY)  # modifiers alone mean nothing
    assert json.loads(obs_config.read_ini_section("Hotkeys")["ReplayBuffer"]) == {"ReplayBuffer.Save": []}
    assert obs_config.load_clip_hotkey() == obs_config.CLEARED_HOTKEY
    assert obs_config.describe_hotkey(*obs_config.load_clip_hotkey()) == "none"
    obs_config.save_clip_hotkey(False, True, False, "~")
    assert obs_config.load_clip_hotkey() == (False, True, False, "~")
