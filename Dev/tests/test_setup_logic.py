import json
import os

import pytest
import setup


def test_bitrate_math_round_trips():
    kbps = setup.compute_video_bitrate_for_filesize(10, 60)
    assert kbps == int(10 * 8000 / 60 - setup.AUDIO_BITRATE_KBPS)
    assert setup.compute_filesize_for_bitrate(kbps, 60, setup.AUDIO_BITRATE_KBPS) == pytest.approx(10, rel=0.01)
    assert setup.compute_video_bitrate_for_filesize(1, 600) == setup.MIN_VIDEO_BITRATE_KBPS


@pytest.mark.parametrize("label,expected", [("~", "OBS_KEY_ASCIITILDE"), ("f12", "OBS_KEY_F12"), ("a", "OBS_KEY_A")])
def test_hotkey_labels(label, expected):
    assert setup._hotkey_label_to_obs_key(label) == expected
    assert setup._obs_key_to_label(expected) == label.upper()


def test_hotkey_rejects_unknown():
    with pytest.raises(ValueError):
        setup._hotkey_label_to_obs_key("F25")


def test_verify_checksums_resolves_shared_and_local_bases(tmp_path, monkeypatch):
    app = tmp_path / "App"
    shared = app / "Shared"
    (shared / "Installer").mkdir(parents=True)
    (shared / "Installer" / "x.py").write_text("print(1)")
    (app / "Linux" / "obs-install").mkdir(parents=True)
    (app / "Linux" / "obs-install" / "obs").write_text("bin")
    from clipem_common import sha256_of_file

    (shared / "checksums.json").write_text(json.dumps({"Installer/x.py": sha256_of_file(shared / "Installer" / "x.py")}))
    (app / "checksums_local.json").write_text(
        json.dumps({"Linux/obs-install/obs": sha256_of_file(app / "Linux" / "obs-install" / "obs")})
    )
    monkeypatch.setattr(setup, "APP_DIR", str(app))
    monkeypatch.setattr(setup, "SHARED_DIR", str(shared))
    monkeypatch.setattr(setup, "CHECKSUMS_MANIFEST_PATH", str(shared / "checksums.json"))
    monkeypatch.setattr(setup, "LOCAL_CHECKSUMS_PATH", str(app / "checksums_local.json"))
    ok, message = setup.verify_checksums()
    assert ok, message
    (shared / "Installer" / "x.py").write_text("tampered")
    ok, message = setup.verify_checksums()
    assert ok is False and "Shared/Installer/x.py" in message
    os.remove(shared / "checksums.json")


def _fake_scene_collection(monkeypatch, tmp_path, monitor_id):
    data = {"sources": [{"id": "monitor_capture", "settings": {"monitor": 0, "monitor_id": monitor_id}}]}
    monkeypatch.setattr(setup.obs_config.clipem_common, "RUN_CONFIG_PATH", str(tmp_path / "run_config.json"))
    monkeypatch.setattr(setup, "load_scene_collection", lambda: data)
    monkeypatch.setattr(setup, "save_scene_collection", lambda new: data.update(new))
    return data["sources"][0]["settings"]


DISPLAYS = [
    {"name": "\\\\.\\DISPLAY1", "width": 1920, "height": 1080, "primary": False, "device_id": "\\\\?\\DISPLAY#AAA"},
    {"name": "\\\\.\\DISPLAY2", "width": 2560, "height": 1440, "primary": True, "device_id": "\\\\?\\DISPLAY#BBB"},
]


def test_capture_monitor_repointed_from_shipped_id_to_primary(monkeypatch, tmp_path):
    settings = _fake_scene_collection(monkeypatch, tmp_path, "\\\\?\\DISPLAY#FROM_ANOTHER_MACHINE")
    monkeypatch.setattr(setup, "list_displays", lambda: DISPLAYS)
    setup.ensure_windows_capture_monitor()
    assert settings["monitor_id"] == "\\\\?\\DISPLAY#BBB"
    assert settings["monitor"] == 1
    # Remembered for the tray's capture recovery.
    assert setup.obs_config.load_setup_capture_display() == {
        "source_id": "monitor_capture",
        "settings": {"monitor": 1, "monitor_id": "\\\\?\\DISPLAY#BBB"},
        "user_changed": False,
    }


def test_capture_monitor_left_alone_when_it_names_a_connected_display(monkeypatch, tmp_path):
    settings = _fake_scene_collection(monkeypatch, tmp_path, "\\\\?\\DISPLAY#AAA")
    monkeypatch.setattr(setup, "list_displays", lambda: DISPLAYS)
    setup.ensure_windows_capture_monitor()
    assert settings["monitor_id"] == "\\\\?\\DISPLAY#AAA"


@pytest.mark.parametrize(
    "width,height,expected",
    [(1920, 1080, 18000), (1280, 720, 8000), (2560, 1440, 32000), (3840, 2160, setup.BITRATE_MAX_KBPS)],
)
def test_recommended_recording_bitrate_scales_from_1080p(width, height, expected):
    assert setup.recommended_recording_bitrate(width, height) == expected


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _bitrate_follower(bitrate):
    import gui

    stub = type("Stub", (), {})()
    stub.controller = {"bitrate_for_new_resolution": setup.bitrate_for_new_resolution}
    stub.bitrate_var = _Var(bitrate)
    stub._bitrate_resolution = (1920, 1080)
    return stub, lambda w, h: gui.Wizard._follow_resolution_with_bitrate(stub, w, h)


def test_bitrate_follows_resolution_while_still_the_recommendation():
    stub, follow = _bitrate_follower(18000)
    follow(1280, 720)
    assert stub.bitrate_var.get() == 8000
    follow(2560, 1440)
    assert stub.bitrate_var.get() == 32000


def test_bitrate_chosen_by_user_is_kept_when_resolution_changes():
    stub, follow = _bitrate_follower(12000)
    follow(1280, 720)
    assert stub.bitrate_var.get() == 12000


def test_cli_offers_resolution_scaled_bitrate(monkeypatch):
    saved = {}
    monkeypatch.setattr(setup, "list_displays", lambda: [])
    monkeypatch.setattr(setup, "load_video_settings", lambda: {"width": 1920, "height": 1080, "fps": 30})
    monkeypatch.setattr(setup, "load_recording_bitrate", lambda: 18000)
    monkeypatch.setattr(setup, "load_replay_buffer_seconds", lambda: 60)
    monkeypatch.setattr(setup, "ask_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(setup, "save_video_settings", lambda *a: None)
    monkeypatch.setattr(setup, "save_replay_buffer_seconds", lambda s: None)
    monkeypatch.setattr(setup, "save_recording_bitrate", lambda kbps: saved.update(kbps=kbps))
    # Resolution index 2 of resolution_choices(1920, 1080) is 1280x720; the
    # rest of the prompts take their defaults.
    answers = iter(["2", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert setup.resolution_choices(1920, 1080)[2] == (1280, 720)
    setup.configure_video_quality()
    assert saved["kbps"] == 8000


def test_only_the_running_pythons_pth_file_is_edited(tmp_path):
    """A reinstall over a Python 3.10 install leaves python310._pth next to
    3.12's - the one this interpreter doesn't read must be left alone."""
    (tmp_path / "python310._pth").write_text("python310.zip\n.\n#import site\n")
    (tmp_path / "python312._pth").write_text("python312.zip\n.\n#import site\n")
    assert setup.own_pth_path(str(tmp_path), (3, 12)) == str(tmp_path / "python312._pth")
    assert setup.own_pth_path(str(tmp_path), (3, 13)) is None


def test_pip_failure_shows_pips_own_output(capsys):
    import sys as _sys

    import pytest as _pytest

    with _pytest.raises(RuntimeError) as exc:
        setup.run_pip([_sys.executable, "-c", "import sys; print('ERROR: No matching distribution for Pillow'); sys.exit(1)"])
    assert "No matching distribution for Pillow" in str(exc.value)
    assert "No matching distribution for Pillow" in capsys.readouterr().out


def test_packages_install_from_the_offline_wheelhouse_first(tmp_path, monkeypatch):
    import clipem_common

    calls = []

    def run_pip(args):
        calls.append(args)
        if "--no-index" in args and fail_offline["value"]:
            raise RuntimeError("pip install failed: No matching distribution")

    fail_offline = {"value": False}
    monkeypatch.setattr(clipem_common, "run_pip", run_pip)
    monkeypatch.setattr(clipem_common, "wheelhouse_dir", lambda: str(tmp_path / "wheelhouse"))
    base = ["python", "-m", "pip", "install"]

    clipem_common.install_requirements(base, "req.txt")  # no wheelhouse: straight from PyPI
    assert calls == [base + ["-r", "req.txt"]]

    (tmp_path / "wheelhouse").mkdir()
    calls.clear()
    clipem_common.install_requirements(base, "req.txt")
    assert calls == [base + ["--no-index", "--find-links", str(tmp_path / "wheelhouse"), "-r", "req.txt"]]

    fail_offline["value"] = True  # e.g. an update changed a pin the zip doesn't have
    calls.clear()
    clipem_common.install_requirements(base, "req.txt")
    assert calls[-1] == base + ["-r", "req.txt"] and len(calls) == 2


def _checksums(monkeypatch, tmp_path):
    app = tmp_path / "App"
    shared = app / "Shared"
    shared.mkdir(parents=True)
    monkeypatch.setattr(setup, "APP_DIR", str(app))
    monkeypatch.setattr(setup, "SHARED_DIR", str(shared))
    monkeypatch.setattr(setup, "CHECKSUMS_MANIFEST_PATH", str(shared / "checksums.json"))
    monkeypatch.setattr(setup, "LOCAL_CHECKSUMS_PATH", str(app / "checksums_local.json"))
    return app, shared


def test_no_manifest_at_all_is_nothing_to_check(tmp_path, monkeypatch):
    _checksums(monkeypatch, tmp_path)
    ok, message = setup.verify_checksums()
    assert ok is None and "skipping" in message


def test_a_missing_file_is_reported_as_missing(tmp_path, monkeypatch):
    _app, shared = _checksums(monkeypatch, tmp_path)
    (shared / "checksums.json").write_text(json.dumps({"gone.py": "0" * 64}))
    ok, message = setup.verify_checksums()
    assert ok is False and "1 missing" in message and "missing:  Shared/gone.py" in message


def test_a_custom_clip_sound_isnt_corruption(tmp_path, monkeypatch):
    _app, shared = _checksums(monkeypatch, tmp_path)
    (shared / "ok.py").write_text("x")
    from clipem_common import sha256_of_file

    (shared / "checksums.json").write_text(json.dumps({
        "ok.py": sha256_of_file(shared / "ok.py"), "Scripts/replay_buffer_save_sound.wav": "0" * 64}))
    ok, message = setup.verify_checksums()
    assert ok is True and "1 files verified" in message


def test_a_long_list_of_problems_is_cut_short(tmp_path, monkeypatch):
    _app, shared = _checksums(monkeypatch, tmp_path)
    for n in range(12):
        (shared / f"changed{n}").write_text("x")
    manifest = {f"changed{n}": "0" * 64 for n in range(12)} | {f"gone{n}": "0" * 64 for n in range(12)}
    (shared / "checksums.json").write_text(json.dumps(manifest))
    _ok, message = setup.verify_checksums()
    assert message.count("changed:") == 10 and message.count("missing:") == 10 and "  ..." in message
    assert "12 changed, 12 missing (of 24 files checked)" in message


def test_any_cut_short_list_says_so(tmp_path, monkeypatch):
    _app, shared = _checksums(monkeypatch, tmp_path)
    (shared / "checksums.json").write_text(json.dumps({f"gone{n}": "0" * 64 for n in range(15)}))
    _ok, message = setup.verify_checksums()
    assert message.count("missing:") == 10 and "  ..." in message


def test_the_result_is_printed_where_it_belongs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "verify_checksums", lambda: (False, "bad"))
    setup.report_checksums()
    assert capsys.readouterr().err.strip() == "bad"
    monkeypatch.setattr(setup, "verify_checksums", lambda: (True, "good"))
    setup.report_checksums()
    assert capsys.readouterr().out.strip() == "good"
    monkeypatch.setattr(setup, "verify_checksums", lambda: (None, "skipped"))
    setup.report_checksums()
    assert capsys.readouterr().out.strip() == "skipped"


@pytest.fixture
def basic_ini(tmp_path, monkeypatch):
    ini = tmp_path / "basic.ini"
    monkeypatch.setattr(setup, "BASIC_INI", str(ini))
    monkeypatch.setattr(setup.obs_config, "BASIC_INI", str(ini))
    return ini


def test_video_settings_round_trip(basic_ini):
    basic_ini.write_text("[Video]\nFPSType=1\n\n[Other]\nA=1\n")
    setup.save_video_settings(2560, 1440, 1920, 1080, 60)
    assert setup.load_video_settings() == {"width": 1920, "height": 1080, "fps": 60}
    text = basic_ini.read_text()
    for line in ("BaseCX=2560", "BaseCY=1440", "OutputCX=1920", "OutputCY=1080", "FPSType=0", "FPSInt=60", "FPSDen=1"):
        assert line in text
    assert "[Other]\nA=1" in text


def test_video_settings_without_a_config(basic_ini, capsys):
    defaults = {"width": 1920, "height": 1080, "fps": 30}
    assert setup.load_video_settings() == defaults
    setup.save_video_settings(1, 2, 3, 4, 5)
    assert "Config file not found" in capsys.readouterr().err and not basic_ini.exists()
    basic_ini.write_text("[General]\nName=x\n")
    assert setup.load_video_settings() == defaults
    basic_ini.write_text("[Video]\nOutputCX=wide\n")
    assert setup.load_video_settings() == defaults


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """The scene collection, read and written through setup's own functions."""
    path = tmp_path / "scene.json"

    def load():
        return json.loads(path.read_text()) if path.exists() else None

    monkeypatch.setattr(setup, "load_scene_collection", load)
    monkeypatch.setattr(setup, "save_scene_collection", lambda data: path.write_text(json.dumps(data)))
    return path


AUDIO_SCENE = {"DesktopAudioDevice1": {"id": "wasapi_output_capture", "settings": {"device_id": "default"}},
               "AuxAudioDevice1": {"id": "wasapi_input_capture"}}


def test_audio_devices_go_to_their_own_sources(scene):
    scene.write_text(json.dumps(AUDIO_SCENE))
    setup.save_audio_devices("speakers", "mic")
    saved = json.loads(scene.read_text())
    assert saved["DesktopAudioDevice1"]["settings"]["device_id"] == "speakers"
    assert saved["AuxAudioDevice1"]["settings"]["device_id"] == "mic"


def test_one_audio_source_missing_still_sets_the_other(scene, capsys):
    scene.write_text(json.dumps({"AuxAudioDevice1": {}}))
    setup.save_audio_devices("speakers", "mic")
    assert json.loads(scene.read_text())["AuxAudioDevice1"]["settings"]["device_id"] == "mic"
    assert "'DesktopAudioDevice1' (output) not found" in capsys.readouterr().err


def test_no_device_chosen_leaves_the_source_alone(scene, capsys):
    scene.write_text(json.dumps(AUDIO_SCENE))
    before = scene.read_text()
    setup.save_audio_devices("", None)
    assert scene.read_text() == before
    assert "No audio device entries were updated" in capsys.readouterr().err


def test_no_scene_collection_cant_set_audio_devices(scene, capsys):
    setup.save_audio_devices("speakers", "mic")
    assert "can't set audio devices" in capsys.readouterr().err and not scene.exists()


def _sound_scene(scene, settings=None):
    entry = {"path": "/x/replay_buffer_save_sound.py"}
    if settings is not None:
        entry["settings"] = settings
    scene.write_text(json.dumps({"modules": {"scripts-tool": [entry]}}))


def test_clip_sound_settings_round_trip(scene):
    _sound_scene(scene)
    assert setup.load_clip_sound_enabled() is True and setup.load_clip_sound_volume() == 0.5
    setup.save_clip_sound_enabled(False)
    setup.save_clip_sound_volume(0.25)
    assert setup.load_clip_sound_enabled() is False and setup.load_clip_sound_volume() == 0.25


@pytest.mark.parametrize("volume, saved", [(-1, 0.0), (0.0, 0.0), (1.0, 1.0), (3, 1.0)])
def test_clip_sound_volume_stays_in_range(scene, volume, saved):
    _sound_scene(scene)
    setup.save_clip_sound_volume(volume)
    assert setup.load_clip_sound_volume() == saved


def test_a_broken_clip_sound_volume_is_the_default(scene):
    _sound_scene(scene, {"audio_volume": "loud"})
    assert setup.load_clip_sound_volume() == 0.5


def test_without_the_sound_script_nothing_is_saved(scene, capsys):
    scene.write_text(json.dumps({"modules": {"scripts-tool": []}}))
    before = scene.read_text()
    assert setup.load_clip_sound_enabled() is True and setup.load_clip_sound_volume() == 0.5
    setup.save_clip_sound_enabled(False)
    setup.save_clip_sound_volume(0.1)
    assert scene.read_text() == before and capsys.readouterr().err.count("entry not found") == 2


def test_a_missing_sound_file_isnt_copied(monkeypatch, capsys):
    monkeypatch.setattr(setup.obs_config, "replace_clip_sound_wav", lambda path: pytest.fail("nothing to copy"))
    setup.replace_clip_sound_wav("/gone.wav")
    setup.replace_clip_sound_wav("")
    assert capsys.readouterr().err.count("File not found") == 2


def test_a_sound_that_wont_play_is_only_reported(monkeypatch, capsys):
    def fail(_volume, _wav):
        raise OSError("no audio device")

    monkeypatch.setattr(setup.obs_config, "play_clip_sound", fail)
    setup.play_clip_sound(0.5)
    assert "no audio device" in capsys.readouterr().err


CAPTURE_SCENE = {"sources": [
    {"id": "monitor_capture", "uuid": "u1", "name": "Display Capture", "settings": {"monitor_id": "x"}},
    {"id": "scene", "settings": {"items": [{"source_uuid": "u1", "name": "Display Capture"},
                                           {"source_uuid": "u2", "name": "Other"}]}},
]}


def test_linux_swaps_the_windows_display_capture_for_pipewire(scene):
    scene.write_text(json.dumps(CAPTURE_SCENE))
    setup.ensure_linux_capture_source()
    source, scene_source = json.loads(scene.read_text())["sources"]
    assert source["id"] == source["versioned_id"] == setup.LINUX_CAPTURE_SOURCE_ID
    assert source["name"] == setup.LINUX_CAPTURE_SOURCE_NAME and source["settings"] == {"ShowCursor": True}
    items = scene_source["settings"]["items"]
    assert items[0]["name"] == setup.LINUX_CAPTURE_SOURCE_NAME and items[1]["name"] == "Other"


def test_the_capture_swap_is_only_done_once(scene):
    scene.write_text(json.dumps({"sources": [{"id": setup.LINUX_CAPTURE_SOURCE_ID, "settings": {"RestoreToken": "t"}}]}))
    before = scene.read_text()
    setup.ensure_linux_capture_source()
    assert scene.read_text() == before


def test_a_scene_collection_with_no_capture_is_left_alone(scene):
    setup.ensure_linux_capture_source()  # not there at all
    assert not scene.exists()
    scene.write_text(json.dumps({"sources": [{"id": "other"}]}))
    before = scene.read_text()
    setup.ensure_linux_capture_source()
    assert scene.read_text() == before


def test_linux_swaps_the_windows_audio_sources_keeping_their_devices(scene):
    scene.write_text(json.dumps(AUDIO_SCENE))
    setup.ensure_linux_audio_source_ids()
    saved = json.loads(scene.read_text())
    assert saved["DesktopAudioDevice1"]["id"] == saved["DesktopAudioDevice1"]["versioned_id"] == "pulse_output_capture"
    assert saved["AuxAudioDevice1"]["id"] == "pulse_input_capture"
    assert saved["DesktopAudioDevice1"]["settings"]["device_id"] == "default"


def test_the_audio_swap_is_only_done_once(scene):
    scene.write_text(json.dumps(AUDIO_SCENE))
    setup.ensure_linux_audio_source_ids()
    scene.write_text(scene.read_text() + " ")
    before = scene.read_text()
    setup.ensure_linux_audio_source_ids()
    assert scene.read_text() == before


def test_the_audio_swap_needs_a_scene_collection(scene):
    setup.ensure_linux_audio_source_ids()
    assert not scene.exists()


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "ClipEm"
    scripts = root / "App" / "Setup-Uninstall_Scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(setup, "ROOT_DIR", str(root))
    monkeypatch.setattr(setup, "UNINSTALL_SCRIPTS_SRC_DIR", str(scripts))
    return root, scripts


def test_setup_scripts_are_tucked_away_and_old_run_scripts_removed(root):
    folder, scripts = root
    (folder / "Setup_Linux.sh").write_text("setup")
    (scripts / "RunLinux.sh").write_text("stale")
    (scripts / "keep.txt").write_text("keep")
    setup.relocate_setup_scripts()
    assert not (folder / "Setup_Linux.sh").exists() and (scripts / "Setup_Linux.sh").read_text() == "setup"
    assert not (scripts / "RunLinux.sh").exists() and (scripts / "keep.txt").exists()


@pytest.mark.parametrize("system, name, other", [
    ("Linux", "UninstallAutostartLinux.sh", "UninstallAutostartWindows.bat"),
    ("Windows", "UninstallAutostartWindows.bat", "UninstallAutostartLinux.sh"),
])
def test_only_this_platforms_uninstall_script_lands_in_the_root(root, monkeypatch, system, name, other):
    folder, scripts = root
    (scripts / name).write_text("uninstall")
    (scripts / other).write_text("the other one")
    monkeypatch.setattr(setup.platform, "system", lambda: system)
    setup.install_uninstall_scripts()
    assert (folder / name).read_text() == "uninstall" and (scripts / name).exists() and not (folder / other).exists()
    if name.endswith(".sh"):
        assert os.access(folder / name, os.X_OK)


def test_an_uninstall_script_already_there_is_kept(root, monkeypatch):
    folder, scripts = root
    (folder / "UninstallAutostartLinux.sh").write_text("edited")
    (scripts / "UninstallAutostartLinux.sh").write_text("template")
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    setup.install_uninstall_scripts()
    assert (folder / "UninstallAutostartLinux.sh").read_text() == "edited"


def test_macos_has_no_uninstall_script(root, monkeypatch):
    folder, _scripts = root
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    setup.install_uninstall_scripts()
    assert sorted(os.listdir(folder)) == ["App"]


def test_a_clip_folder_is_only_made_at_the_end_of_setup(fake_obs_config, tmp_path):
    (fake_obs_config / "basic.ini").write_text("[SimpleOutput]\nFilePath=/x\n\n[AdvOut]\nRecFilePath=/x\n")
    folder = tmp_path / "picked" / "Clips"
    setup.set_clip_folder(str(folder))
    assert not folder.exists()
    assert setup.load_clip_folder() == str(folder)
    setup.ensure_clip_folder_exists()
    assert folder.is_dir()


def test_no_config_to_save_a_clip_folder_in_is_reported(monkeypatch, capsys):
    def missing(_folder):
        raise FileNotFoundError("Config file not found")

    monkeypatch.setattr(setup.obs_config, "save_clip_folder", missing)
    setup.set_clip_folder("/x")
    captured = capsys.readouterr()
    assert "Config file not found" in captured.err and "Clip output folder set" not in captured.out


@pytest.mark.parametrize("system, picker, picked, expected", [
    ("Linux", "pick_folder_linux", "/picked", "/picked"),
    ("Windows", "pick_folder_windows", "/picked", "/picked"),
    ("Darwin", "pick_folder_macos", "/picked", "/picked"),
    ("Linux", "pick_folder_linux", None, None),  # backed out: the default
])
def test_the_clip_folder_prompt(monkeypatch, system, picker, picked, expected):
    chosen = []
    monkeypatch.setattr(setup, "set_clip_folder", chosen.append)
    monkeypatch.setattr(setup, "ask_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(setup.platform, "system", lambda: system)
    monkeypatch.setattr(setup, picker, lambda: picked)
    setup.configure_clip_folder()
    assert chosen == [expected or os.path.join(setup.ROOT_DIR, "Clips")]


def test_the_default_clip_folder_is_beside_clipem(monkeypatch):
    chosen = []
    monkeypatch.setattr(setup, "set_clip_folder", chosen.append)
    monkeypatch.setattr(setup, "ask_yes_no", lambda *a, **k: False)
    setup.configure_clip_folder()
    assert chosen == [os.path.join(setup.ROOT_DIR, "Clips")]
