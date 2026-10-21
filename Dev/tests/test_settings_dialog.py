import os
import sys

import pytest

pytest.importorskip("tkinter")
from conftest import TRAY_WIDGET_DIR  # noqa: E402

sys.path.insert(0, TRAY_WIDGET_DIR)
import obs_config  # noqa: E402
import settings_dialog  # noqa: E402

INITIAL = {
    "websocket": {"port": 4455, "password": "old-password-123"},
    "clip_hotkey": [False, True, False, "~"],
}


def _with(**changes):
    return {**INITIAL, **changes}


def test_nothing_changed_writes_nothing(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: pytest.fail("no need to ask"))
    assert settings_dialog.save_obs_settings(dict(INITIAL), INITIAL) == "unchanged"
    assert not os.path.exists(obs_config.PENDING_OBS_SETTINGS_PATH)


def test_obs_closed_applies_straight_away(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: False)

    outcome = settings_dialog.save_obs_settings(_with(clip_hotkey=[True, False, False, "F9"]), INITIAL)

    assert outcome == "applied"
    assert obs_config.load_clip_hotkey() == (True, False, False, "F9")
    assert not os.path.exists(obs_config.PENDING_OBS_SETTINGS_PATH)


def test_obs_running_only_queues_changed_settings(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: True)

    outcome = settings_dialog.save_obs_settings(_with(clip_hotkey=[True, False, False, "F9"]), INITIAL)

    assert outcome == "queued"
    assert obs_config.load_pending_obs_settings() == {"clip_hotkey": [True, False, False, "F9"]}
    assert obs_config.load_clip_hotkey() == (False, True, False, "~")  # OBS's own file untouched


def test_queued_settings_accumulate_until_applied(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: True)
    settings_dialog.save_obs_settings(_with(clip_hotkey=[True, False, False, "F9"]), INITIAL)
    settings_dialog.save_obs_settings(_with(websocket={"port": 4460, "password": "new-password"}), INITIAL)
    assert set(obs_config.load_pending_obs_settings()) == {"clip_hotkey", "websocket"}


def test_a_second_launch_brings_the_open_dialog_to_the_front(tmp_path, monkeypatch):
    import time
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    raise_path = tmp_path / ".settings.raise"
    monkeypatch.setattr(settings_dialog, "RAISE_PATH", str(raise_path))
    monkeypatch.setattr(settings_dialog, "RAISE_POLL_MS", 20)
    raised = []
    monkeypatch.setattr(settings_dialog, "bring_to_front", lambda window: raised.append(window))
    try:
        settings_dialog.watch_for_reopen(root)
        deadline = time.time() + 0.2
        while time.time() < deadline:
            root.update()
        assert raised == []  # nobody asked
        raise_path.write_text("raise")  # what a second launch does
        deadline = time.time() + 2
        while not raised and time.time() < deadline:
            root.update()
            time.sleep(0.01)
        assert raised == [root] and not raise_path.exists()
    finally:
        root.destroy()


def test_a_second_launch_asks_the_first_and_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_dialog, "RAISE_PATH", str(tmp_path / ".settings.raise"))
    monkeypatch.setattr(settings_dialog, "acquire_instance_lock", lambda path: False)
    monkeypatch.setattr(settings_dialog, "exit_with_parent", lambda pid: None)
    monkeypatch.setattr(sys, "argv", ["settings_dialog.py"])
    settings_dialog.main()  # returns instead of opening a second dialog
    assert (tmp_path / ".settings.raise").exists()
