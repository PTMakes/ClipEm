import json
import os

import pytest
import setup


def test_bitrate_math_round_trips():
    kbps = setup.compute_video_bitrate_for_filesize(10, 60)
    assert kbps == int(10 * 8192 / 60 - setup.AUDIO_BITRATE_KBPS)
    assert setup.compute_filesize_for_bitrate(kbps, 60) == pytest.approx(10, rel=0.01)
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
