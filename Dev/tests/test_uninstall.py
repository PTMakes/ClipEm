"""Installer/uninstall.py and Settings' Uninstall: what goes (ClipEm's files
and entries), what never does (the clips), and the hand-over to the
uninstaller once Settings has exited. Every test works on a throwaway copy
of an install - never this checkout, which refusal() also protects."""

import os
import shutil
import subprocess
import sys

import pytest
from conftest import INSTALLER_DIR, TRAY_WIDGET_DIR

sys.path.insert(0, INSTALLER_DIR)
import uninstall  # noqa: E402

SHARED_DIR = os.path.dirname(INSTALLER_DIR)


def _install(root):
    """A ClipEm folder as Setup leaves it, with clips and a file of the user's."""
    (root / "App" / "Shared").mkdir(parents=True)
    (root / "App" / "cache").mkdir()
    (root / "App" / "cache" / "thumb.jpg").write_bytes(b"jpg")
    (root / "Clips").mkdir()
    (root / "Clips" / "Replay-1.mp4").write_bytes(b"clip")
    for name in ("ClipEm.desktop", "UninstallAutostartLinux.sh"):
        (root / name).write_text("x")
    return root


@pytest.mark.skipif(not uninstall._checkout(SHARED_DIR), reason="not run from a git checkout (the Windows VM's copy)")
def test_this_checkout_is_never_uninstalled():
    # Its ClipEm folder is <checkout>/App - the .git is above it, not in it.
    assert "development checkout" in uninstall.refusal(uninstall.ROOT_DIR)
    assert "development checkout" in uninstall.refusal(os.path.join(uninstall.ROOT_DIR, "App"))


def test_what_stops_an_uninstall(tmp_path):
    root = _install(tmp_path / "ClipEm")
    assert uninstall.refusal(str(root), str(root / "Clips")) is None
    assert uninstall.refusal(str(root), "/elsewhere/Clips") is None
    assert "inside ClipEm's App folder" in uninstall.refusal(str(root), str(root / "App" / "Clips"))
    assert "doesn't look like a ClipEm install" in uninstall.refusal(str(tmp_path / "nothing"))
    (tmp_path / ".git").mkdir()  # a checkout around it
    assert "development checkout" in uninstall.refusal(str(root))


def test_clipems_files_go_and_the_clips_stay(tmp_path):
    root = _install(tmp_path / "ClipEm")
    (root / "notes.txt").write_text("mine")
    assert uninstall.remove_files(str(root), log=lambda m: None) == []
    assert sorted(os.listdir(root)) == ["Clips", "notes.txt"]
    assert (root / "Clips" / "Replay-1.mp4").read_bytes() == b"clip"


def test_an_emptied_folder_goes_too(tmp_path):
    root = tmp_path / "ClipEm"
    (root / "App").mkdir(parents=True)
    (root / "ClipEm.lnk").write_text("x")
    uninstall.remove_files(str(root), log=lambda m: None)
    assert not root.exists()


def test_the_entries_outside_the_folder_are_removed(monkeypatch):
    calls = []
    entries = [{"this_install": True, "path": "ours"}, {"this_install": False, "path": "another copy's"}]
    monkeypatch.setattr(uninstall.autostart, "find_autostart_entries", lambda: entries)
    monkeypatch.setattr(uninstall.autostart, "remove_autostart_entries", lambda found: calls.append(("autostart", found)))
    monkeypatch.setattr(uninstall.autostart, "uninstall_shortcut", lambda kind: calls.append(("shortcut", kind)))
    monkeypatch.setattr(uninstall.autostart, "remove_start_menu_shortcut", lambda: calls.append(("menu",)))
    monkeypatch.setattr(uninstall.autostart, "remove_window_desktop_entries", lambda: calls.append(("windows",)))
    uninstall.remove_entries(log=print)
    assert calls == [("autostart", [entries[0]]), ("shortcut", "start_menu"), ("shortcut", "desktop"), ("menu",),
                     ("windows",)]


def test_uninstall_exe_starts_once_settings_has_exited():
    cmd = uninstall.windows_uninstaller_command(r"C:\Users\O'Neil\ClipEm\Uninstall.exe", 1234)
    script = cmd[-1]
    assert cmd[0] == "powershell" and script.index("Wait-Process -Id 1234") < script.index("Start-Process")
    assert r"'C:\Users\O''Neil\ClipEm\Uninstall.exe'" in script  # quoted for PowerShell


@pytest.mark.skipif(sys.platform == "win32", reason="Windows hands the files to Uninstall.exe")
def test_the_detached_uninstall_removes_a_real_install(tmp_path):
    root = _install(tmp_path / "ClipEm")
    shared = root / "App" / "Shared"
    for folder in ("Installer", "Scripts"):
        shutil.copytree(os.path.join(SHARED_DIR, folder), shared / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "mic-filter", "*.png"))
    (shared / "VERSION").write_text("0.0.0\n")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}  # no window
    env["HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, str(shared / "Installer" / "uninstall.py"), "--files",
                             "--after", "999999999"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(os.listdir(root)) == ["Clips"] and (root / "Clips" / "Replay-1.mp4").exists()
    assert "Your clips are still in" in result.stdout


def _settings(monkeypatch, system, closes=True, exe=None):
    sys.path.insert(0, TRAY_WIDGET_DIR)
    import settings_dialog

    calls = []
    monkeypatch.setattr(settings_dialog.sys, "platform", system)
    monkeypatch.setattr(settings_dialog.obs_process, "close_obs_stopping_outputs", lambda log: closes)
    monkeypatch.setattr(settings_dialog, "stop_clip_editor", lambda log: calls.append("editor"))
    monkeypatch.setattr(settings_dialog, "stop_tray_widget", lambda log: calls.append("tray"))
    monkeypatch.setattr(settings_dialog.uninstall, "remove_entries", lambda log: calls.append("entries"))
    monkeypatch.setattr(settings_dialog.uninstall, "windows_uninstaller", lambda: exe)
    monkeypatch.setattr(settings_dialog.uninstall, "start_windows_uninstaller",
                        lambda path, pid: calls.append(("uninstaller", path, pid)))
    monkeypatch.setattr(settings_dialog.uninstall, "start_file_removal",
                        lambda after_pid: calls.append(("files", after_pid)))
    return settings_dialog, calls


def test_settings_closes_clipem_then_hands_over_to_the_uninstaller(monkeypatch):
    settings_dialog, calls = _settings(monkeypatch, "win32", exe=r"C:\ClipEm\Uninstall.exe")
    assert settings_dialog.uninstall_clipem(log=print, pid=42) == "uninstaller"
    assert calls == ["editor", "tray", "entries", ("uninstaller", r"C:\ClipEm\Uninstall.exe", 42)]

    settings_dialog, calls = _settings(monkeypatch, "linux")
    assert settings_dialog.uninstall_clipem(log=print, pid=42) == "files"
    assert calls == ["editor", "tray", "entries", ("files", 42)]

    settings_dialog, calls = _settings(monkeypatch, "win32", exe=None)  # a copy of the folder
    assert settings_dialog.uninstall_clipem(log=print, pid=42) == "folder" and calls[-1] == "entries"


def test_nothing_is_removed_while_obs_wont_close(monkeypatch):
    settings_dialog, calls = _settings(monkeypatch, "linux", closes=False)
    assert "OBS didn't close" in settings_dialog.uninstall_clipem(log=print, pid=42)
    assert calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' PowerShell hand-over")
def test_the_uninstaller_starts_only_once_settings_has_exited(tmp_path):
    import time

    marker = tmp_path / "started.txt"
    stand_in = tmp_path / "Uninstall it.cmd"  # a space and the like: quoted
    stand_in.write_text(f'@echo started> "{marker}"\r\n')
    settings = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4)"])
    uninstall.start_windows_uninstaller(str(stand_in), settings.pid)
    time.sleep(2)
    assert not marker.exists()  # Settings is still running
    settings.wait()
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert marker.exists()
