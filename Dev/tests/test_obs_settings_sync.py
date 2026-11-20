import array
import json
import os
import shutil
import subprocess
import sys

import obs_config
import obs_process
import pytest


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


def test_audio_script_settings_round_trip_and_queue(fake_obs_config):
    assert obs_config.load_audio_script_settings() == obs_config.DEFAULT_AUDIO_SCRIPT_SETTINGS
    settings = {"peak_guard_enabled": False, "peak_ceiling_db": -3.0, "debug_enabled": True}
    obs_config.queue_obs_settings({"audio_script": settings})
    assert obs_config.load_audio_script_settings() == obs_config.DEFAULT_AUDIO_SCRIPT_SETTINGS  # queued
    obs_config.apply_pending_obs_settings()
    assert obs_config.load_audio_script_settings() == settings
    # The stuck-check setting from before it moved to the tray is left alone.
    entry = obs_config.find_script_entry(obs_config.load_scene_collection(), "stuck_output_check.py")
    assert entry["settings"]["stuck_check_enabled"] is True


def test_audio_script_peak_ceiling_is_clamped(fake_obs_config):
    obs_config.save_audio_script_settings(True, 0.0, False)
    assert obs_config.load_audio_script_settings()["peak_ceiling_db"] == obs_config.PEAK_CEILING_MAX_DB
    obs_config.save_audio_script_settings(True, -50, False)
    assert obs_config.load_audio_script_settings()["peak_ceiling_db"] == obs_config.PEAK_CEILING_MIN_DB


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


@pytest.fixture
def sound_library(tmp_path, monkeypatch):
    for name, filename in obs_config.CLIP_SOUND_PRESETS.items():
        (tmp_path / filename).write_bytes(b"RIFF0000WAVE" + name.encode())
    monkeypatch.setattr(obs_config, "CLIP_SOUND_WAV_PATH", str(tmp_path / "replay_buffer_save_sound.wav"))
    monkeypatch.setattr(obs_config, "CLIP_SOUND_LIBRARY_DIR", str(tmp_path / "clip-sounds"))
    return tmp_path


def _pcm_wav(path, value=1000):
    import array
    import wave

    with wave.open(str(path), "wb") as f:
        f.setnchannels(1), f.setsampwidth(2), f.setframerate(48000)
        f.writeframes(array.array("h", [value] * 10).tobytes())
    return path


def test_added_clip_sounds_are_listed_after_the_presets(sound_library):
    ding = _pcm_wav(sound_library / "ding.wav")
    assert obs_config.add_clip_sound(str(ding)) == "ding"
    assert obs_config.add_clip_sound(str(ding)) == "ding"  # the same sound again isn't a second entry
    (sound_library / "x").mkdir()
    other = _pcm_wav(sound_library / "x" / "ding.wav", 5)  # same name, different sound
    assert obs_config.add_clip_sound(str(other)) == "ding (2)"
    scissors = _pcm_wav(sound_library / "Scissors.wav", 7)
    assert obs_config.add_clip_sound(str(scissors)) == "Scissors (2)"  # never shadows a preset
    assert obs_config.clip_sound_names() == ["Scissors", "Flash", "ding", "ding (2)", "Scissors (2)"]
    assert (sound_library / "clip-sounds" / "ding.wav").read_bytes() == ding.read_bytes()  # PCM is copied as-is

    obs_config.replace_clip_sound_wav(obs_config.clip_sound_path("ding (2)"))
    assert obs_config.active_clip_sound_name() == "ding (2)"
    obs_config.remove_clip_sound("ding (2)")
    assert obs_config.clip_sound_names() == ["Scissors", "Flash", "ding", "Scissors (2)"]
    assert obs_config.active_clip_sound_name() is None  # still playing its copy, just not listed
    obs_config.remove_clip_sound("Flash")  # presets can't be removed
    assert "Flash" in obs_config.clip_sound_names()


def test_other_sound_files_are_converted_by_ffmpeg(sound_library):
    mp3 = sound_library / "horn.mp3"
    mp3.write_bytes(b"ID3\x03\x00\x00\x00")
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        with open(cmd[-1], "wb") as f:  # raw PCM: 3 stereo frames and a stray byte
            f.write(array.array("h", [1, -1, 2, -2, 3, -3]).tobytes() + b"x")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert obs_config.add_clip_sound(str(mp3), ffmpeg="ffmpeg", run=run) == "horn"
    assert calls[0][calls[0].index("-i") + 1] == str(mp3)
    assert calls[0][calls[0].index("-f") + 1] == "s16le"  # ClipEm's ffmpeg has no WAV muxer
    rate, samples = _samples(obs_config.clip_sound_path("horn"))
    assert (rate, samples) == (48000, [1, -1, 2, -2, 3, -3])
    assert os.listdir(sound_library / "clip-sounds") == ["horn.wav"]  # no temp file left behind


def test_a_sound_ffmpeg_cant_read_isnt_added(sound_library):
    bad = sound_library / "notes.txt"
    bad.write_text("hello")

    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "notes.txt: Invalid data found when processing input\n"
                                                    "Error opening output files: Invalid argument\n")

    with pytest.raises(ValueError, match="Invalid data found"):  # the cause, not what failed after it
        obs_config.add_clip_sound(str(bad), ffmpeg="ffmpeg", run=run)
    assert os.listdir(sound_library / "clip-sounds") == []


def test_without_ffmpeg_only_wavs_can_be_added(sound_library, monkeypatch):
    monkeypatch.setattr(obs_config, "_find_ffmpeg", lambda: None)
    mp3 = sound_library / "horn.mp3"
    mp3.write_bytes(b"ID3")
    with pytest.raises(ValueError, match="needs ffmpeg"):
        obs_config.add_clip_sound(str(mp3))


def _samples(path):
    import array
    import wave

    with wave.open(str(path), "rb") as f:
        return f.getframerate(), list(array.array("h", f.readframes(f.getnframes())))


def _ramp_wav(path):
    """1 s at 1 kHz of a constant level."""
    import array
    import wave

    with wave.open(str(path), "wb") as f:
        f.setnchannels(1), f.setsampwidth(2), f.setframerate(1000)
        f.writeframes(array.array("h", [1000] * 1000).tobytes())
    return path


def test_a_saved_sound_is_trimmed_at_both_ends_with_fades_and_kept_whole(sound_library):
    name = obs_config.add_clip_sound(str(_ramp_wav(sound_library / "long.wav")))
    assert obs_config.clip_sound_trim(name) == (0.0, None)
    obs_config.use_clip_sound(name, 0.2, 0.45)
    assert obs_config.clip_sound_trim(name) == (0.2, 0.45)
    rate, samples = _samples(sound_library / "replay_buffer_save_sound.wav")
    assert len(samples) == 250
    assert samples[0] == 0 and samples[9] == 900  # 10 ms fade in where the dead air was cut
    assert samples[10:240] == [1000] * 230
    assert samples[240] == 900 and samples[-1] == 0  # and out at the end
    assert len(_samples(obs_config.clip_sound_path(name))[1]) == 1000  # the saved sound stays whole
    assert obs_config.active_clip_sound_name() == name  # recognised as trimmed

    obs_config.use_clip_sound(name, 0.0, None)  # back to all of it
    assert obs_config.clip_sound_trim(name) == (0.0, None)
    assert len(_samples(sound_library / "replay_buffer_save_sound.wav")[1]) == 1000
    assert obs_config.active_clip_sound_name() == name


def test_only_the_cut_ends_fade(sound_library):
    name = obs_config.add_clip_sound(str(_ramp_wav(sound_library / "long.wav")))
    obs_config.use_clip_sound(name, 0.5, None)  # the start trimmed, the end left
    _rate, samples = _samples(sound_library / "replay_buffer_save_sound.wav")
    assert len(samples) == 500 and samples[0] == 0 and samples[-1] == 1000


def test_a_removed_sound_takes_its_trim_with_it(sound_library):
    name = obs_config.add_clip_sound(str(_ramp_wav(sound_library / "long.wav")))
    obs_config.save_clip_sound_trim(name, 0.1, 0.5)
    obs_config.remove_clip_sound(name)
    assert obs_config.add_clip_sound(str(sound_library / "long.wav")) == name
    assert obs_config.clip_sound_trim(name) == (0.0, None)


def test_a_preview_plays_the_trim_without_changing_anything(sound_library, monkeypatch):
    played = []
    monkeypatch.setattr(obs_config, "play_clip_sound", lambda volume, path: played.append(
        (volume, len(_samples(path)[1]))))
    name = obs_config.add_clip_sound(str(_ramp_wav(sound_library / "long.wav")))
    obs_config.preview_clip_sound(name, 0.3, 0.25, 0.5)
    obs_config.preview_clip_sound(name, 0.3)  # as saved: all of it
    assert played == [(0.3, 250), (0.3, 1000)]
    assert obs_config.clip_sound_trim(name) == (0.0, None)
    assert not (sound_library / "replay_buffer_save_sound.wav").exists()


def test_stop_ends_the_preview_and_a_new_play_replaces_it(sound_library, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("winsound has no process to stop")

    class Player:
        def __init__(self, cmd, **_kwargs):
            self.cmd, self.stopped = cmd, False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

    monkeypatch.setattr(subprocess, "Popen", Player)
    monkeypatch.setattr(obs_config.platform, "system", lambda: "Linux")
    wav = str(_pcm_wav(sound_library / "ding.wav"))
    obs_config.play_clip_sound(0.5, wav)
    first = obs_config._players[0]
    obs_config.play_clip_sound(0.5, wav)  # a second Play doesn't talk over the first
    second = obs_config._players[0]
    assert first.stopped and not second.stopped
    obs_config.stop_clip_sound()
    assert second.stopped and obs_config._players == []


def test_an_unreadable_trims_file_means_untrimmed(sound_library):
    name = obs_config.add_clip_sound(str(_ramp_wav(sound_library / "long.wav")))
    (sound_library / "clip-sounds" / "trims.json").write_text("[1, 2]")
    assert obs_config.clip_sound_trim(name) == (0.0, None)
    (sound_library / "clip-sounds" / "trims.json").write_text('{"long": ["x", 1]}')
    assert obs_config.clip_sound_trim(name) == (0.0, None)


def test_a_float_wav_is_converted_without_ffmpeg(sound_library, monkeypatch):
    from conftest import float_wav_bytes

    monkeypatch.setattr(obs_config, "_find_ffmpeg", lambda: None)
    path = sound_library / "f64.wav"
    path.write_bytes(float_wav_bytes(3, "d", 64, [0.5, -0.5]))  # ClipEm's ffmpeg can't decode 64-bit float
    name = obs_config.add_clip_sound(str(path))
    assert _samples(obs_config.clip_sound_path(name)) == (8000, [16383, -16383])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_real_mp3_becomes_a_wav_the_clip_sound_can_scale(sound_library):
    mp3 = sound_library / "beep.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=0.2", str(mp3)], check=True)
    name = obs_config.add_clip_sound(str(mp3), ffmpeg="ffmpeg")
    obs_config.wav_at_volume(obs_config.clip_sound_path(name), 0.5)  # integer PCM - no wave.Error


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


def test_clip_sound_presets(tmp_path, monkeypatch):
    for name, filename in obs_config.CLIP_SOUND_PRESETS.items():
        (tmp_path / filename).write_bytes(b"RIFF0000WAVE" + name.encode())
    target = tmp_path / "replay_buffer_save_sound.wav"
    monkeypatch.setattr(obs_config, "CLIP_SOUND_WAV_PATH", str(target))

    assert obs_config.active_clip_sound_name() is None
    obs_config.ensure_clip_sound_wav()  # a missing sound becomes the default preset
    assert obs_config.active_clip_sound_name() == obs_config.DEFAULT_CLIP_SOUND_PRESET == "Scissors"

    obs_config.replace_clip_sound_wav(obs_config.clip_sound_preset_path("Flash"))
    assert obs_config.active_clip_sound_name() == "Flash"

    custom = tmp_path / "mine.wav"
    custom.write_bytes(b"RIFF0000WAVEmine")
    obs_config.replace_clip_sound_wav(str(custom))
    obs_config.ensure_clip_sound_wav()  # an existing (custom) sound is never replaced
    assert obs_config.active_clip_sound_name() is None
    assert target.read_bytes() == b"RIFF0000WAVEmine"
