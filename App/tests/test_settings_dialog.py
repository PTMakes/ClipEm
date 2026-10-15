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
