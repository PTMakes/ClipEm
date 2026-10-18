import json
import os

import obs_config
import obs_process


def test_clip_hotkey_round_trip(fake_obs_config):
    obs_config.save_clip_hotkey(True, False, True, "F9")
    assert obs_config.load_clip_hotkey() == (True, False, True, "F9")
    assert obs_config.describe_hotkey(*obs_config.load_clip_hotkey()) == "Ctrl+Shift+F9"


def test_queued_settings_only_reach_obs_config_when_applied(fake_obs_config):
    obs_config.queue_obs_settings({"websocket": {"port": 4460, "password": "new-password-456"}})
    obs_config.queue_obs_settings({"clip_hotkey": [False, True, False, "C"]})
    # Nothing written to OBS's own files yet...
    assert obs_config.load_obs_websocket_settings()["port"] == 4455

    applied = obs_config.apply_pending_obs_settings()
    assert set(applied) == {"websocket", "clip_hotkey"}
    assert obs_config.load_obs_websocket_settings() == {"port": 4460, "password": "new-password-456"}
    assert obs_config.load_clip_hotkey() == (False, True, False, "C")
    assert not os.path.exists(obs_config.PENDING_OBS_SETTINGS_PATH)
    assert obs_config.apply_pending_obs_settings() == {}


def test_only_this_installs_obs_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(obs_process, "ROOT_DIR", str(tmp_path))
    assert obs_process._is_ours(str(tmp_path / "App" / "Linux" / "obs-install" / "bin" / "obs"))
    assert obs_process._is_ours(str(tmp_path / "App" / "macOS" / "OBS.app" / "Contents" / "MacOS" / "OBS"))
    assert not obs_process._is_ours("/usr/bin/obs")
    assert not obs_process._is_ours(str(tmp_path / "App" / "Windows" / "Python" / "python.exe"))


def test_stuck_check_setting_lives_in_run_config(fake_obs_config):
    assert obs_config.load_stuck_check_enabled() is True
    obs_config.save_stuck_check_enabled(False)
    assert obs_config.load_stuck_check_enabled() is False
    assert json.loads((fake_obs_config / "run_config.json").read_text())["stuck_check_enabled"] is False


def test_stuck_check_setting_falls_back_to_the_old_obs_script_setting(fake_obs_config):
    """Installs from before the check moved into the tray widget."""
    scenes = fake_obs_config / "scenes.json"
    scenes.write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "/x/stuck_output_check.py", "settings": {"stuck_check_enabled": False}},
    ]}}))
    assert obs_config.load_stuck_check_enabled() is False
    obs_config.save_stuck_check_enabled(True)
    assert obs_config.load_stuck_check_enabled() is True


def test_stuck_check_left_in_an_old_queue_still_applies(fake_obs_config):
    obs_config.queue_obs_settings({"stuck_check_enabled": False})
    obs_config.apply_pending_obs_settings()
    assert obs_config.load_stuck_check_enabled() is False


def test_black_check_setting_defaults_on(fake_obs_config):
    assert obs_config.load_black_check_enabled() is True
    (fake_obs_config / "run_config.json").write_text(json.dumps({"black_check_enabled": False}))
    assert obs_config.load_black_check_enabled() is False


def test_disk_space_warnings_setting_defaults_on(fake_obs_config):
    assert obs_config.load_disk_space_warnings_enabled() is True
    (fake_obs_config / "run_config.json").write_text(json.dumps({"disk_space_warnings_enabled": False}))
    assert obs_config.load_disk_space_warnings_enabled() is False


def test_setup_capture_display_round_trip(fake_obs_config):
    assert obs_config.load_setup_capture_display() is None
    obs_config.save_setup_capture_display("monitor_capture", {"monitor": 1})
    obs_config.set_capture_display_user_changed(True)
    assert obs_config.load_setup_capture_display()["user_changed"] is True
    obs_config.save_setup_capture_display("monitor_capture", {"monitor": 2})  # Setup picks again
    assert obs_config.load_setup_capture_display() == {
        "source_id": "monitor_capture", "settings": {"monitor": 2}, "user_changed": False}


def _with_clip_sound_script(tmp_path, settings):
    import json as _json

    scenes = tmp_path / "scenes.json"
    scenes.write_text(_json.dumps({"modules": {"scripts-tool": [
        {"path": "/x/replay_buffer_save_sound.py", "settings": settings},
    ]}}))


def test_clip_sound_round_trip_and_queue(fake_obs_config):
    _with_clip_sound_script(fake_obs_config, {"sound_enabled": True, "audio_volume": 0.5})
    assert obs_config.load_clip_sound() == {"enabled": True, "volume": 0.5}

    obs_config.queue_obs_settings({"clip_sound": {"enabled": False, "volume": 0.25}})
    assert obs_config.load_clip_sound() == {"enabled": True, "volume": 0.5}  # queued, not written
    obs_config.apply_pending_obs_settings()
    assert obs_config.load_clip_sound() == {"enabled": False, "volume": 0.25}


def test_clip_sound_volume_is_clamped(fake_obs_config):
    _with_clip_sound_script(fake_obs_config, {"audio_volume": 7})
    assert obs_config.load_clip_sound()["volume"] == 1.0
    obs_config.save_clip_sound(True, -3)
    assert obs_config.load_clip_sound()["volume"] == 0.0


def test_replacing_the_clip_sound_only_accepts_wav(tmp_path, monkeypatch):
    import pytest

    target = tmp_path / "replay_buffer_save_sound.wav"
    monkeypatch.setattr(obs_config, "CLIP_SOUND_WAV_PATH", str(target))
    mp3 = tmp_path / "ding.mp3"
    mp3.write_bytes(b"ID3\x03\x00\x00\x00")
    with pytest.raises(ValueError):
        obs_config.replace_clip_sound_wav(str(mp3))
    assert not target.exists()

    wav = tmp_path / "ding.wav"
    wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    obs_config.replace_clip_sound_wav(str(wav))
    assert target.read_bytes() == wav.read_bytes()


def test_queued_clip_folder_reaches_every_output_path(fake_obs_config):
    ini = fake_obs_config / "basic.ini"
    ini.write_text("[SimpleOutput]\nFilePath=/old\n\n[AdvOut]\nRecFilePath=/old\nFFFilePath=/old\n")
    obs_config.queue_obs_settings({"clip_folder": "/new/clips"})
    assert obs_config.load_clip_folder() == "/old"

    obs_config.apply_pending_obs_settings()
    assert obs_config.load_clip_folder() == "/new/clips"
    assert ini.read_text().count("=/new/clips\n") == 3


def test_queued_split_tracks_goes_through_setup(fake_obs_config, monkeypatch):
    import setup

    calls = []
    monkeypatch.setattr(setup, "save_split_tracks", calls.append)
    obs_config.queue_obs_settings({"split_tracks": True})
    obs_config.apply_pending_obs_settings()
    assert calls == [True]


def test_queued_small_copy_settings_go_through_setup(fake_obs_config, monkeypatch):
    import setup

    calls = []
    monkeypatch.setattr(setup, "save_clip_reexport", lambda *args: calls.append(args))
    obs_config.queue_obs_settings({"clip_reexport": {"enabled": True, "target_mb": 25.0, "crop_seconds": 20}})
    assert calls == []  # queued while OBS runs
    obs_config.apply_pending_obs_settings()
    assert calls == [(True, 25.0, 20)]


def test_queued_recording_settings_go_through_setup(fake_obs_config, monkeypatch):
    import setup

    calls = []
    for name in ("save_video_settings", "save_capture_monitor", "save_replay_buffer_seconds",
                 "save_recording_bitrate", "save_audio_devices", "save_mic_filter"):
        monkeypatch.setattr(setup, name, lambda *args, n=name: calls.append((n, args)))
    obs_config.queue_obs_settings({
        "video": {"base_width": 2560, "base_height": 1440, "output_width": 1920, "output_height": 1080, "fps": 60},
        "capture_monitor": {"index": 1, "device_id": "\\\\?\\DISPLAY2"},
        "replay_buffer_seconds": 90, "recording_bitrate": 24000,
        "audio_devices": {"output": "default", "input": "mic-2"},
        "mic_filter": {"noise_floor_db": -62.0, "speech_level_db": -24.0},
    })
    assert calls == []
    obs_config.apply_pending_obs_settings()
    assert calls == [
        ("save_video_settings", (2560, 1440, 1920, 1080, 60)),
        ("save_capture_monitor", (1, "\\\\?\\DISPLAY2")),
        ("save_replay_buffer_seconds", (90,)),
        ("save_recording_bitrate", (24000,)),
        ("save_audio_devices", ("default", "mic-2")),
        ("save_mic_filter", (-62.0, -24.0)),
    ]


def test_split_tracks_defaults_off(fake_obs_config):
    assert obs_config.load_split_tracks() is False
    scenes = fake_obs_config / "scenes.json"
    scenes.write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "/x/clip_reexport.py", "settings": {"split_tracks_enabled": True}},
    ]}}))
    assert obs_config.load_split_tracks() is True
