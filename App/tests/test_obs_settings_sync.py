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
