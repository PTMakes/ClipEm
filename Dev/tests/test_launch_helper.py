import json
import os

import obs_config
import portable_launch_helper as helper
import pytest


@pytest.fixture
def root(tmp_path):
    """A minimal install: ClipEm/App/{Shared,Linux,Windows}."""
    app = tmp_path / "ClipEm" / "App"
    config = app / "Shared" / "OBS-Config"
    (config / "basic" / "profiles" / "DefaultPTM").mkdir(parents=True)
    (config / "basic" / "profiles" / "DefaultPTM" / "basic.ini").write_text(
        "[AdvOut]\nRecFilePath={{CLIPS_DIR}}\n[SimpleOutput]\nFilePath={{CLIPS_DIR}}\n"
    )
    (config / "basic" / "scenes").mkdir()
    (config / "basic" / "scenes" / "DefaultPTM.json").write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "../../Scripts/stuck_output_check.py", "settings": {}},
        {"path": "C:/old/Scripts/replay_buffer_save_sound.py",
         "settings": {"audio_file": "C:/old/Scripts/replay_buffer_save_sound.wav"}},
    ]}}))
    (config / "user.ini").write_text("[Python]\nPath64bit=../../Python\n")
    (app / "Shared" / "Scripts").mkdir()
    (app / "Linux").mkdir()
    (app / "Windows" / "Python").mkdir(parents=True)
    (app / "Windows" / "Python" / "python.exe").write_text("")
    return app


def _is_link(path):
    return path.is_symlink() or helper._is_junction(str(path))  # junctions on Windows


def _ini_value(path):
    """How basic.ini holds a path: OBS's ini files double Windows' backslashes."""
    return str(path).replace("\\", "\\\\") if os.name == "nt" else str(path)


def test_links_config_and_scripts(root):
    helper.prepare(str(root / "Linux"))
    link = root / "Linux" / "config" / "obs-studio"
    assert _is_link(link)
    assert os.path.realpath(link) == os.path.realpath(root / "Shared" / "OBS-Config")
    assert os.path.realpath(root / "Linux" / "Scripts") == os.path.realpath(root / "Shared" / "Scripts")
    helper.prepare(str(root / "Linux"))  # idempotent
    assert _is_link(link)


def test_old_layout_config_link_becomes_a_folder(root):
    old_link = root / "Linux" / "config"
    old_link.symlink_to(root / "Shared" / "OBS-Config", target_is_directory=True)
    helper.prepare(str(root / "Linux"))
    assert not old_link.is_symlink() and old_link.is_dir()
    assert _is_link(old_link / "obs-studio")


def test_real_directory_in_the_way_is_left_alone(root):
    real = root / "Linux" / "config" / "obs-studio"
    real.mkdir(parents=True)
    (real / "keep.txt").write_text("user data")
    helper.prepare(str(root / "Linux"))
    assert not real.is_symlink()
    assert (real / "keep.txt").read_text() == "user data"


def test_clips_dir_placeholder_is_filled_once(root):
    helper.prepare(str(root / "Linux"))
    ini = (root / "Shared" / "OBS-Config" / "basic" / "profiles" / "DefaultPTM" / "basic.ini").read_text()
    clips = _ini_value(root.parent / "Clips")
    assert "{{CLIPS_DIR}}" not in ini
    assert f"RecFilePath={clips}" in ini
    assert os.path.isdir(clips)


def test_script_paths_become_absolute_and_new_scripts_are_registered(root):
    helper.prepare(str(root / "Linux"))
    data = json.loads((root / "Shared" / "OBS-Config" / "basic" / "scenes" / "DefaultPTM.json").read_text())
    scripts_dir = str(root / "Shared" / "Scripts")
    entries = {os.path.basename(e["path"]): e for e in data["modules"]["scripts-tool"]}
    assert entries["stuck_output_check.py"]["path"] == os.path.join(scripts_dir, "stuck_output_check.py")
    wav = entries["replay_buffer_save_sound.py"]["settings"]["audio_file"]
    assert wav == os.path.join(scripts_dir, "replay_buffer_save_sound.wav")
    assert "clip_reexport.py" in entries  # ADDED_SCRIPTS


def test_windows_points_obs_at_the_bundled_python(root):
    helper.prepare(str(root / "Windows"))
    user_ini = str(root / "Shared" / "OBS-Config" / "user.ini")
    expected = str(root / "Windows" / "Python").replace("\\", "/")
    assert obs_config.read_ini_section("Python", user_ini)["Path64bit"] == expected


def test_windows_python_found_in_a_nested_folder(root):
    nested = root / "Windows" / "Python" / "python-3.12.10-embed-amd64"
    nested.mkdir()
    (root / "Windows" / "Python" / "python.exe").unlink()
    (nested / "python.exe").write_text("")
    helper.prepare(str(root / "Windows"))
    user_ini = str(root / "Shared" / "OBS-Config" / "user.ini")
    assert obs_config.read_ini_section("Python", user_ini)["Path64bit"] == str(nested).replace("\\", "/")


def test_linux_leaves_the_windows_python_path_alone(root):
    helper.prepare(str(root / "Linux"))
    assert "Path64bit=../../Python" in (root / "Shared" / "OBS-Config" / "user.ini").read_text()


def test_leaked_developer_clips_path_is_repaired(root):
    """Releases up to 1.2.7 shipped the developer's clips path instead of the
    placeholder; an install still carrying it gets the default folder."""
    ini = root / "Shared" / "OBS-Config" / "basic" / "profiles" / "DefaultPTM" / "basic.ini"
    leaked = helper.LEAKED_CLIPS_DIRS[0]
    ini.write_text(f"[AdvOut]\nRecFilePath={leaked}\n[SimpleOutput]\nFilePath={leaked}\n")
    helper.prepare(str(root / "Linux"))
    clips = _ini_value(root.parent / "Clips")
    assert ini.read_text() == f"[AdvOut]\nRecFilePath={clips}\n[SimpleOutput]\nFilePath={clips}\n"


def test_a_chosen_clips_folder_is_left_alone(root):
    ini = root / "Shared" / "OBS-Config" / "basic" / "profiles" / "DefaultPTM" / "basic.ini"
    ini.write_text("[SimpleOutput]\nFilePath=/media/usb/MyClips\n")
    helper.prepare(str(root / "Linux"))
    assert "FilePath=/media/usb/MyClips" in ini.read_text()
    assert not (root.parent / "Clips").exists()


@pytest.mark.usefixtures("links")
def test_a_link_to_the_wrong_place_is_repointed_and_a_right_one_kept(tmp_path):
    right, wrong = tmp_path / "right", tmp_path / "wrong"
    right.mkdir()
    wrong.mkdir()
    (wrong / "keep").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(wrong)
    helper._link_dir(str(link), str(right))
    assert os.path.realpath(link) == os.path.realpath(right) and (wrong / "keep").exists()
    before = os.lstat(link).st_ino
    helper._link_dir(str(link), str(right))
    assert os.lstat(link).st_ino == before


def test_no_ini_means_no_clips_folder_to_fill(tmp_path):
    helper._fill_clips_dir(str(tmp_path), str(tmp_path / "Clips"))
    assert os.listdir(tmp_path) == []


def test_a_real_folder_isnt_a_junction(tmp_path):
    assert not helper._is_junction(str(tmp_path))
