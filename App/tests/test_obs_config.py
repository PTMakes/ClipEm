import json

import obs_config


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
