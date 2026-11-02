import json

import obs_config
import pytest


def test_update_ini_section_updates_and_appends(tmp_path):
    ini = tmp_path / "basic.ini"
    ini.write_text("[General]\nName=x\n\n[Video]\nBaseCX=1\n")
    obs_config.update_ini_section(str(ini), "Video", {"BaseCX": 1920, "FPSCommon": 60})
    obs_config.update_ini_section(str(ini), "AdvOut", {"RecRBTime": 30})
    assert obs_config.read_ini_section("Video", str(ini)) == {"BaseCX": "1920", "FPSCommon": "60"}
    assert obs_config.read_ini_section("AdvOut", str(ini)) == {"RecRBTime": "30"}
    assert obs_config.read_ini_section("General", str(ini)) == {"Name": "x"}


def test_find_ini_section_bounds():
    lines = ["[A]\n", "k=v\n", "[B]\n", "x=y\n"]
    assert obs_config.find_ini_section(lines, "A") == (0, 2)
    assert obs_config.find_ini_section(lines, "B") == (2, 4)
    assert obs_config.find_ini_section(lines, "C") == (None, None)


def test_shipped_websocket_password_is_replaced(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server_port": 4455, "server_password": obs_config.SHIPPED_OBS_WEBSOCKET_PASSWORD}))
    monkeypatch.setattr(obs_config, "OBS_WEBSOCKET_CONFIG_PATH", str(cfg))
    settings = obs_config.load_obs_websocket_settings()
    assert settings["password"] != obs_config.SHIPPED_OBS_WEBSOCKET_PASSWORD
    assert len(settings["password"]) == 24
    assert json.loads(cfg.read_text())["server_password"] == settings["password"]
    # Idempotent once replaced.
    assert obs_config.load_obs_websocket_settings() == settings


def test_save_websocket_settings_validates(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server_port": 4455, "server_password": "a"}))
    monkeypatch.setattr(obs_config, "OBS_WEBSOCKET_CONFIG_PATH", str(cfg))
    for port, password in ((0, "p"), (70000, "p"), (4455, "")):
        try:
            obs_config.save_obs_websocket_settings(port, password)
        except ValueError:
            continue
        raise AssertionError(f"accepted {port!r}/{password!r}")
    obs_config.save_obs_websocket_settings("4456", "new")
    assert json.loads(cfg.read_text()) == {"server_port": 4456, "server_password": "new"}


def test_load_clip_folder(tmp_path, monkeypatch):
    ini = tmp_path / "basic.ini"
    monkeypatch.setattr(obs_config, "BASIC_INI", str(ini))
    ini.write_text("[SimpleOutput]\nFilePath={{CLIPS_DIR}}\n")
    assert obs_config.load_clip_folder() == obs_config.DEFAULT_CLIPS_DIR
    ini.write_text("[SimpleOutput]\nFilePath=C:\\\\Users\\\\me\\\\Clips\n")
    assert obs_config.load_clip_folder() == "C:\\Users\\me\\Clips"


def test_leaked_developer_clips_path_reads_as_unset(tmp_path, monkeypatch):
    ini = tmp_path / "basic.ini"
    ini.write_text(f"[SimpleOutput]\nFilePath={obs_config.LEAKED_CLIPS_DIRS[0]}\n")
    monkeypatch.setattr(obs_config, "BASIC_INI", str(ini))
    assert obs_config.load_clip_folder() == obs_config.DEFAULT_CLIPS_DIR


def _write_wav(path, width, samples):
    import wave

    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(width)
        f.setframerate(8000)
        f.writeframes(samples)


def test_wav_at_volume_scales_every_integer_pcm_width(tmp_path):
    import array
    import io
    import wave

    cases = {
        1: (bytes([0, 128, 255]), bytes([64, 128, 191])),
        2: (array.array("h", [-32768, 0, 32766]).tobytes(), array.array("h", [-16384, 0, 16383]).tobytes()),
        3: (b"".join(v.to_bytes(3, "little", signed=True) for v in (-8388608, 1000, 8388606)),
            b"".join(v.to_bytes(3, "little", signed=True) for v in (-4194304, 500, 4194303))),
        4: (array.array("i", [-2**31, 10, 2**31 - 2]).tobytes(), array.array("i", [-2**30, 5, 2**30 - 1]).tobytes()),
    }
    for width, (samples, halved) in cases.items():
        path = tmp_path / f"{width}.wav"
        _write_wav(path, width, samples)
        with wave.open(io.BytesIO(obs_config.wav_at_volume(str(path), 0.5)), "rb") as f:
            assert (f.getsampwidth(), f.readframes(3)) == (width, halved)
        with wave.open(io.BytesIO(obs_config.wav_at_volume(str(path), 1.5)), "rb") as f:
            assert f.readframes(3) == samples  # clamped to full volume


def test_wav_at_volume_rejects_what_it_cant_scale(tmp_path):
    import wave

    import pytest

    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF")
    with pytest.raises((wave.Error, EOFError)):
        obs_config.wav_at_volume(str(bad), 0.5)


def _scene_with_reexport(tmp_path, monkeypatch, settings):
    path = tmp_path / "scenes.json"
    path.write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "/x/Scripts/clip_reexport.py", "settings": settings},
    ]}}))
    monkeypatch.setattr(obs_config, "SCENE_COLLECTION_JSON", str(path))
    return path


def test_discord_target_moves_from_10_to_20_mb_once(tmp_path, monkeypatch):
    path = _scene_with_reexport(tmp_path, monkeypatch, {"target_mb": 10.0, "reexport_enabled": True})
    assert obs_config.migrate_discord_target_size() is True
    settings = json.loads(path.read_text())["modules"]["scripts-tool"][0]["settings"]
    assert settings["target_mb"] == 20.0 and settings["reexport_enabled"] is True
    settings["target_mb"] = 10.0  # chosen again by the user afterwards
    path.write_text(json.dumps({"modules": {"scripts-tool": [{"path": "clip_reexport.py", "settings": settings}]}}))
    assert obs_config.migrate_discord_target_size() is False
    assert json.loads(path.read_text())["modules"]["scripts-tool"][0]["settings"]["target_mb"] == 10.0


def test_discord_target_keeps_a_custom_size(tmp_path, monkeypatch):
    path = _scene_with_reexport(tmp_path, monkeypatch, {"target_mb": 8.0})
    assert obs_config.migrate_discord_target_size() is False
    assert json.loads(path.read_text())["modules"]["scripts-tool"][0]["settings"]["target_mb"] == 8.0
    monkeypatch.setattr(obs_config, "SCENE_COLLECTION_JSON", str(tmp_path / "missing.json"))
    assert obs_config.migrate_discord_target_size() is False


def test_split_tracks_from_before_tray_recording_move_to_the_new_layout_once(tmp_path, monkeypatch):
    import setup

    path = _scene_with_reexport(tmp_path, monkeypatch, {"split_tracks_enabled": True})
    calls = []
    monkeypatch.setattr(setup, "save_split_tracks", lambda enabled: calls.append(enabled))
    assert obs_config.migrate_split_tracks_layout() is True and calls == [True]
    path.write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "clip_reexport.py", "settings": {"split_tracks_enabled": True, "tray_tracks_enabled": False}}]}}))
    monkeypatch.setattr(obs_config, "app_capture_supported", lambda: False)
    assert obs_config.migrate_split_tracks_layout() is False  # already decided (e.g. macOS)
    path.write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "clip_reexport.py", "settings": {"split_tracks_enabled": False}}]}}))
    assert obs_config.migrate_split_tracks_layout() is False and calls == [True]


def test_split_tracks_turn_app_tracks_back_on_once_app_capture_is_available(tmp_path, monkeypatch):
    import setup

    _scene_with_reexport(tmp_path, monkeypatch, {"split_tracks_enabled": True, "tray_tracks_enabled": False})
    calls = []
    monkeypatch.setattr(setup, "save_split_tracks", lambda enabled: calls.append(enabled))
    monkeypatch.setattr(obs_config, "app_capture_supported", lambda: False)
    assert obs_config.migrate_split_tracks_layout() is False and calls == []
    monkeypatch.setattr(obs_config, "app_capture_supported", lambda: True)
    assert obs_config.migrate_split_tracks_layout() is True and calls == [True]


def test_only_audio_tracks_at_the_old_default_move_to_the_new_bitrate(fake_obs_config):
    ini = fake_obs_config / "basic.ini"
    ini.write_text("[AdvOut]\nTrack1Bitrate=160\nTrack2Bitrate=320\nTrack3Bitrate=nonsense\n")
    assert obs_config.migrate_audio_bitrate() is True
    adv = obs_config.read_ini_section("AdvOut")
    new = str(obs_config.DEFAULT_AUDIO_TRACK_KBPS)
    assert adv["Track1Bitrate"] == new and adv["Track6Bitrate"] == new  # unset counts as the old default
    assert adv["Track2Bitrate"] == "320"  # set in OBS itself


def test_the_audio_bitrate_migration_is_done_once(fake_obs_config):
    ini = fake_obs_config / "basic.ini"
    ini.write_text("[AdvOut]\n" + "".join(f"Track{n}Bitrate=320\n" for n in range(1, 7)))
    assert obs_config.migrate_audio_bitrate() is False


def test_the_audio_bitrate_migration_needs_a_config(fake_obs_config):
    (fake_obs_config / "basic.ini").unlink()
    assert obs_config.migrate_audio_bitrate() is False


def test_no_config_means_the_default_hotkey_and_saving_says_so(fake_obs_config):
    (fake_obs_config / "basic.ini").unlink()
    assert obs_config.load_clip_hotkey() == obs_config.DEFAULT_CLIP_HOTKEY
    with pytest.raises(FileNotFoundError):
        obs_config.save_clip_hotkey(True, False, False, "F9")


@pytest.mark.parametrize("ini", [
    "[General]\nName=x\n",  # no [Hotkeys]
    "[Hotkeys]\nOther={}\n",  # no clip hotkey
    "[Hotkeys]\nReplayBuffer={}\n",  # nothing for this action
    "[Hotkeys]\nReplayBuffer={broken\n",
    "[Hotkeys]\nReplayBuffer=[1, 2]\n",
])
def test_a_config_without_a_usable_hotkey_is_the_default(fake_obs_config, ini):
    (fake_obs_config / "basic.ini").write_text(ini)
    assert obs_config.load_clip_hotkey() == obs_config.DEFAULT_CLIP_HOTKEY


def test_a_hotkey_that_isnt_an_object_is_the_default(fake_obs_config):
    (fake_obs_config / "basic.ini").write_text("[Hotkeys]\nReplayBuffer=5\n")
    assert obs_config.load_clip_hotkey() == obs_config.DEFAULT_CLIP_HOTKEY


def test_saving_a_hotkey_adds_its_section_and_keeps_the_rest(fake_obs_config):
    ini = fake_obs_config / "basic.ini"
    ini.write_text("[General]\nName=DefaultPTM")  # no newline at the end
    obs_config.save_clip_hotkey(True, False, True, "F9")
    assert obs_config.load_clip_hotkey() == (True, False, True, "F9")
    assert obs_config.read_ini_section("General")["Name"] == "DefaultPTM"


def test_saving_a_hotkey_adds_its_line_to_the_section(fake_obs_config):
    (fake_obs_config / "basic.ini").write_text("[Hotkeys]\nOther=1\n\n[Next]\nA=1\n")
    obs_config.save_clip_hotkey(False, True, False, "F8")
    assert obs_config.load_clip_hotkey() == (False, True, False, "F8")
    assert obs_config.read_ini_section("Hotkeys")["Other"] == "1"
    assert obs_config.read_ini_section("Next")["A"] == "1"


def test_saving_an_unknown_key_changes_nothing(fake_obs_config):
    before = (fake_obs_config / "basic.ini").read_text()
    with pytest.raises(ValueError):
        obs_config.save_clip_hotkey(False, False, False, "F25")
    assert (fake_obs_config / "basic.ini").read_text() == before


def test_with_post_clip_on_a_broken_scene_collection_stops_the_hotkey_saving(fake_obs_config):
    obs_config.clipem_common.save_run_config({"post_clip": {"enabled": True}})
    (fake_obs_config / "scenes.json").write_text("{broken")
    assert obs_config.load_clip_hotkey() == obs_config.DEFAULT_CLIP_HOTKEY
    before = (fake_obs_config / "basic.ini").read_text()
    with pytest.raises(ValueError):
        obs_config.save_clip_hotkey(True, False, False, "F9")
    assert (fake_obs_config / "basic.ini").read_text() == before


def test_with_post_clip_on_a_missing_scene_collection_stops_the_hotkey_saving(fake_obs_config):
    obs_config.clipem_common.save_run_config({"post_clip": {"enabled": True}})
    (fake_obs_config / "scenes.json").unlink()
    with pytest.raises(FileNotFoundError):
        obs_config.save_clip_hotkey(True, False, False, "F9")


def test_with_post_clip_off_a_broken_scene_collection_doesnt_matter(fake_obs_config):
    (fake_obs_config / "scenes.json").write_text("{broken")
    obs_config.save_clip_hotkey(True, False, False, "F9")
    assert obs_config.load_clip_hotkey() == (True, False, False, "F9")


def test_saved_keys_that_make_no_sense_are_the_default(fake_obs_config):
    obs_config.clipem_common.save_run_config({"post_clip": {"enabled": True}})
    (fake_obs_config / "scenes.json").write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "/x/post_clip.py", "settings": {"post_clip_hotkey": 5}},
    ]}}))
    assert obs_config.load_clip_hotkey() == obs_config.DEFAULT_CLIP_HOTKEY
